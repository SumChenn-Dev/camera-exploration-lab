"""SQLite schema and small repository operations for the application core."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    MeasurementSample,
    SessionRecord,
    SettingSnapshot,
    utc_now_iso,
)
from .recording_ownership import (
    RecordingOwnership,
    RecordingOwnershipBusyError,
    RecordingOwnershipError,
    acquire_recording_ownership as _acquire_recording_ownership,
    inspect_recording_recovery as _inspect_recording_recovery,
    require_recording_ownership as _require_recording_ownership,
)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    stable_identifier TEXT NOT NULL UNIQUE,
    backend TEXT NOT NULL,
    device_path TEXT,
    manufacturer TEXT,
    model TEXT,
    serial_number TEXT,
    vendor_id TEXT,
    product_id TEXT,
    identity_source TEXT NOT NULL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id) ON DELETE SET NULL,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    status TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS setting_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    benchmark_id TEXT REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    raw_record_id INTEGER,
    setting_key TEXT NOT NULL,
    requested_value_json TEXT,
    reported_value_json TEXT,
    verified_value_json TEXT,
    verification_status TEXT NOT NULL,
    requested_at TEXT,
    checked_at TEXT,
    verification_method TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS import_sources (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_basename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    row_count INTEGER NOT NULL DEFAULT 0,
    measurement_count INTEGER NOT NULL DEFAULT 0,
    format TEXT NOT NULL DEFAULT 'csv',
    UNIQUE(source_path, sha256)
);

CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES import_sources(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    benchmark_id TEXT REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    timestamp_raw TEXT,
    observed_at TEXT,
    elapsed_seconds REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(source_id, row_number)
);

CREATE TABLE IF NOT EXISTS measurements (
    id TEXT PRIMARY KEY,
    benchmark_id TEXT NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    raw_record_id INTEGER REFERENCES raw_records(id) ON DELETE SET NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    stream_scope TEXT NOT NULL CHECK (stream_scope IN
        ('capture', 'preview', 'processing', 'transport', 'camera_reported', 'unknown')),
    observed_at TEXT,
    elapsed_seconds REAL,
    sample_index INTEGER,
    interval_ns INTEGER,
    method TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_summaries (
    benchmark_id TEXT PRIMARY KEY REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    frame_count INTEGER NOT NULL DEFAULT 0,
    read_failures INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    capture_fps REAL,
    preview_fps REAL,
    processing_fps REAL,
    interval_count INTEGER NOT NULL DEFAULT 0,
    median_interval_ms REAL,
    p95_interval_ms REAL,
    p99_interval_ms REAL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_camera_started
    ON sessions(camera_id, started_at);
CREATE INDEX IF NOT EXISTS idx_benchmarks_session_started
    ON benchmark_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_scope_time
    ON measurements(metric, stream_scope, observed_at);
CREATE INDEX IF NOT EXISTS idx_measurements_benchmark_metric_sample
    ON measurements(benchmark_id, metric, stream_scope, sample_index);
CREATE INDEX IF NOT EXISTS idx_measurements_benchmark_interval
    ON measurements(benchmark_id, metric, interval_ns)
    WHERE interval_ns IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_records_source_row
    ON raw_records(source_id, row_number);
CREATE INDEX IF NOT EXISTS idx_settings_session_key_time
    ON setting_snapshots(session_id, setting_key, checked_at);
"""


def default_database_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "camera-exploration-lab" / "history.sqlite3"


