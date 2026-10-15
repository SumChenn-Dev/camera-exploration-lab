"""Synthetic-testable repository primitives for future grouped recording.

These APIs do not attach to capture workers and are not called by the GUI.
They require a database that has already been explicitly migrated to schema 3.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .db import Database, serialize_json
from .models import BackendKind, CameraIdentity, MeasurementSample, SQLITE_INT_MAX, utc_now_iso
from .recording_ownership import RecordingOwnership
from .schema_migrations import GROUPED_SCHEMA_VERSION


MAX_GROUP_MEASUREMENT_BATCH = 1024
MAX_EVENT_DETAIL_BYTES = 8192
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class GroupedRepositoryError(RuntimeError):
    """Base error for grouped repository operations."""


class GroupInvariantError(GroupedRepositoryError):
    """A requested operation violates grouped experiment invariants."""


class RecordingLeaseBusyError(GroupedRepositoryError):
    """Another process owns the single-database recording lease."""


class RecordingLeaseOwnershipError(GroupedRepositoryError):
    """A caller attempted to release a lease it does not own."""


class GroupStatus(StrEnum):
    PENDING = "pending"
    RECORDING = "recording"
    DEGRADED = "degraded"
    COMPLETED = "completed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class StreamStatus(StrEnum):
    PENDING = "pending"
    RECORDING = "recording"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


_GROUP_TRANSITIONS: dict[GroupStatus, set[GroupStatus]] = {
    GroupStatus.PENDING: {
        GroupStatus.RECORDING, GroupStatus.DEGRADED, GroupStatus.PARTIAL,
        GroupStatus.FAILED, GroupStatus.INTERRUPTED,
    },
    GroupStatus.RECORDING: {
        GroupStatus.DEGRADED, GroupStatus.COMPLETED, GroupStatus.PARTIAL,
        GroupStatus.INTERRUPTED, GroupStatus.FAILED,
    },
    GroupStatus.DEGRADED: {
        GroupStatus.PARTIAL, GroupStatus.INTERRUPTED, GroupStatus.FAILED,
    },
    GroupStatus.COMPLETED: set(),
    GroupStatus.PARTIAL: set(),
    GroupStatus.INTERRUPTED: set(),
    GroupStatus.FAILED: set(),
}
_STREAM_TRANSITIONS: dict[StreamStatus, set[StreamStatus]] = {
    StreamStatus.PENDING: {
        StreamStatus.RECORDING, StreamStatus.INTERRUPTED, StreamStatus.FAILED,
    },
    StreamStatus.RECORDING: {
        StreamStatus.COMPLETED, StreamStatus.INTERRUPTED, StreamStatus.FAILED,
    },
    StreamStatus.COMPLETED: set(),
    StreamStatus.INTERRUPTED: set(),
    StreamStatus.FAILED: set(),
}
_COUNTER_COLUMNS = {
    "capture_frame_count",
    "read_failure_count",
    "invalid_frame_count",
    "timing_queue_loss_count",
    "persistence_loss_count",
    "preview_skip_count",
}


def _validate_nonnegative_sqlite_int(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or None")
    if value < 0 or value > SQLITE_INT_MAX:
        raise ValueError(f"{name} is outside SQLite's nonnegative integer range")


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _enum_value(value: StrEnum | str) -> str:
    return str(value.value) if isinstance(value, StrEnum) else str(value)


def _json_object(value: Mapping[str, Any], *, field_name: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    text = serialize_json(dict(value))
    if len(text.encode("utf-8")) > MAX_EVENT_DETAIL_BYTES:
        raise ValueError(f"{field_name} exceeds the allowed structured-data size")
    return text


@dataclass(frozen=True, slots=True)
class GroupedSetting:
    setting_key: str
    requested_value: Any = None
    reported_value: Any = None
    verified_value: Any = None
    verification_status: str = "not_observed"
    verification_method: str | None = None
    requested_at: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class GroupedStreamSpec:
    slot: str
    camera: CameraIdentity
    endpoint_key: str
    benchmark_label: str
    configuration: Mapping[str, Any]
    settings: tuple[GroupedSetting, ...] = ()
    timing_method: str = "opencv_read_return_v1"
    timestamp_source: str = "host_monotonic_after_read"
    clock_type: str = "monotonic_ns"
    collection_point: str = "after_videocapture_read_return"
    timing_method_version: str = "1"


@dataclass(frozen=True, slots=True)
class GroupedExperimentSpec:
    label: str
    streams: tuple[GroupedStreamSpec, ...]
    notes: str | None = None
    duration_target_seconds: float | None = None
    record_requested_at: str = field(default_factory=utc_now_iso)
    created_at: str = field(default_factory=utc_now_iso)
    clock_epoch_id: str = field(default_factory=lambda: str(uuid4()))
    timing_method_version: str = "1"
    session_id: str = field(default_factory=lambda: str(uuid4()))
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroupedSettingRecord:
    setting_key: str
    requested_value: Any
    reported_value: Any
    verified_value: Any
    verification_status: str
    verification_method: str | None


@dataclass(frozen=True, slots=True)
class GroupedStreamRecord:
    session_id: str
    slot: str
    benchmark_id: str
    camera_id: str | None
    endpoint_key: str
    identity_snapshot: Mapping[str, Any]
    camera_record: Mapping[str, Any] | None
    benchmark_label: str
    benchmark_status: str
    configuration: Mapping[str, Any]
    stream_status: str
    timing_method: str
    timestamp_source: str
    clock_type: str
    collection_point: str
    timing_method_version: str
    first_host_arrival_elapsed_ns: int | None
    last_host_arrival_elapsed_ns: int | None
    first_persisted_elapsed_ns: int | None
    last_persisted_elapsed_ns: int | None
    last_persisted_sample_index: int | None
    capture_frame_count: int
    read_failure_count: int
    invalid_frame_count: int
    timing_queue_loss_count: int
    persistence_loss_count: int
    sequence_gap_count: int
    preview_skip_count: int | None
    data_complete: bool
    integrity_flags: tuple[str, ...]
    summary: Mapping[str, Any] | None
    settings: tuple[GroupedSettingRecord, ...]


@dataclass(frozen=True, slots=True)
class ExperimentEventRecord:
    event_id: int
    benchmark_id: str | None
    event_type: str
    elapsed_ns: int | None
    observed_at: str
    reason_code: str | None
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GroupedExperimentRecord:
    session_id: str
    label: str
    notes: str | None
    source: str
    started_at: str | None
    ended_at: str | None
    status: str
    duration_target_seconds: float | None
    record_requested_at: str | None
    recording_started_at: str | None
    stop_requested_at: str | None
    finalized_at: str | None
    clock_epoch_id: str
    timing_method_version: str
    data_complete: bool
    integrity_flags: tuple[str, ...]
    streams: tuple[GroupedStreamRecord, ...]
    events: tuple[ExperimentEventRecord, ...]


@dataclass(frozen=True, slots=True)
class RecordingLease:
    owner_token: str
    session_id: str
    owner_pid: int
    owner_process_start_ticks: str | None
    acquired_at: str


def _decode_json_object(value: str | None, *, field_name: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GroupInvariantError(f"stored {field_name} is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise GroupInvariantError(f"stored {field_name} is not a JSON object")
    return decoded


def _camera_snapshot(camera: CameraIdentity) -> dict[str, Any]:
    return {
        "stable_identifier": camera.stable_identifier,
        "backend": _enum_value(camera.backend),
        "device_path": camera.device_path,
        "manufacturer": camera.manufacturer,
        "model": camera.model,
        "serial_number": camera.serial_number,
        "vendor_id": camera.vendor_id,
        "product_id": camera.product_id,
        "identity_source": camera.identity_source,
        "metadata": dict(camera.metadata),
    }


class GroupedExperimentRepository:
    """Repository for schema-v3 grouped records; no capture or writer thread."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_group(
        self,
        spec: GroupedExperimentSpec,
        *,
        ownership: RecordingOwnership | None = None,
    ) -> GroupedExperimentRecord:
        _required_text("experiment label", spec.label)
        _required_text("clock_epoch_id", spec.clock_epoch_id)
        if spec.duration_target_seconds is not None and (
            isinstance(spec.duration_target_seconds, bool)
            or not isinstance(spec.duration_target_seconds, (int, float))
            or not math.isfinite(float(spec.duration_target_seconds))
            or spec.duration_target_seconds <= 0
        ):
            raise ValueError("duration_target_seconds must be a finite positive number or None")
        if len(spec.streams) != 2 or {stream.slot for stream in spec.streams} != {"A", "B"}:
            raise GroupInvariantError("a grouped experiment requires exactly slots A and B")
        if len({stream.endpoint_key for stream in spec.streams}) != 2:
            raise GroupInvariantError("stream endpoints must identify two distinct capture endpoints")
        for stream in spec.streams:
            _required_text("camera stable identifier", stream.camera.stable_identifier)
            _required_text("endpoint_key", stream.endpoint_key)
            _required_text("benchmark_label", stream.benchmark_label)
            for name in (
                "timing_method", "timestamp_source", "clock_type",
                "collection_point", "timing_method_version",
            ):
                _required_text(name, getattr(stream, name))
            _json_object(stream.configuration, field_name="stream configuration")
            keys = [setting.setting_key for setting in stream.settings]
            if len(keys) != len(set(keys)):
                raise GroupInvariantError("setting keys must be unique within each stream")
            for setting in stream.settings:
                _required_text("setting_key", setting.setting_key)

        session_id = spec.session_id
        run_ids = {stream.slot: str(uuid4()) for stream in spec.streams}
        group_metadata = dict(spec.metadata)
        group_metadata["experiment_type"] = "grouped"
        group_metadata["schema_design_version"] = GROUPED_SCHEMA_VERSION

        with self.database.connect() as connection:
            self._require_v3(connection)
            if ownership is not None:
                ownership.assert_active(self.database)
            connection.execute(
                """INSERT INTO sessions
                   (id, camera_id, label, source, started_at, ended_at, created_at, notes, metadata_json)
                   VALUES (?, NULL, ?, 'grouped_experiment', ?, NULL, ?, ?, ?)""",
                (
                    session_id, spec.label, spec.record_requested_at, spec.created_at,
                    spec.notes, serialize_json(group_metadata),
                ),
            )
            connection.execute(
                """INSERT INTO group_recordings
                   (session_id, status, duration_target_seconds, record_requested_at,
                    clock_epoch_id, timing_method_version, metadata_json)
                   VALUES (?, 'pending', ?, ?, ?, ?, ?)""",
                (
                    session_id, spec.duration_target_seconds, spec.record_requested_at,
                    spec.clock_epoch_id, spec.timing_method_version, serialize_json(group_metadata),
                ),
            )
            for stream in sorted(spec.streams, key=lambda item: item.slot):
                camera_id = self._upsert_camera(connection, stream.camera, spec.created_at)
                benchmark_id = run_ids[stream.slot]
                connection.execute(
                    """INSERT INTO benchmark_runs
                       (id, session_id, label, started_at, ended_at, status, configuration_json)
                       VALUES (?, ?, ?, NULL, NULL, 'pending', ?)""",
                    (
                        benchmark_id, session_id, stream.benchmark_label,
                        serialize_json(dict(stream.configuration)),
                    ),
                )
                connection.execute(
                    """INSERT INTO benchmark_streams (
                           session_id, slot, benchmark_id, camera_id, endpoint_key,
                           identity_snapshot_json, timing_method, timestamp_source,
                           clock_type, collection_point, timing_method_version, stream_status
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (
                        session_id, stream.slot, benchmark_id, camera_id, stream.endpoint_key,
                        serialize_json(_camera_snapshot(stream.camera)), stream.timing_method,
                        stream.timestamp_source, stream.clock_type, stream.collection_point,
                        stream.timing_method_version,
                    ),
                )
                for setting in stream.settings:
                    connection.execute(
                        """INSERT INTO setting_snapshots (
                               id, session_id, benchmark_id, raw_record_id, setting_key,
                               requested_value_json, reported_value_json, verified_value_json,
                               verification_status, requested_at, checked_at, verification_method, raw_json
                           ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
                        (
                            str(uuid4()), session_id, benchmark_id, setting.setting_key,
                            serialize_json(setting.requested_value)
                            if setting.requested_value is not None else None,
                            serialize_json(setting.reported_value)
                            if setting.reported_value is not None else None,
                            serialize_json(setting.verified_value)
                            if setting.verified_value is not None else None,
                            setting.verification_status, setting.requested_at, setting.checked_at,
                            setting.verification_method,
                        ),
                    )
            self._insert_event(
                connection, session_id, None, "group_created", None,
                spec.record_requested_at, None, {"stream_slots": ["A", "B"]},
            )

        group = self.get_group(session_id)
        if group is None:
            raise GroupInvariantError("created group could not be read back")
        return group

    def delete_unstarted_group(
        self,
        session_id: str,
        *,
        ownership: RecordingOwnership,
        owner_token: str,
    ) -> None:
        """Remove only a factory-created group that never acquired a writer lease."""
        ownership.assert_active(self.database, owner_token)
        with self.database.connect() as connection:
            self._require_v3(connection)
            ownership.assert_active(self.database, owner_token)
            parent = connection.execute(
                "SELECT status FROM group_recordings WHERE session_id=?", (session_id,)
            ).fetchone()
            if parent is None or parent["status"] != GroupStatus.PENDING:
                raise GroupInvariantError("only an unstarted pending group can be discarded")
            streams = connection.execute(
                "SELECT benchmark_id, stream_status FROM benchmark_streams WHERE session_id=? ORDER BY slot",
                (session_id,),
            ).fetchall()
            if len(streams) != 2 or any(
                row["stream_status"] != StreamStatus.PENDING for row in streams
            ):
                raise GroupInvariantError("group already contains recording lifecycle changes")
            run_ids = tuple(str(row["benchmark_id"]) for row in streams)
            placeholders = ",".join("?" for _ in run_ids)
            if connection.execute(
                f"SELECT 1 FROM measurements WHERE benchmark_id IN ({placeholders}) LIMIT 1",
                run_ids,
            ).fetchone():
                raise GroupInvariantError("group already contains measurement rows")
            if connection.execute(
                "SELECT 1 FROM experiment_events WHERE session_id=? "
                "AND event_type!='group_created' LIMIT 1",
                (session_id,),
            ).fetchone():
                raise GroupInvariantError("group already contains recording events")
            lease = connection.execute(
                "SELECT session_id, owner_token FROM recording_leases WHERE lease_key=1"
            ).fetchone()
            if lease is not None:
                if lease["session_id"] != session_id or lease["owner_token"] != owner_token:
                    raise RecordingLeaseOwnershipError("group has a different active recording lease")
                connection.execute(
                    "DELETE FROM recording_leases WHERE lease_key=1 AND owner_token=?",
                    (owner_token,),
                )
            connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    @staticmethod
    def _require_v3(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != GROUPED_SCHEMA_VERSION:
            raise GroupedRepositoryError(
                f"grouped repository requires schema version {GROUPED_SCHEMA_VERSION}"
            )

    @staticmethod
    def _require_lease_owner(
        connection: sqlite3.Connection,
        session_id: str,
        owner_token: str | None,
    ) -> None:
        """Enforce the database-wide lease whenever one is active.

        Ordinary repository use without a lease remains compatible. Once a
        lease row exists, however, grouped writes must name its exact session
        and owner token. Age alone never invalidates or transfers ownership.
        """
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT session_id, owner_token FROM recording_leases WHERE lease_key=1"
        ).fetchone()
        if row is None:
            if owner_token is not None:
                raise RecordingLeaseOwnershipError("recording lease is no longer active")
            return
        if row["session_id"] != session_id or row["owner_token"] != owner_token:
            raise RecordingLeaseOwnershipError("grouped write is not owned by the active recording lease")

    @staticmethod
    def _upsert_camera(
        connection: sqlite3.Connection,
        camera: CameraIdentity,
        seen_at: str,
    ) -> str:
        backend = _enum_value(camera.backend)
        camera_id = camera.stable_identifier
        connection.execute(
            """INSERT INTO cameras (
                   id, stable_identifier, backend, device_path, manufacturer, model,
                   serial_number, vendor_id, product_id, identity_source,
                   first_seen_at, last_seen_at, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stable_identifier) DO UPDATE SET
                   backend=excluded.backend,
                   device_path=excluded.device_path,
                   manufacturer=COALESCE(excluded.manufacturer, cameras.manufacturer),
                   model=COALESCE(excluded.model, cameras.model),
                   serial_number=COALESCE(excluded.serial_number, cameras.serial_number),
                   vendor_id=COALESCE(excluded.vendor_id, cameras.vendor_id),
                   product_id=COALESCE(excluded.product_id, cameras.product_id),
                   identity_source=excluded.identity_source,
                   last_seen_at=excluded.last_seen_at,
                   metadata_json=excluded.metadata_json""",
            (
                camera_id, camera.stable_identifier, backend, camera.device_path,
                camera.manufacturer, camera.model, camera.serial_number,
                camera.vendor_id, camera.product_id, camera.identity_source,
                seen_at, seen_at, serialize_json(camera.metadata),
            ),
        )
        return camera_id

    def get_group(self, session_id: str) -> GroupedExperimentRecord | None:
        with self.database.connect_readonly() as connection:
            return self._read_group(connection, session_id)

    def list_groups(
        self,
        *,
        status: GroupStatus | str | None = None,
        camera_id: str | None = None,
        limit: int = 100,
    ) -> tuple[GroupedExperimentRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.connect_readonly() as connection:
            self._require_v3(connection)
            conditions: list[str] = []
            parameters: list[Any] = []
            if status is not None:
                conditions.append("g.status=?")
                parameters.append(_enum_value(status))
            if camera_id is not None:
                conditions.append(
                    """EXISTS (SELECT 1 FROM benchmark_streams bs
                               WHERE bs.session_id=g.session_id AND bs.camera_id=?)"""
                )
                parameters.append(camera_id)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT g.session_id
                    FROM group_recordings g JOIN sessions s ON s.id=g.session_id
                    {where}
                    ORDER BY COALESCE(s.started_at, s.created_at) DESC, g.session_id
                    LIMIT ?""",
                parameters,
            ).fetchall()
            return tuple(
                group for row in rows
                if (group := self._read_group(connection, str(row["session_id"]))) is not None
            )

    @staticmethod
    def _read_group(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> GroupedExperimentRecord | None:
        parent = connection.execute(
            """SELECT g.*, s.label, s.notes, s.source, s.started_at, s.ended_at
               FROM group_recordings g JOIN sessions s ON s.id=g.session_id
               WHERE g.session_id=?""",
            (session_id,),
        ).fetchone()
        if parent is None:
            return None
        stream_rows = connection.execute(
            """SELECT bs.*, b.label AS benchmark_label, b.status AS benchmark_status,
                      b.configuration_json, c.stable_identifier, c.identity_source,
                      c.backend, c.device_path, c.manufacturer, c.model, c.serial_number,
                      c.vendor_id, c.product_id, z.frame_count, z.read_failures,
                      z.duration_seconds, z.capture_fps, z.preview_fps, z.processing_fps,
                      z.interval_count, z.median_interval_ms, z.p95_interval_ms,
                      z.p99_interval_ms, z.summary_json
               FROM benchmark_streams bs
               JOIN benchmark_runs b ON b.id=bs.benchmark_id AND b.session_id=bs.session_id
               LEFT JOIN cameras c ON c.id=bs.camera_id
               LEFT JOIN benchmark_summaries z ON z.benchmark_id=bs.benchmark_id
               WHERE bs.session_id=? ORDER BY bs.slot""",
            (session_id,),
        ).fetchall()
        if len(stream_rows) != 2 or [str(row["slot"]) for row in stream_rows] != ["A", "B"]:
            raise GroupInvariantError("stored group does not contain exactly stream slots A and B")

        stream_records: list[GroupedStreamRecord] = []
        for row in stream_rows:
            settings_rows = connection.execute(
                """SELECT setting_key, requested_value_json, reported_value_json,
                          verified_value_json, verification_status, verification_method
                   FROM setting_snapshots WHERE benchmark_id=? ORDER BY setting_key""",
                (row["benchmark_id"],),
            ).fetchall()
            settings = tuple(
                GroupedSettingRecord(
                    setting_key=str(setting["setting_key"]),
                    requested_value=json.loads(setting["requested_value_json"])
                    if setting["requested_value_json"] is not None else None,
                    reported_value=json.loads(setting["reported_value_json"])
                    if setting["reported_value_json"] is not None else None,
                    verified_value=json.loads(setting["verified_value_json"])
                    if setting["verified_value_json"] is not None else None,
                    verification_status=str(setting["verification_status"]),
                    verification_method=setting["verification_method"],
                )
                for setting in settings_rows
            )
            camera_record = None
            if row["stable_identifier"] is not None:
                camera_record = {
                    "stable_identifier": row["stable_identifier"],
                    "identity_source": row["identity_source"],
                    "backend": row["backend"],
                    "device_path": row["device_path"],
                    "manufacturer": row["manufacturer"],
                    "model": row["model"],
                    "serial_number": row["serial_number"],
                    "vendor_id": row["vendor_id"],
                    "product_id": row["product_id"],
                }
            summary = (
                _decode_json_object(row["summary_json"], field_name="benchmark summary")
                if row["summary_json"] is not None else None
            )
            if summary is not None:
                summary.update({
                    key: row[key] for key in (
                        "frame_count", "read_failures", "duration_seconds", "capture_fps",
                        "preview_fps", "processing_fps", "interval_count", "median_interval_ms",
                        "p95_interval_ms", "p99_interval_ms",
                    )
                })
            flags = tuple(sorted(
                _decode_json_object(row["integrity_flags_json"], field_name="stream integrity flags")
            ))
            stream_records.append(
                GroupedStreamRecord(
                    session_id=str(row["session_id"]),
                    slot=str(row["slot"]),
                    benchmark_id=str(row["benchmark_id"]),
                    camera_id=row["camera_id"],
                    endpoint_key=str(row["endpoint_key"]),
                    identity_snapshot=_decode_json_object(
                        row["identity_snapshot_json"], field_name="camera identity snapshot"
                    ),
                    camera_record=camera_record,
                    benchmark_label=str(row["benchmark_label"]),
                    benchmark_status=str(row["benchmark_status"]),
                    configuration=_decode_json_object(
                        row["configuration_json"], field_name="benchmark configuration"
                    ),
                    stream_status=str(row["stream_status"]),
                    timing_method=str(row["timing_method"]),
                    timestamp_source=str(row["timestamp_source"]),
                    clock_type=str(row["clock_type"]),
                    collection_point=str(row["collection_point"]),
                    timing_method_version=str(row["timing_method_version"]),
                    first_host_arrival_elapsed_ns=row["first_host_arrival_elapsed_ns"],
                    last_host_arrival_elapsed_ns=row["last_host_arrival_elapsed_ns"],
                    first_persisted_elapsed_ns=row["first_persisted_elapsed_ns"],
                    last_persisted_elapsed_ns=row["last_persisted_elapsed_ns"],
                    last_persisted_sample_index=row["last_persisted_sample_index"],
                    capture_frame_count=int(row["capture_frame_count"]),
                    read_failure_count=int(row["read_failure_count"]),
                    invalid_frame_count=int(row["invalid_frame_count"]),
                    timing_queue_loss_count=int(row["timing_queue_loss_count"]),
                    persistence_loss_count=int(row["persistence_loss_count"]),
                    sequence_gap_count=int(row["sequence_gap_count"]),
                    preview_skip_count=row["preview_skip_count"],
                    data_complete=bool(row["data_complete"]),
                    integrity_flags=flags,
                    summary=summary,
                    settings=settings,
                )
            )
        event_rows = connection.execute(
            """SELECT event_id, benchmark_id, event_type, elapsed_ns, observed_at,
                      reason_code, detail_json
               FROM experiment_events WHERE session_id=?
               ORDER BY COALESCE(elapsed_ns, -1), event_id""",
            (session_id,),
        ).fetchall()
        events = tuple(
            ExperimentEventRecord(
                event_id=int(row["event_id"]),
                benchmark_id=row["benchmark_id"],
                event_type=str(row["event_type"]),
                elapsed_ns=row["elapsed_ns"],
                observed_at=str(row["observed_at"]),
                reason_code=row["reason_code"],
                details=_decode_json_object(row["detail_json"], field_name="event details"),
            )
            for row in event_rows
        )
        return GroupedExperimentRecord(
            session_id=str(parent["session_id"]),
            label=str(parent["label"]),
            notes=parent["notes"],
            source=str(parent["source"]),
            started_at=parent["started_at"],
            ended_at=parent["ended_at"],
            status=str(parent["status"]),
            duration_target_seconds=parent["duration_target_seconds"],
            record_requested_at=parent["record_requested_at"],
            recording_started_at=parent["recording_started_at"],
            stop_requested_at=parent["stop_requested_at"],
            finalized_at=parent["finalized_at"],
            clock_epoch_id=str(parent["clock_epoch_id"]),
            timing_method_version=str(parent["timing_method_version"]),
            data_complete=bool(parent["data_complete"]),
            integrity_flags=tuple(sorted(
                _decode_json_object(parent["integrity_flags_json"], field_name="group integrity flags")
            )),
            streams=tuple(stream_records),
            events=events,
        )

    def validate_ready_to_record(self, session_id: str) -> None:
        with self.database.connect_readonly() as connection:
            self._require_v3(connection)
            group = connection.execute(
                "SELECT status FROM group_recordings WHERE session_id=?", (session_id,)
            ).fetchone()
            if group is None:
                raise GroupInvariantError("unknown grouped experiment")
            slots = connection.execute(
                """SELECT slot, stream_status, endpoint_key, camera_id
                   FROM benchmark_streams WHERE session_id=? ORDER BY slot""",
                (session_id,),
            ).fetchall()
            if len(slots) != 2 or [row["slot"] for row in slots] != ["A", "B"]:
                raise GroupInvariantError("group must have exactly slots A and B")
            if len({row["endpoint_key"] for row in slots}) != 2:
                raise GroupInvariantError("group stream endpoints are not distinct")
            if any(str(row["stream_status"]) != StreamStatus.RECORDING for row in slots):
                raise GroupInvariantError("both child streams must be recording before the group starts")
            if str(group["status"]) != GroupStatus.PENDING:
                raise GroupInvariantError("only a pending group can enter recording")

    def set_stream_status(
        self,
        session_id: str,
        slot: str,
        status: StreamStatus | str,
        *,
        observed_at: str,
        elapsed_ns: int | None = None,
        reason_code: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        target = StreamStatus(_enum_value(status))
        _validate_nonnegative_sqlite_int("elapsed_ns", elapsed_ns)
        self._validate_reason(reason_code)
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            current = connection.execute(
                """SELECT bs.stream_status, bs.benchmark_id, bs.data_complete,
                          g.status AS group_status
                   FROM benchmark_streams bs JOIN group_recordings g ON g.session_id=bs.session_id
                   WHERE bs.session_id=? AND bs.slot=?""",
                (session_id, slot),
            ).fetchone()
            if current is None:
                raise GroupInvariantError("unknown grouped stream")
            source = StreamStatus(str(current["stream_status"]))
            if source == target:
                return
            if target not in _STREAM_TRANSITIONS[source]:
                raise GroupInvariantError(f"invalid stream transition: {source} to {target}")
            run_status = "running" if target == StreamStatus.RECORDING else target.value
            started_at = observed_at if target == StreamStatus.RECORDING else None
            ended_at = observed_at if target in {
                StreamStatus.COMPLETED, StreamStatus.INTERRUPTED, StreamStatus.FAILED
            } else None
            connection.execute(
                """UPDATE benchmark_streams SET stream_status=?,
                          data_complete=CASE WHEN ? IN ('interrupted','failed')
                                             THEN 0 ELSE data_complete END
                   WHERE session_id=? AND slot=?""",
                (target.value, target.value, session_id, slot),
            )
            connection.execute(
                "UPDATE benchmark_runs SET status=?, started_at=COALESCE(?, started_at), ended_at=? WHERE id=?",
                (run_status, started_at, ended_at, current["benchmark_id"]),
            )
            if target in {StreamStatus.INTERRUPTED, StreamStatus.FAILED}:
                connection.execute(
                    "UPDATE group_recordings SET data_complete=0 WHERE session_id=?",
                    (session_id,),
                )
            self._insert_event(
                connection, session_id, str(current["benchmark_id"]),
                f"stream_{target.value}", elapsed_ns, observed_at, reason_code, {},
            )
            parent_status = GroupStatus(str(current["group_status"]))
            if parent_status in {GroupStatus.PENDING, GroupStatus.RECORDING} and target in {
                StreamStatus.INTERRUPTED, StreamStatus.FAILED
            }:
                sibling_active = connection.execute(
                    """SELECT COUNT(*) FROM benchmark_streams
                       WHERE session_id=? AND stream_status='recording'""",
                    (session_id,),
                ).fetchone()[0]
                if sibling_active:
                    self._set_group_status_in_transaction(
                        connection, session_id, GroupStatus.DEGRADED, observed_at,
                        owner_token=owner_token,
                    )

    def set_group_status(
        self,
        session_id: str,
        status: GroupStatus | str,
        *,
        observed_at: str,
        elapsed_ns: int | None = None,
        reason_code: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        target = GroupStatus(_enum_value(status))
        if target in {
            GroupStatus.COMPLETED, GroupStatus.PARTIAL,
            GroupStatus.INTERRUPTED, GroupStatus.FAILED,
        }:
            raise GroupInvariantError("terminal group outcomes must be written through finalize_group")
        _validate_nonnegative_sqlite_int("elapsed_ns", elapsed_ns)
        self._validate_reason(reason_code)
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._set_group_status_in_transaction(
                connection, session_id, target, observed_at, elapsed_ns, reason_code,
                owner_token=owner_token,
            )

    def _set_group_status_in_transaction(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        target: GroupStatus,
        observed_at: str,
        elapsed_ns: int | None = None,
        reason_code: str | None = None,
        owner_token: str | None = None,
    ) -> None:
        self._require_lease_owner(connection, session_id, owner_token)
        row = connection.execute(
            "SELECT status FROM group_recordings WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise GroupInvariantError("unknown grouped experiment")
        source = GroupStatus(str(row["status"]))
        if source == target:
            return
        if target not in _GROUP_TRANSITIONS[source]:
            raise GroupInvariantError(f"invalid group transition: {source} to {target}")
        stream_rows = connection.execute(
            """SELECT slot, stream_status, endpoint_key
               FROM benchmark_streams WHERE session_id=? ORDER BY slot""",
            (session_id,),
        ).fetchall()
        if target in {GroupStatus.RECORDING, GroupStatus.DEGRADED, GroupStatus.COMPLETED,
                      GroupStatus.PARTIAL, GroupStatus.INTERRUPTED}:
            if len(stream_rows) != 2 or [r["slot"] for r in stream_rows] != ["A", "B"]:
                raise GroupInvariantError("group must contain exactly slots A and B")
            if len({r["endpoint_key"] for r in stream_rows}) != 2:
                raise GroupInvariantError("group stream endpoints are not distinct")
        if target == GroupStatus.RECORDING:
            if any(r["stream_status"] != StreamStatus.RECORDING for r in stream_rows):
                raise GroupInvariantError("both child streams must be recording before the group starts")
            integrity = connection.execute(
                "SELECT data_complete, integrity_flags_json FROM group_recordings WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if not bool(integrity["data_complete"]) or _decode_json_object(
                integrity["integrity_flags_json"], field_name="group integrity flags"
            ):
                raise GroupInvariantError("an incomplete group cannot enter recording")
        if target in {GroupStatus.PARTIAL, GroupStatus.INTERRUPTED, GroupStatus.FAILED}:
            if any(r["stream_status"] in {
                StreamStatus.PENDING, StreamStatus.RECORDING
            } for r in stream_rows):
                raise GroupInvariantError("all child streams must be terminal before group finalization")
        connection.execute(
            """UPDATE group_recordings SET status=?,
                      recording_started_at=CASE WHEN ?='recording'
                          THEN COALESCE(recording_started_at, ?) ELSE recording_started_at END,
                      stop_requested_at=CASE WHEN ? IN ('partial','interrupted','failed')
                          THEN COALESCE(stop_requested_at, ?) ELSE stop_requested_at END
               WHERE session_id=?""",
            (target.value, target.value, observed_at, target.value, observed_at, session_id),
        )
        self._insert_event(
            connection, session_id, None, f"group_{target.value}", elapsed_ns,
            observed_at, reason_code, {},
        )

    def increment_stream_counters(
        self,
        session_id: str,
        slot: str,
        increments: Mapping[str, int],
        *,
        observed_at: str,
        elapsed_ns: int | None = None,
        owner_token: str | None = None,
    ) -> None:
        if not increments:
            return
        _validate_nonnegative_sqlite_int("elapsed_ns", elapsed_ns)
        if set(increments) - _COUNTER_COLUMNS:
            raise ValueError("unsupported grouped stream counter")
        for name, value in increments.items():
            _validate_nonnegative_sqlite_int(name, value)
        assignments = [f"{name}=COALESCE({name},0)+?" for name in sorted(increments)]
        parameters = [increments[name] for name in sorted(increments)]
        assignments.append(
            """data_complete=CASE
                 WHEN ? > 0 OR ? > 0 THEN 0 ELSE data_complete END"""
        )
        queue_loss = increments.get("timing_queue_loss_count", 0)
        persistence_loss = increments.get("persistence_loss_count", 0)
        parameters.extend((queue_loss, persistence_loss, session_id, slot))
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            cursor = connection.execute(
                f"""UPDATE benchmark_streams SET {', '.join(assignments)}
                    WHERE session_id=? AND slot=?""",
                parameters,
            )
            if cursor.rowcount != 1:
                raise GroupInvariantError("unknown grouped stream")
            if queue_loss or persistence_loss:
                connection.execute(
                    """UPDATE group_recordings SET data_complete=0,
                              status=CASE WHEN status='recording' THEN 'degraded' ELSE status END
                       WHERE session_id=?""",
                    (session_id,),
                )
                stream = connection.execute(
                    "SELECT benchmark_id FROM benchmark_streams WHERE session_id=? AND slot=?",
                    (session_id, slot),
                ).fetchone()
                self._insert_event(
                    connection, session_id, str(stream["benchmark_id"]),
                    "timing_persistence_loss", elapsed_ns, observed_at,
                    "timing_queue_overflow" if queue_loss else "persistence_write_failure",
                    {
                        "timing_queue_loss_increment": queue_loss,
                        "persistence_loss_increment": persistence_loss,
                    },
                )
            else:
                stream = connection.execute(
                    "SELECT benchmark_id FROM benchmark_streams WHERE session_id=? AND slot=?",
                    (session_id, slot),
                ).fetchone()
                self._insert_event(
                    connection, session_id, str(stream["benchmark_id"]),
                    "stream_counter_update", elapsed_ns, observed_at, None,
                    dict(increments),
                )

    def mark_stream_incomplete(
        self,
        session_id: str,
        slot: str,
        reason_code: str,
        *,
        observed_at: str,
        elapsed_ns: int | None = None,
        details: Mapping[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> None:
        self._validate_reason(reason_code)
        _validate_nonnegative_sqlite_int("elapsed_ns", elapsed_ns)
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            row = connection.execute(
                """SELECT bs.benchmark_id, bs.integrity_flags_json, g.status
                   FROM benchmark_streams bs JOIN group_recordings g ON g.session_id=bs.session_id
                   WHERE bs.session_id=? AND bs.slot=?""",
                (session_id, slot),
            ).fetchone()
            if row is None:
                raise GroupInvariantError("unknown grouped stream")
            flags = _decode_json_object(row["integrity_flags_json"], field_name="stream integrity flags")
            flags[reason_code] = True
            connection.execute(
                """UPDATE benchmark_streams SET data_complete=0, integrity_flags_json=?
                   WHERE session_id=? AND slot=?""",
                (serialize_json(flags), session_id, slot),
            )
            group = connection.execute(
                "SELECT integrity_flags_json FROM group_recordings WHERE session_id=?",
                (session_id,),
            ).fetchone()
            group_flags = _decode_json_object(
                group["integrity_flags_json"], field_name="group integrity flags"
            )
            group_flags[reason_code] = True
            connection.execute(
                """UPDATE group_recordings SET data_complete=0,
                          integrity_flags_json=?,
                          status=CASE WHEN status='recording' THEN 'degraded' ELSE status END
                   WHERE session_id=?""",
                (serialize_json(group_flags), session_id),
            )
            self._insert_event(
                connection, session_id, str(row["benchmark_id"]), "stream_integrity_warning",
                elapsed_ns, observed_at, reason_code, dict(details or {}),
            )

    def record_stream_measurements(
        self,
        session_id: str,
        slot: str,
        samples: Sequence[MeasurementSample],
        *,
        owner_token: str | None = None,
    ) -> int:
        if not samples:
            return 0
        if len(samples) > MAX_GROUP_MEASUREMENT_BATCH:
            raise ValueError(f"group measurement batch exceeds {MAX_GROUP_MEASUREMENT_BATCH} samples")
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            stream = connection.execute(
                """SELECT benchmark_id, stream_status, last_persisted_sample_index,
                          last_persisted_elapsed_ns, integrity_flags_json
                   FROM benchmark_streams WHERE session_id=? AND slot=?""",
                (session_id, slot),
            ).fetchone()
            if stream is None:
                raise GroupInvariantError("unknown grouped stream")
            if stream["stream_status"] != StreamStatus.RECORDING:
                raise GroupInvariantError("timing samples can only be stored for a recording stream")
            benchmark_id = str(stream["benchmark_id"])
            if any(sample.benchmark_id != benchmark_id for sample in samples):
                raise GroupInvariantError("measurement batch contains a sample from another stream")
            arrivals = [
                sample for sample in samples
                if sample.metric == "frame_arrival" and _enum_value(sample.stream_scope) == "capture"
            ]
            last_index = stream["last_persisted_sample_index"]
            last_elapsed = stream["last_persisted_elapsed_ns"]
            expected_elapsed: list[int] = []
            expected_indices: list[int] = []
            for sample in arrivals:
                _validate_nonnegative_sqlite_int("sample_index", sample.sample_index)
                if sample.sample_index is None:
                    raise GroupInvariantError("frame-arrival samples require a stream-local sample index")
                _validate_nonnegative_sqlite_int("elapsed_ns", sample.elapsed_ns)
                if sample.elapsed_ns is None:
                    raise GroupInvariantError("grouped frame-arrival samples require elapsed_ns")
                expected_indices.append(sample.sample_index)
                expected_elapsed.append(sample.elapsed_ns)
            if any(right <= left for left, right in zip(expected_indices, expected_indices[1:])):
                raise GroupInvariantError("frame-arrival sample indexes must increase within a batch")
            if any(right < left for left, right in zip(expected_elapsed, expected_elapsed[1:])):
                raise GroupInvariantError("frame-arrival elapsed times must not move backwards")
            if expected_indices and last_index is not None and expected_indices[0] <= int(last_index):
                raise GroupInvariantError("frame-arrival sample index did not advance")
            if expected_elapsed and last_elapsed is not None and expected_elapsed[0] < int(last_elapsed):
                raise GroupInvariantError("frame-arrival elapsed time did not advance monotonically")
            sequence_gap_count = 0
            previous_index = int(last_index) if last_index is not None else None
            for sample_index in expected_indices:
                if previous_index is not None and sample_index > previous_index + 1:
                    sequence_gap_count += sample_index - previous_index - 1
                previous_index = sample_index
            self.database._insert_measurements(connection, samples)
            if arrivals:
                stream_flags = _decode_json_object(
                    stream["integrity_flags_json"], field_name="stream integrity flags"
                )
                if sequence_gap_count:
                    stream_flags["sample_sequence_gap"] = True
                connection.execute(
                    """UPDATE benchmark_streams SET
                              first_host_arrival_elapsed_ns=COALESCE(first_host_arrival_elapsed_ns, ?),
                              last_host_arrival_elapsed_ns=?,
                              first_persisted_elapsed_ns=COALESCE(first_persisted_elapsed_ns, ?),
                              last_persisted_elapsed_ns=?,
                              last_persisted_sample_index=?,
                              sequence_gap_count=sequence_gap_count+?,
                              data_complete=CASE WHEN ?>0 THEN 0 ELSE data_complete END,
                              integrity_flags_json=?
                       WHERE session_id=? AND slot=?""",
                    (
                        expected_elapsed[0], expected_elapsed[-1],
                        expected_elapsed[0], expected_elapsed[-1], expected_indices[-1],
                        sequence_gap_count, sequence_gap_count, serialize_json(stream_flags),
                        session_id, slot,
                    ),
                )
                if sequence_gap_count:
                    group_row = connection.execute(
                        "SELECT integrity_flags_json FROM group_recordings WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                    group_flags = _decode_json_object(
                        group_row["integrity_flags_json"], field_name="group integrity flags"
                    )
                    group_flags["sample_sequence_gap"] = True
                    connection.execute(
                        """UPDATE group_recordings SET data_complete=0,
                                  integrity_flags_json=?,
                                  status=CASE WHEN status='recording' THEN 'degraded' ELSE status END
                           WHERE session_id=?""",
                        (serialize_json(group_flags), session_id),
                    )
                    self._insert_event(
                        connection, session_id, benchmark_id, "measurement_sequence_gap",
                        expected_elapsed[-1], arrivals[-1].observed_at or utc_now_iso(),
                        "sample_sequence_gap",
                        {
                            "missing_sample_index_count": sequence_gap_count,
                            "first_persisted_index": expected_indices[0],
                            "last_persisted_index": expected_indices[-1],
                        },
                    )
        return len(samples)

    def note_host_arrival_bounds(
        self,
        session_id: str,
        slot: str,
        *,
        first_elapsed_ns: int,
        last_elapsed_ns: int,
        owner_token: str | None = None,
    ) -> None:
        """Persist host-observed bounds independently from durable sample rows.

        A future coordinator can use this for frames whose timing event was
        rejected by a bounded persistence queue. These markers do not recreate
        samples or imply driver/sensor timestamps.
        """
        _validate_nonnegative_sqlite_int("first_elapsed_ns", first_elapsed_ns)
        _validate_nonnegative_sqlite_int("last_elapsed_ns", last_elapsed_ns)
        if first_elapsed_ns is None or last_elapsed_ns is None or last_elapsed_ns < first_elapsed_ns:
            raise ValueError("host arrival bounds must be ordered nonnegative integers")
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            row = connection.execute(
                """SELECT first_host_arrival_elapsed_ns, last_host_arrival_elapsed_ns
                   FROM benchmark_streams WHERE session_id=? AND slot=?""",
                (session_id, slot),
            ).fetchone()
            if row is None:
                raise GroupInvariantError("unknown grouped stream")
            if row["first_host_arrival_elapsed_ns"] is not None:
                first_elapsed_ns = min(first_elapsed_ns, int(row["first_host_arrival_elapsed_ns"]))
            if row["last_host_arrival_elapsed_ns"] is not None:
                last_elapsed_ns = max(last_elapsed_ns, int(row["last_host_arrival_elapsed_ns"]))
            connection.execute(
                """UPDATE benchmark_streams
                   SET first_host_arrival_elapsed_ns=?, last_host_arrival_elapsed_ns=?
                   WHERE session_id=? AND slot=?""",
                (first_elapsed_ns, last_elapsed_ns, session_id, slot),
            )

    def finalize_group(
        self,
        session_id: str,
        status: GroupStatus | str,
        *,
        ended_at: str,
        duration_by_slot: Mapping[str, float],
        termination_reason: str,
        owner_token: str | None = None,
    ) -> GroupedExperimentRecord:
        target = GroupStatus(_enum_value(status))
        if target not in {
            GroupStatus.COMPLETED, GroupStatus.PARTIAL, GroupStatus.INTERRUPTED, GroupStatus.FAILED
        }:
            raise ValueError("final status must be completed, partial, interrupted, or failed")
        if set(duration_by_slot) != {"A", "B"}:
            raise GroupInvariantError("finalization requires a duration for slots A and B")
        for value in duration_by_slot.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError("stream durations must be finite nonnegative numbers")
        self._validate_reason(termination_reason)
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            parent = connection.execute(
                """SELECT status, data_complete, integrity_flags_json
                   FROM group_recordings WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            if parent is None:
                raise GroupInvariantError("unknown grouped experiment")
            streams = connection.execute(
                    """SELECT bs.*, b.status AS benchmark_status
                   FROM benchmark_streams bs
                   JOIN benchmark_runs b ON b.id=bs.benchmark_id AND b.session_id=bs.session_id
                   WHERE bs.session_id=? ORDER BY bs.slot""",
                (session_id,),
            ).fetchall()
            if len(streams) != 2 or [row["slot"] for row in streams] != ["A", "B"]:
                raise GroupInvariantError("finalization requires exactly slots A and B")
            if any(row["stream_status"] in {"pending", "recording"} for row in streams):
                raise GroupInvariantError("all child streams must be terminal before finalization")
            if target == GroupStatus.COMPLETED:
                target_duration = connection.execute(
                    "SELECT duration_target_seconds FROM group_recordings WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                if target_duration is None:
                    raise GroupInvariantError("completed status requires a requested duration")
                for row in streams:
                    lifecycle = connection.execute(
                        """SELECT event_type, elapsed_ns FROM experiment_events
                           WHERE session_id=? AND benchmark_id=?
                             AND event_type IN ('stream_recording','stream_completed')
                           ORDER BY event_id""",
                        (session_id, row["benchmark_id"]),
                    ).fetchall()
                    starts = [event["elapsed_ns"] for event in lifecycle
                              if event["event_type"] == "stream_recording"]
                    ends = [event["elapsed_ns"] for event in lifecycle
                            if event["event_type"] == "stream_completed"]
                    if len(starts) != 1 or len(ends) != 1 or starts[0] is None or ends[0] is None:
                        raise GroupInvariantError(
                            "completed status requires monotonic start and completion events for both streams"
                        )
                    if int(ends[0]) < int(starts[0]):
                        raise GroupInvariantError("stream lifecycle elapsed time moved backwards")
                    measured_duration = (int(ends[0]) - int(starts[0])) / 1_000_000_000
                    reported_duration = float(duration_by_slot[str(row["slot"])])
                    if not math.isclose(
                        measured_duration, reported_duration, rel_tol=1e-9, abs_tol=1e-6
                    ):
                        raise GroupInvariantError(
                            "completed stream duration does not match its monotonic lifecycle events"
                        )
                    if measured_duration < float(target_duration):
                        raise GroupInvariantError(
                            "completed status requires both streams to reach the requested duration"
                        )
            summaries: dict[str, dict[str, object]] = {}
            for row in streams:
                summary = self.database._summary_values(
                    connection, str(row["benchmark_id"]), float(duration_by_slot[str(row["slot"])]),
                    termination_reason,
                )
                summaries[str(row["slot"])] = summary
                self.database._save_summary(connection, str(row["benchmark_id"]), summary)
            if target == GroupStatus.COMPLETED:
                if GroupStatus(str(parent["status"])) == GroupStatus.DEGRADED:
                    raise GroupInvariantError("a degraded group cannot be finalized as completed")
                if any(
                    row["stream_status"] != StreamStatus.COMPLETED
                    or row["benchmark_status"] != "completed"
                    or not bool(row["data_complete"])
                    or int(row["timing_queue_loss_count"]) != 0
                    or int(row["persistence_loss_count"]) != 0
                    or int(row["sequence_gap_count"]) != 0
                    or bool(_decode_json_object(
                        row["integrity_flags_json"], field_name="stream integrity flags"
                    ))
                    or int(summaries[str(row["slot"])]["frame_count"]) == 0
                    for row in streams
                ):
                    raise GroupInvariantError(
                        "completed group requires two useful, completed, loss-free, complete streams"
                    )
                if not bool(parent["data_complete"]) or bool(_decode_json_object(
                    parent["integrity_flags_json"], field_name="group integrity flags"
                )):
                    raise GroupInvariantError("completed group has an incomplete integrity state")
            if target == GroupStatus.PARTIAL and not any(
                int(summaries[str(row["slot"])]["frame_count"]) > 0
                or int(summaries[str(row["slot"])]["read_failures"]) > 0
                for row in streams
            ):
                raise GroupInvariantError("partial group requires at least one recorded observation")
            self._set_group_status_in_transaction(
                connection, session_id, target, ended_at,
                reason_code=termination_reason,
                owner_token=owner_token,
            )
            connection.execute(
                """UPDATE group_recordings SET finalized_at=?
                   WHERE session_id=?""",
                (ended_at, session_id),
            )
            connection.execute(
                "UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, session_id)
            )
            if owner_token is not None:
                cursor = connection.execute(
                    "DELETE FROM recording_leases WHERE lease_key=1 AND session_id=? AND owner_token=?",
                    (session_id, owner_token),
                )
                if cursor.rowcount != 1:
                    raise RecordingLeaseOwnershipError("cannot finalize after losing the recording lease")
        result = self.get_group(session_id)
        if result is None:
            raise GroupInvariantError("finalized group could not be read back")
        return result

    def acquire_recording_lease(
        self,
        session_id: str,
        *,
        owner_pid: int,
        owner_process_start_ticks: str | None,
        acquired_at: str,
        owner_token: str | None = None,
    ) -> RecordingLease:
        _validate_nonnegative_sqlite_int("owner_pid", owner_pid)
        if not owner_pid:
            raise ValueError("owner_pid must be positive")
        token = owner_token or str(uuid4())
        with self.database.connect() as connection:
            self._require_v3(connection)
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute(
                "SELECT status FROM group_recordings WHERE session_id=?", (session_id,)
            ).fetchone()
            if group is None:
                raise GroupInvariantError("unknown grouped experiment")
            if group["status"] not in {"pending", "recording", "degraded"}:
                raise GroupInvariantError("recording lease requires a nonterminal group")
            cursor = connection.execute(
                """INSERT INTO recording_leases
                   (lease_key, session_id, owner_token, owner_pid,
                    owner_process_start_ticks, acquired_at)
                   VALUES (1, ?, ?, ?, ?, ?)
                   ON CONFLICT(lease_key) DO NOTHING""",
                (session_id, token, owner_pid, owner_process_start_ticks, acquired_at),
            )
            if cursor.rowcount != 1:
                raise RecordingLeaseBusyError("another process owns the experiment writer lease")
        return RecordingLease(token, session_id, owner_pid, owner_process_start_ticks, acquired_at)

    def release_recording_lease(self, owner_token: str) -> None:
        with self.database.connect() as connection:
            self._require_v3(connection)
            cursor = connection.execute(
                "DELETE FROM recording_leases WHERE lease_key=1 AND owner_token=?",
                (owner_token,),
            )
            if cursor.rowcount != 1:
                raise RecordingLeaseOwnershipError("caller does not own the experiment writer lease")

    def get_recording_lease(self) -> RecordingLease | None:
        with self.database.connect_readonly() as connection:
            self._require_v3(connection)
            row = connection.execute(
                """SELECT owner_token, session_id, owner_pid,
                          owner_process_start_ticks, acquired_at
                   FROM recording_leases WHERE lease_key=1"""
            ).fetchone()
            if row is None:
                return None
            return RecordingLease(
                owner_token=str(row["owner_token"]),
                session_id=str(row["session_id"]),
                owner_pid=int(row["owner_pid"]),
                owner_process_start_ticks=row["owner_process_start_ticks"],
                acquired_at=str(row["acquired_at"]),
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        session_id: str,
        benchmark_id: str | None,
        event_type: str,
        elapsed_ns: int | None,
        observed_at: str,
        reason_code: str | None,
        details: Mapping[str, Any],
    ) -> None:
        _required_text("event_type", event_type)
        GroupedExperimentRepository._validate_reason(event_type)
        _validate_nonnegative_sqlite_int("elapsed_ns", elapsed_ns)
        GroupedExperimentRepository._validate_reason(reason_code)
        details_json = _json_object(details, field_name="event details")
        connection.execute(
            """INSERT INTO experiment_events
               (session_id, benchmark_id, event_type, elapsed_ns, observed_at, reason_code, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, benchmark_id, event_type, elapsed_ns, observed_at, reason_code, details_json),
        )

    def add_event(
        self,
        session_id: str,
        event_type: str,
        *,
        observed_at: str,
        elapsed_ns: int | None = None,
        benchmark_id: str | None = None,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> int:
        with self.database.connect() as connection:
            self._require_v3(connection)
            self._require_lease_owner(connection, session_id, owner_token)
            self._insert_event(
                connection, session_id, benchmark_id, event_type, elapsed_ns,
                observed_at, reason_code, details or {},
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    @staticmethod
    def _validate_reason(reason_code: str | None) -> None:
        if reason_code is not None and not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("reason_code must be a short lowercase structured code")