def serialize_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Database:
    """Open and initialize a SQLite database without requiring a server."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        self._memory_uri = None
        self._memory_anchor = None
        if str(self.path) == ":memory:":
            self._memory_uri = f"file:celab-{uuid4().hex}?mode=memory&cache=shared"
            self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_uri is not None:
            connection = sqlite3.connect(self._memory_uri, uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def connect_readonly(self) -> Iterator[sqlite3.Connection]:
        """Open a query-only connection without creating or initializing a database."""
        if self._memory_uri is not None:
            connection = sqlite3.connect(self._memory_uri, uri=True)
        else:
            if not self.path.is_file():
                raise FileNotFoundError(f"database does not exist: {self.path}")
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def acquire_recording_ownership(
        self,
        *,
        owner_kind: str,
        owner_token: str | None = None,
        check_recovery: bool = True,
    ) -> RecordingOwnership:
        """Acquire the common cross-process lease for an experiment writer."""
        return _acquire_recording_ownership(
            self, owner_kind=owner_kind, owner_token=owner_token,
            check_recovery=check_recovery,
        )

    def require_recording_ownership(self, owner_token: str) -> RecordingOwnership:
        return _require_recording_ownership(self, owner_token)

    def inspect_recording_recovery(self):
        """Inspect recording states without changing the database."""
        return _inspect_recording_recovery(self)

    def close(self) -> None:
        """Release the keeper connection for an in-memory database."""
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    def initialize(self) -> None:
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
                )
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def upsert_camera(self, camera: CameraIdentity, *, seen_at: str | None = None) -> str:
        camera_id = camera.stable_identifier
        backend = camera.backend.value if isinstance(camera.backend, BackendKind) else str(camera.backend)
        with self.connect() as connection:
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
                       last_seen_at=COALESCE(excluded.last_seen_at, cameras.last_seen_at),
                       metadata_json=excluded.metadata_json""",
                (
                    camera_id,
                    camera.stable_identifier,
                    backend,
                    camera.device_path,
                    camera.manufacturer,
                    camera.model,
                    camera.serial_number,
                    camera.vendor_id,
                    camera.product_id,
                    camera.identity_source,
                    seen_at,
                    seen_at,
                    serialize_json(camera.metadata),
                ),
            )
        return camera_id

    def create_session(self, session: SessionRecord) -> str:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sessions
                   (id, camera_id, label, source, started_at, ended_at, created_at, notes, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.camera_id,
                    session.label,
                    session.source,
                    session.started_at,
                    session.ended_at,
                    session.created_at,
                    session.notes,
                    serialize_json(session.metadata),
                ),
            )
        return session.id

    def create_benchmark(self, benchmark: BenchmarkRecord) -> str:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO benchmark_runs
                   (id, session_id, label, started_at, ended_at, status, configuration_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    benchmark.id,
                    benchmark.session_id,
                    benchmark.label,
                    benchmark.started_at,
                    benchmark.ended_at,
                    benchmark.status,
                    serialize_json(benchmark.configuration),
                ),
            )
        return benchmark.id

    def record_setting(self, setting: SettingSnapshot, *, raw: dict | None = None) -> str:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO setting_snapshots (
                       id, session_id, benchmark_id, raw_record_id, setting_key,
                       requested_value_json, reported_value_json, verified_value_json,
                       verification_status, requested_at, checked_at, verification_method, raw_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    setting.id,
                    setting.session_id,
                    setting.benchmark_id,
                    setting.raw_record_id,
                    setting.setting_key,
                    serialize_json(setting.requested_value) if setting.requested_value is not None else None,
                    serialize_json(setting.reported_value) if setting.reported_value is not None else None,
                    serialize_json(setting.verified_value) if setting.verified_value is not None else None,
                    setting.verification_status,
                    setting.requested_at,
                    setting.checked_at,
                    setting.verification_method,
                    serialize_json(raw or {}),
                ),
            )
        return setting.id

    def record_measurement(self, sample: MeasurementSample) -> str:
        self.record_measurements_batch([sample])
        return sample.id

    def begin_live_experiment(
        self,
        camera: CameraIdentity,
        session: SessionRecord,
        benchmark: BenchmarkRecord,
        settings: list[SettingSnapshot],
        *,
        seen_at: str,
        ownership: RecordingOwnership | None = None,
    ) -> tuple[str, str]:
        """Create an experiment and its initial settings in one transaction."""
        if session.source == "live_experiment" and ownership is None:
            raise RecordingOwnershipError("live experiment creation requires database recording ownership")
        if session.source == "live_experiment" and (
            (session.metadata or {}).get("recording_owner_token") != ownership.owner_token
        ):
            raise RecordingOwnershipError("live experiment owner identity does not match the database lease")
        camera_id = camera.stable_identifier
        backend = camera.backend.value if isinstance(camera.backend, BackendKind) else str(camera.backend)
        with self.connect() as connection:
            if ownership is not None:
                ownership.assert_active(self)
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
            connection.execute(
                """INSERT INTO sessions
                   (id, camera_id, label, source, started_at, ended_at, created_at, notes, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id, camera_id, session.label, "live_experiment",
                    session.started_at, session.ended_at, session.created_at,
                    session.notes, serialize_json(session.metadata),
                ),
            )
            connection.execute(
                """INSERT INTO benchmark_runs
                   (id, session_id, label, started_at, ended_at, status, configuration_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    benchmark.id, session.id, benchmark.label, benchmark.started_at,
                    benchmark.ended_at, "running", serialize_json(benchmark.configuration),
                ),
            )
            for setting in settings:
                connection.execute(
                    """INSERT INTO setting_snapshots (
                           id, session_id, benchmark_id, raw_record_id, setting_key,
                           requested_value_json, reported_value_json, verified_value_json,
                           verification_status, requested_at, checked_at, verification_method, raw_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        setting.id, session.id, benchmark.id, setting.raw_record_id,
                        setting.setting_key,
                        serialize_json(setting.requested_value) if setting.requested_value is not None else None,
                        serialize_json(setting.reported_value) if setting.reported_value is not None else None,
                        serialize_json(setting.verified_value) if setting.verified_value is not None else None,
                        setting.verification_status, setting.requested_at, setting.checked_at,
                        setting.verification_method, "{}",
                    ),
                )
        return session.id, benchmark.id

    @staticmethod
    def _summary_values(
        connection: sqlite3.Connection,
        benchmark_id: str,
        duration_seconds: float,
        termination_reason: str,
    ) -> dict[str, object]:
        frame_stats = connection.execute(
            """SELECT COUNT(*) AS frame_count, MIN(elapsed_seconds) AS first_elapsed,
                      MAX(elapsed_seconds) AS last_elapsed
               FROM measurements
               WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'""",
            (benchmark_id,),
        ).fetchone()
        frame_count = int(frame_stats["frame_count"] or 0)
        elapsed_span = (frame_stats["last_elapsed"] or 0) - (frame_stats["first_elapsed"] or 0)
        capture_fps = (frame_count - 1) / elapsed_span if frame_count > 1 and elapsed_span > 0 else None
        failure_row = connection.execute(
            """SELECT COUNT(*) FROM measurements
               WHERE benchmark_id=? AND metric='read_failure' AND stream_scope='capture'""",
            (benchmark_id,),
        ).fetchone()
        failures = int(failure_row[0] or 0)
        preview_row = connection.execute(
            """SELECT AVG(value) FROM measurements
               WHERE benchmark_id=? AND metric='preview_fps' AND stream_scope='preview'""",
            (benchmark_id,),
        ).fetchone()
        preview_fps = float(preview_row[0]) if preview_row[0] is not None else None

        interval_row = connection.execute(
            """SELECT COUNT(*) FROM measurements
               WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
                 AND interval_ns IS NOT NULL""",
            (benchmark_id,),
        ).fetchone()
        interval_count = int(interval_row[0] or 0)

        def interval_at(offset: int) -> int | None:
            row = connection.execute(
                """SELECT interval_ns FROM measurements
                   WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
                     AND interval_ns IS NOT NULL
                   ORDER BY interval_ns LIMIT 1 OFFSET ?""",
                (benchmark_id, offset),
            ).fetchone()
            return int(row[0]) if row else None

        median_ns: float | None = None
        p95_ns: int | None = None
        p99_ns: int | None = None
        if interval_count:
            center = (interval_count - 1) / 2
            lower = interval_at(math.floor(center))
            upper = interval_at(math.ceil(center))
            if lower is not None and upper is not None:
                median_ns = (lower + upper) / 2
            p95_ns = interval_at(max(0, math.ceil(interval_count * 0.95) - 1))
            p99_ns = interval_at(max(0, math.ceil(interval_count * 0.99) - 1))

        return {
            "frame_count": frame_count,
            "read_failures": failures,
            "duration_seconds": max(0.0, float(duration_seconds)),
            "capture_fps": capture_fps,
            "preview_fps": preview_fps,
            "processing_fps": None,
            "interval_count": interval_count,
            "median_interval_ms": median_ns / 1_000_000 if median_ns is not None else None,
            "p95_interval_ms": p95_ns / 1_000_000 if p95_ns is not None else None,
            "p99_interval_ms": p99_ns / 1_000_000 if p99_ns is not None else None,
            "termination_reason": termination_reason,
        }

    @staticmethod
    def _save_summary(
        connection: sqlite3.Connection,
        benchmark_id: str,
        values: dict[str, object],
    ) -> None:
        connection.execute(
            """INSERT INTO benchmark_summaries (
                   benchmark_id, frame_count, read_failures, duration_seconds,
                   capture_fps, preview_fps, processing_fps, interval_count,
                   median_interval_ms, p95_interval_ms, p99_interval_ms, summary_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(benchmark_id) DO UPDATE SET
                   frame_count=excluded.frame_count,
                   read_failures=excluded.read_failures,
                   duration_seconds=excluded.duration_seconds,
                   capture_fps=excluded.capture_fps,
                   preview_fps=excluded.preview_fps,
                   processing_fps=excluded.processing_fps,
                   interval_count=excluded.interval_count,
                   median_interval_ms=excluded.median_interval_ms,
                   p95_interval_ms=excluded.p95_interval_ms,
                   p99_interval_ms=excluded.p99_interval_ms,
                   summary_json=excluded.summary_json""",
            (
                benchmark_id, values["frame_count"], values["read_failures"], values["duration_seconds"],
                values["capture_fps"], values["preview_fps"], values["processing_fps"],
                values["interval_count"], values["median_interval_ms"],
                values["p95_interval_ms"], values["p99_interval_ms"], serialize_json(values),
            ),
        )

    def record_measurements_batch(
        self,
        samples: list[MeasurementSample],
        *,
        ownership: RecordingOwnership | None = None,
    ) -> int:
        """Insert one bounded batch of timing data in a single SQLite transaction."""
        return self._record_measurements_batch(samples, ownership=ownership)

    def _record_measurements_batch(
        self,
        samples: list[MeasurementSample],
        *,
        ownership: RecordingOwnership | None,
    ) -> int:
        if not samples:
            return 0
        with self.connect() as connection:
            if ownership is not None:
                ownership.assert_active(self)
            benchmark_ids = tuple(dict.fromkeys(sample.benchmark_id for sample in samples))
            placeholders = ",".join("?" for _ in benchmark_ids)
            active_rows = connection.execute(
                f"""SELECT s.metadata_json FROM benchmark_runs b JOIN sessions s ON s.id=b.session_id
                    WHERE b.id IN ({placeholders}) AND s.source='live_experiment'
                      AND b.status='running'""",
                benchmark_ids,
            ).fetchall()
            if active_rows and ownership is None:
                raise RecordingOwnershipError("active live experiment samples require database ownership")
            for row in active_rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                if metadata.get("recording_owner_token") != ownership.owner_token:
                    raise RecordingOwnershipError("active live experiment owner identity does not match")
            return self._insert_measurements(connection, samples)

    @staticmethod
    def _insert_measurements(
        connection: sqlite3.Connection,
        samples: list[MeasurementSample] | tuple[MeasurementSample, ...],
    ) -> int:
        """Insert samples using the columns supported by this DB transaction."""
        if not samples:
            return 0
        available_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(measurements)")
        }
        provenance_columns = {
            "elapsed_ns", "source_timestamp_ns", "source_sequence", "source_clock"
        }
        present_provenance = provenance_columns & available_columns
        if present_provenance and present_provenance != provenance_columns:
            raise RuntimeError("measurement timing-provenance schema is incomplete")
        has_v3_provenance = present_provenance == provenance_columns
        values = []
        for sample in samples:
            scope = sample.stream_scope.value if hasattr(sample.stream_scope, "value") else str(sample.stream_scope)
            if not has_v3_provenance and any((
                sample.elapsed_ns is not None,
                sample.source_timestamp_ns is not None,
                sample.source_sequence is not None,
                sample.source_clock is not None,
            )):
                raise RuntimeError("timing provenance fields require the grouped schema")
            elapsed_seconds = sample.elapsed_seconds
            if elapsed_seconds is None and sample.elapsed_ns is not None:
                elapsed_seconds = sample.elapsed_ns / 1_000_000_000
            row = (
                sample.id, sample.benchmark_id, sample.raw_record_id, sample.metric,
                sample.value, sample.unit, scope, sample.observed_at,
                elapsed_seconds, sample.sample_index, sample.interval_ns, sample.method,
            )
            if has_v3_provenance:
                row += (
                    sample.elapsed_ns, sample.source_timestamp_ns,
                    sample.source_sequence, sample.source_clock,
                )
            values.append(row)
        if has_v3_provenance:
            connection.executemany(
                """INSERT INTO measurements (
                       id, benchmark_id, raw_record_id, metric, value, unit, stream_scope,
                       observed_at, elapsed_seconds, sample_index, interval_ns, method,
                       elapsed_ns, source_timestamp_ns, source_sequence, source_clock
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        else:
            connection.executemany(
                """INSERT INTO measurements (
                       id, benchmark_id, raw_record_id, metric, value, unit, stream_scope,
                       observed_at, elapsed_seconds, sample_index, interval_ns, method
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        return len(samples)

    def finish_live_experiment(
        self,
        benchmark_id: str,
        *,
        ended_at: str,
        duration_seconds: float,
        status: str,
        termination_reason: str,
        ownership: RecordingOwnership | None = None,
    ) -> dict[str, object]:
        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError(f"invalid live experiment status: {status}")
        with self.connect() as connection:
            if ownership is not None:
                ownership.assert_active(self)
            benchmark = connection.execute(
                "SELECT session_id FROM benchmark_runs WHERE id=?", (benchmark_id,)
            ).fetchone()
            if benchmark is None:
                raise ValueError(f"unknown benchmark: {benchmark_id}")
            session_owner = connection.execute(
                "SELECT source, metadata_json FROM sessions WHERE id=?", (benchmark["session_id"],)
            ).fetchone()
            current_status = connection.execute(
                "SELECT status FROM benchmark_runs WHERE id=?", (benchmark_id,)
            ).fetchone()["status"]
            if (
                session_owner is not None and session_owner["source"] == "live_experiment"
                and current_status == "running" and ownership is None
            ):
                raise RecordingOwnershipError("live experiment finalization requires database ownership")
            if (
                session_owner is not None and session_owner["source"] == "live_experiment"
                and current_status == "running"
            ):
                metadata = json.loads(session_owner["metadata_json"] or "{}")
                if metadata.get("recording_owner_token") != ownership.owner_token:
                    raise RecordingOwnershipError("live experiment owner identity does not match")
            values = self._summary_values(connection, benchmark_id, duration_seconds, termination_reason)
            connection.execute(
                "UPDATE benchmark_runs SET ended_at=?, status=? WHERE id=?",
                (ended_at, status, benchmark_id),
            )
            connection.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, benchmark["session_id"]))
            self._save_summary(connection, benchmark_id, values)
        return values

    def recover_interrupted_experiments(self, *, ended_at: str | None = None) -> int:
        """Mark dead-process v2 runs interrupted under the common database lock.

        If another CEL recorder owns the database, recovery is skipped. The
        caller can retry after that recorder finalizes.
        """
        if self._memory_uri is not None:
            return 0
        try:
            ownership = self.acquire_recording_ownership(
                owner_kind="legacy_startup_recovery", check_recovery=False
            )
        except RecordingOwnershipBusyError:
            return 0
        try:
            ownership.assert_active(self)
            with self.connect_readonly() as connection:
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                    return 0
            return self._recover_interrupted_experiments_locked(
                ownership=ownership, ended_at=ended_at
            )
        finally:
            ownership.release(ownership.owner_token)

    def _recover_interrupted_experiments_locked(
        self,
        *,
        ownership: RecordingOwnership,
        ended_at: str | None = None,
    ) -> int:
        ended_at = ended_at or utc_now_iso()
        recovered = 0
        with self.connect() as connection:
            ownership.assert_active(self)
            rows = connection.execute(
                """SELECT b.id AS benchmark_id, b.session_id, s.started_at, s.metadata_json
                   FROM benchmark_runs b JOIN sessions s ON s.id=b.session_id
                   WHERE s.source='live_experiment' AND b.status='running'"""
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                if _owner_process_is_alive(metadata):
                    continue
                last_elapsed = connection.execute(
                    "SELECT MAX(elapsed_seconds) FROM measurements WHERE benchmark_id=?",
                    (row["benchmark_id"],),
                ).fetchone()[0]
                # The exact crash instant is unknowable after restart. Report
                # only through the last committed measurement as a lower bound.
                duration = max(0.0, float(last_elapsed or 0.0))
                values = self._summary_values(connection, row["benchmark_id"], duration, "application_shutdown_recovery")
                values["duration_source"] = "last_committed_sample"
                connection.execute(
                    "UPDATE benchmark_runs SET ended_at=?, status='interrupted' WHERE id=?",
                    (ended_at, row["benchmark_id"]),
                )
                connection.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, row["session_id"]))
                self._save_summary(connection, row["benchmark_id"], values)
                recovered += 1
        return recovered

    def history_entries(self) -> list[dict[str, object]]:
        """List history through a query-only connection; filtering never writes data."""
        with self.connect_readonly() as connection:
            rows = connection.execute(
                """SELECT s.id AS session_id, b.id AS benchmark_id, s.label AS session_label,
                          s.source, s.started_at AS session_started_at, s.ended_at AS session_ended_at,
                          s.notes, s.metadata_json AS session_metadata_json,
                          b.label AS benchmark_label, b.started_at AS benchmark_started_at,
                          b.ended_at AS benchmark_ended_at, b.status, b.configuration_json,
                          c.id AS camera_id, c.stable_identifier, c.identity_source,
                          c.vendor_id, c.product_id, c.model, c.manufacturer,
                          i.source_basename,
                          (SELECT GROUP_CONCAT(
                               setting_key || ' ' || COALESCE(requested_value_json, '') || ' ' ||
                               COALESCE(reported_value_json, ''), ' ')
                           FROM setting_snapshots ss WHERE ss.benchmark_id=b.id) AS setting_search_text,
                          z.frame_count, z.read_failures, z.duration_seconds,
                          COALESCE(z.capture_fps, (SELECT AVG(m.value) FROM measurements m
                              WHERE m.benchmark_id=b.id AND m.metric='capture_fps' AND m.stream_scope='capture')) AS capture_fps,
                          COALESCE(z.preview_fps, (SELECT AVG(m.value) FROM measurements m
                              WHERE m.benchmark_id=b.id AND m.metric='preview_fps' AND m.stream_scope='preview')) AS preview_fps,
                          z.processing_fps, z.interval_count,
                          z.median_interval_ms, z.p95_interval_ms, z.p99_interval_ms, z.summary_json
                   FROM sessions s
                   LEFT JOIN benchmark_runs b ON b.session_id=s.id
                   LEFT JOIN cameras c ON c.id=s.camera_id
                   LEFT JOIN import_sources i ON i.session_id=s.id
                   LEFT JOIN benchmark_summaries z ON z.benchmark_id=b.id
                   ORDER BY COALESCE(b.started_at, s.started_at, s.created_at) DESC, b.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def history_detail(self, benchmark_id: str | None, session_id: str) -> dict[str, object]:
        with self.connect_readonly() as connection:
            if benchmark_id:
                entry = connection.execute(
                    """SELECT s.*, b.id AS benchmark_id, b.label AS benchmark_label,
                              b.started_at AS benchmark_started_at, b.ended_at AS benchmark_ended_at,
                              b.status, b.configuration_json, c.stable_identifier, c.identity_source,
                              c.vendor_id, c.product_id, c.model, c.manufacturer,
                              i.source_basename, i.row_count AS imported_row_count,
                              i.measurement_count AS imported_measurement_count,
                              z.frame_count, z.read_failures, z.duration_seconds, z.capture_fps,
                              z.preview_fps, z.processing_fps, z.interval_count,
                              z.median_interval_ms, z.p95_interval_ms, z.p99_interval_ms, z.summary_json
                       FROM sessions s JOIN benchmark_runs b ON b.session_id=s.id
                       LEFT JOIN cameras c ON c.id=s.camera_id
                       LEFT JOIN import_sources i ON i.session_id=s.id
                       LEFT JOIN benchmark_summaries z ON z.benchmark_id=b.id
                       WHERE b.id=? AND s.id=?""",
                    (benchmark_id, session_id),
                ).fetchone()
            else:
                entry = connection.execute(
                    """SELECT s.*, NULL AS benchmark_id, NULL AS benchmark_label,
                              NULL AS benchmark_started_at, NULL AS benchmark_ended_at,
                              NULL AS status, NULL AS configuration_json, c.stable_identifier,
                              c.identity_source, c.vendor_id, c.product_id, c.model, c.manufacturer, i.source_basename,
                              i.row_count AS imported_row_count, i.measurement_count AS imported_measurement_count,
                              NULL AS frame_count, NULL AS read_failures, NULL AS duration_seconds,
                              NULL AS capture_fps, NULL AS preview_fps, NULL AS processing_fps,
                              NULL AS interval_count, NULL AS median_interval_ms, NULL AS p95_interval_ms,
                              NULL AS p99_interval_ms, NULL AS summary_json
                       FROM sessions s LEFT JOIN cameras c ON c.id=s.camera_id
                       LEFT JOIN import_sources i ON i.session_id=s.id WHERE s.id=?""",
                    (session_id,),
                ).fetchone()
            if entry is None:
                raise ValueError(f"unknown history entry: {session_id}/{benchmark_id}")
            settings = []
            metrics = []
            if benchmark_id:
                settings = [dict(row) for row in connection.execute(
                    """SELECT setting_key, requested_value_json, reported_value_json,
                              verified_value_json, verification_status, verification_method,
                              requested_at, checked_at
                       FROM setting_snapshots WHERE benchmark_id=? ORDER BY setting_key""",
                    (benchmark_id,),
                ).fetchall()]
                metrics = [dict(row) for row in connection.execute(
                    """SELECT metric, stream_scope, unit, COUNT(*) AS sample_count,
                              AVG(value) AS average, MIN(value) AS minimum, MAX(value) AS maximum
                       FROM measurements WHERE benchmark_id=? GROUP BY metric, stream_scope, unit
                       ORDER BY metric, stream_scope""",
                    (benchmark_id,),
                ).fetchall()]
                if entry["source"] == "imported":
                    entry = dict(entry)
                    entry["raw_record_count"] = int(connection.execute(
                        "SELECT COUNT(*) FROM raw_records WHERE session_id=?", (session_id,)
                    ).fetchone()[0])
                else:
                    entry = dict(entry)
            else:
                entry = dict(entry)
            return {"entry": dict(entry), "settings": settings, "metrics": metrics}

    def count(self, table: str) -> int:
        allowed = {
            "cameras", "sessions", "benchmark_runs", "benchmark_summaries",
            "measurements", "raw_records", "import_sources", "setting_snapshots",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        with self.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _owner_process_is_alive(metadata: dict[str, object]) -> bool:
    """Protect another still-running GUI process from startup recovery."""
    pid = metadata.get("owner_pid")
    expected_start = metadata.get("owner_process_start_ticks")
    if not isinstance(pid, int) or pid < 1 or not isinstance(expected_start, str):
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # The command field can contain spaces and parentheses; fields after its
        # final ')' start at Linux stat field 3. Process start time is field 22.
        actual_start = stat[stat.rfind(")") + 2 :].split()[19]
        return actual_start == expected_start
    except (OSError, IndexError):
        return False
