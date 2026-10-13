"""Non-destructive CSV history importer with row-level provenance."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Database, serialize_json
from .models import BackendKind, CameraIdentity, ImportResult, new_id, utc_now_iso


@dataclass(frozen=True, slots=True)
class _CameraProfile:
    stable_identifier: str
    backend: BackendKind
    manufacturer: str
    model: str
    vendor_id: str | None
    product_id: str | None


@dataclass(frozen=True, slots=True)
class _SettingEvidence:
    key: str
    requested: Any = None
    reported: Any = None
    verification_status: str = "requested_only"
    verification_method: str | None = None


def _legacy_camera_profile(path: Path) -> _CameraProfile | None:
    parts = {part.casefold() for part in path.parts}
    all_text = " ".join(parts)
    if "ps3eye" in all_text or "playstation-eye" in all_text:
        return _CameraProfile(
            "legacy:sony-playstation-eye:1415:2000",
            BackendKind.V4L2,
            "Sony",
            "PlayStation Eye",
            "1415",
            "2000",
        )
    if "webcam" in all_text or "avermedia" in all_text:
        return _CameraProfile(
            "legacy:avermedia-live-streamer-cam-313:07ca:313a",
            BackendKind.V4L2,
            "AVerMedia",
            "Live Streamer CAM 313",
            "07ca",
            "313a",
        )
    if "canon" in all_text:
        return _CameraProfile(
            "legacy:canon-powershot-sx40-hs:chdkptp",
            BackendKind.CHDKPTP,
            "Canon",
            "PowerShot SX40 HS",
            None,
            None,
        )
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _scalar(value: str | None) -> int | float | str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    number = _number(normalized)
    if number is None:
        return normalized
    if number.is_integer():
        return int(number)
    return number


def _timestamp(row: dict[str | None, str | None]) -> tuple[str | None, str | None]:
    raw = next((row.get(key) for key in ("timestamp", "observed_at", "time") if row.get(key)), None)
    if raw is None:
        return None, None
    value = raw.strip()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return value, None
    return value, parsed.isoformat()


def _filename_timestamp(path: Path) -> str | None:
    """Find a local wall-clock run stamp in a source filename or parent path."""
    pattern = re.compile(r"(?<!\d)(\d{8})_(\d{6})(?!\d)")
    for component in reversed(path.parts):
        match = pattern.search(component)
        if not match:
            continue
        try:
            return datetime.strptime("_".join(match.groups()), "%Y%m%d_%H%M%S").isoformat()
        except ValueError:
            return None
    return None


def _context_scope(profile: _CameraProfile | None) -> str:
    if profile is None:
        return "unknown"
    if profile.backend is BackendKind.CHDKPTP:
        return "preview"
    return "capture"


def _metric_for(
    field: str,
    value: str | None,
    *,
    fps_scope: str,
) -> tuple[str, float, str, str, str] | None:
    """Return metric, value, unit, scope, method for recognized legacy fields."""
    number = _number(value)
    if number is None:
        return None
    method = f"legacy_csv:{field}"

    if field in {"measured_fps", "actual_fps"}:
        metric = {"capture": "capture_fps", "preview": "preview_fps"}.get(fps_scope, "measured_fps")
        semantics = {
            "capture": "; host-side successful frame-return throughput",
            "preview": "; CHDKPTP live-view throughput inferred from source location",
            "unknown": "; stream semantics not inferred",
        }.get(fps_scope, "")
        return metric, number, "frames/s", fps_scope, method + semantics
    if field == "reported_fps":
        return "camera_reported_fps", number, "frames/s", "camera_reported", method
    if field == "read_ms":
        return "capture_read_duration_ms", number, "ms", "capture", method + "; host read-call duration, not sensor latency"
    if field in {"mean_read_ms", "avg_read_ms"}:
        return "capture_read_duration_mean_ms", number, "ms", "capture", method + "; mean host read-call duration, not sensor latency"
    if field == "interval_ms":
        return "capture_frame_interval_ms", number, "ms", "capture", method
    if field == "median_interval_ms":
        return "frame_interval_median_ms", number, "ms", fps_scope, method
    if field == "p95_interval_ms":
        return "frame_interval_p95_ms", number, "ms", fps_scope, method
    if field == "frame_ms":
        return "legacy_frame_duration_ms", number, "ms", "unknown", method
    if field in {"usb_transfer_ms", "usb_ms"}:
        return "usb_transfer_duration_ms", number, "ms", "transport", method
    if field in {"throughput_kbps", "usb_speed_kbps"}:
        metric = "transfer_throughput_kbps" if field == "throughput_kbps" else "usb_link_rate_kbps"
        return metric, number, "kbit/s", "transport", method
    if field in {"frames", "frame_count"}:
        return "frame_count", number, "frames", fps_scope, method
    if field in {"failures", "read_failures"}:
        return "read_failure_count", number, "failures", fps_scope, method
    if field == "dropped_frames":
        return "dropped_frame_count", number, "frames", fps_scope, method
    if field == "dropped_total":
        return "dropped_frame_counter", number, "frames", fps_scope, method
    if field == "long_gaps":
        return "long_gap_count", number, "gaps", fps_scope, method
    if field == "action_ms":
        return "control_action_duration_ms", number, "ms", "processing", method
    if field == "mean_brightness":
        return "image_mean_brightness", number, "level", "processing", method
    return None


_SETTING_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "frame_rate": (("requested_fps", "target_fps"), ("reported_fps",)),
    "width": (("requested_width",), ("actual_width",)),
    "height": (("requested_height",), ("actual_height",)),
    "pixel_format": (("requested_format",), ("actual_format",)),
}


def _setting_evidence(
    row: dict[str | None, Any],
    profile: _CameraProfile | None,
) -> list[_SettingEvidence]:
    evidence: list[_SettingEvidence] = []
    for setting_key, (requested_fields, reported_fields) in _SETTING_FIELDS.items():
        requested_field = next((name for name in requested_fields if row.get(name) not in (None, "")), None)
        reported_field = next((name for name in reported_fields if row.get(name) not in (None, "")), None)
        if not requested_field and not reported_field:
            continue
        evidence.append(
            _SettingEvidence(
                key=setting_key,
                requested=_scalar(row.get(requested_field)) if requested_field else None,
                reported=_scalar(row.get(reported_field)) if reported_field else None,
                verification_status="reported" if reported_field else "requested_only",
                verification_method=f"legacy_csv:{reported_field}" if reported_field else None,
            )
        )

    exposure_value = _scalar(row.get("exposure_value"))
    if profile and profile.vendor_id == "07ca" and exposure_value is not None:
        if str(exposure_value).casefold() == "auto":
            evidence.append(_SettingEvidence(key="exposure_mode", requested="auto"))
        else:
            evidence.append(_SettingEvidence(key="exposure_mode", requested="manual"))
            evidence.append(_SettingEvidence(key="exposure_time_absolute", requested=exposure_value))

    # The PS Eye summary script wrote CAP_PROP readbacks into generic width/height columns.
    if profile and profile.vendor_id == "1415" and row.get("frame_number") in (None, ""):
        for setting_key, field_name in (("width", "width"), ("height", "height")):
            if row.get(field_name) not in (None, "") and not row.get(f"actual_{field_name}"):
                evidence.append(
                    _SettingEvidence(
                        key=setting_key,
                        reported=_scalar(row.get(field_name)),
                        verification_status="reported",
                        verification_method=f"legacy_csv:{field_name} (OpenCV driver readback)",
                    )
                )

    # CHDKPTP zoom events include both requested step (phase) and get_zoom() readback (note).
    if profile and profile.backend is BackendKind.CHDKPTP:
        phase = (row.get("phase") or "").strip().casefold()
        event = (row.get("event") or "").strip().casefold()
        note = (row.get("note") or "").strip()
        if phase.startswith("zoom") and phase[4:].isdigit() and event in {"action_start", "action_end"}:
            reported = _scalar(note) if event == "action_end" else None
            evidence.append(
                _SettingEvidence(
                    key="optical_zoom_step",
                    requested=int(phase[4:]),
                    reported=reported,
                    verification_status="reported" if reported is not None else "requested_only",
                    verification_method="legacy_csv:note (CHDKPTP get_zoom)" if reported is not None else None,
                )
            )
        elif event == "started" and note.startswith("initial_zoom_"):
            reported = _scalar(note.removeprefix("initial_zoom_"))
            evidence.append(
                _SettingEvidence(
                    key="optical_zoom_step",
                    reported=reported,
                    verification_status="reported",
                    verification_method="legacy_csv:initial_zoom (CHDKPTP get_zoom)",
                )
            )
    return evidence


def _store_setting_evidence(
    connection,
    *,
    evidence: list[_SettingEvidence],
    session_id: str,
    benchmark_id: str,
    raw_record_id: int,
    observed_at: str | None,
    raw_json: str,
    only_if_missing: bool = False,
) -> None:
    for setting in evidence:
        if only_if_missing:
            existing = connection.execute(
                "SELECT 1 FROM setting_snapshots WHERE raw_record_id=? AND setting_key=? LIMIT 1",
                (raw_record_id, setting.key),
            ).fetchone()
            if existing:
                continue
        connection.execute(
            """INSERT INTO setting_snapshots (
                   id, session_id, benchmark_id, raw_record_id, setting_key,
                   requested_value_json, reported_value_json, verified_value_json,
                   verification_status, requested_at, checked_at, verification_method, raw_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                new_id(), session_id, benchmark_id, raw_record_id, setting.key,
                serialize_json(setting.requested) if setting.requested is not None else None,
                serialize_json(setting.reported) if setting.reported is not None else None,
                setting.verification_status,
                observed_at if setting.requested is not None else None,
                observed_at if setting.reported is not None else None,
                setting.verification_method,
                raw_json,
            ),
        )


class CSVHistoryImporter:
    """Import a CSV stream into SQLite, retaining each parsed source row.

    Source files are opened read-only. The importer computes a content hash
    before parsing and checks it again before commit so an edited source does
    not leave a partial or misleading import. Re-importing the same path and
    bytes is idempotent.
    """

    def __init__(self, database: Database, *, logger: logging.Logger | None = None) -> None:
        self.database = database
        self.logger = logger or logging.getLogger("camera_exploration_lab.importer")

    def import_file(self, path: str | Path) -> ImportResult:
        source_path = Path(path).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"CSV source is not a file: {source_path}")
        source_hash = _file_sha256(source_path)
        source_size = source_path.stat().st_size
        imported_at = utc_now_iso()
        source_id = new_id()
        session_id = new_id()
        profile = _legacy_camera_profile(source_path)
        filename_time = _filename_timestamp(source_path)
        session_metadata = {
            "source_path": str(source_path),
            "identity_is_inferred": bool(profile),
            "filename_timestamp": filename_time,
            "start_time_source": "pending",
        }
        camera_id = profile.stable_identifier if profile else None
        fps_scope = _context_scope(profile)
        row_count = 0
        measurement_count = 0
        earliest: str | None = None
        latest: str | None = None
        benchmark_ids: dict[str, str] = {}
        benchmark_times: dict[str, list[str]] = {}

        with self.database.connect() as connection:
            with connection:
                duplicate = connection.execute(
                    "SELECT id, session_id, row_count, measurement_count FROM import_sources "
                    "WHERE source_path=? AND sha256=?",
                    (str(source_path), source_hash),
                ).fetchone()
                if duplicate:
                    imported_session = connection.execute(
                        "SELECT started_at, ended_at, metadata_json FROM sessions WHERE id=?",
                        (duplicate["session_id"],),
                    ).fetchone()
                    if imported_session:
                        row_times = connection.execute(
                            "SELECT MIN(observed_at), MAX(observed_at) FROM raw_records WHERE session_id=?",
                            (duplicate["session_id"],),
                        ).fetchone()
                        row_start, row_end = row_times[0], row_times[1]
                        inferred_start = row_start or filename_time or imported_session["started_at"]
                        inferred_end = row_end or imported_session["ended_at"]
                        metadata = json.loads(imported_session["metadata_json"] or "{}")
                        time_source = "row_timestamp" if row_start else "csv_filename" if filename_time else "unknown"
                        metadata["filename_timestamp"] = filename_time
                        metadata["start_time_source"] = time_source
                        if (
                            inferred_start != imported_session["started_at"]
                            or inferred_end != imported_session["ended_at"]
                            or metadata != json.loads(imported_session["metadata_json"] or "{}")
                        ):
                            connection.execute(
                                "UPDATE sessions SET started_at=?, ended_at=?, metadata_json=? WHERE id=?",
                                (inferred_start, inferred_end, serialize_json(metadata), duplicate["session_id"]),
                            )
                    old_records = connection.execute(
                        """SELECT id, benchmark_id, observed_at, raw_json FROM raw_records
                           WHERE source_id=? ORDER BY row_number""",
                        (duplicate["id"],),
                    ).fetchall()
                    for record in old_records:
                        raw_json = record["raw_json"]
                        row = json.loads(raw_json)
                        _store_setting_evidence(
                            connection,
                            evidence=_setting_evidence(row, profile),
                            session_id=duplicate["session_id"],
                            benchmark_id=record["benchmark_id"],
                            raw_record_id=record["id"],
                            observed_at=record["observed_at"],
                            raw_json=raw_json,
                            only_if_missing=True,
                        )
                    return ImportResult(
                        source_path=str(source_path),
                        source_id=duplicate["id"],
                        session_id=duplicate["session_id"],
                        row_count=0,
                        measurement_count=0,
                        already_imported=True,
                    )

                if profile:
                    identity = CameraIdentity(
                        stable_identifier=profile.stable_identifier,
                        backend=profile.backend,
                        manufacturer=profile.manufacturer,
                        model=profile.model,
                        vendor_id=profile.vendor_id,
                        product_id=profile.product_id,
                        identity_source="inferred_from_legacy_path",
                        metadata={"identity_is_inferred": True},
                    )
                    connection.execute(
                        """INSERT INTO cameras (
                               id, stable_identifier, backend, device_path, manufacturer, model,
                               serial_number, vendor_id, product_id, identity_source,
                               first_seen_at, last_seen_at, metadata_json
                           ) VALUES (?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(stable_identifier) DO NOTHING""",
                        (
                            identity.stable_identifier,
                            identity.stable_identifier,
                            identity.backend.value,
                            identity.manufacturer,
                            identity.model,
                            identity.vendor_id,
                            identity.product_id,
                            identity.identity_source,
                            imported_at,
                            imported_at,
                            serialize_json(identity.metadata),
                        ),
                    )

                connection.execute(
                    """INSERT INTO sessions
                       (id, camera_id, label, source, started_at, ended_at, created_at, notes, metadata_json)
                       VALUES (?, ?, ?, 'imported', NULL, NULL, ?, ?, ?)""",
                    (
                        session_id,
                        camera_id,
                        f"Imported: {source_path.name}",
                        imported_at,
                        "Legacy camera identity inferred from path" if profile else None,
                        serialize_json(session_metadata),
                    ),
                )
                connection.execute(
                    """INSERT INTO import_sources
                       (id, source_path, source_basename, sha256, source_size_bytes, imported_at,
                        session_id, row_count, measurement_count, format)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'csv')""",
                    (source_id, str(source_path), source_path.name, source_hash, source_size, imported_at, session_id),
                )

                with source_path.open("r", encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source)
                    if not reader.fieldnames or not any(name for name in reader.fieldnames):
                        raise ValueError(f"CSV has no header row: {source_path}")
                    for row_number, row in enumerate(reader, start=1):
                        row_count += 1
                        mode = (row.get("mode") or "").strip()
                        benchmark_label = mode or source_path.stem
                        benchmark_key = mode or "__source__"
                        benchmark_id = benchmark_ids.get(benchmark_key)
                        if benchmark_id is None:
                            benchmark_id = new_id()
                            benchmark_ids[benchmark_key] = benchmark_id
                            benchmark_times[benchmark_key] = []
                            connection.execute(
                                """INSERT INTO benchmark_runs
                                   (id, session_id, label, started_at, ended_at, status, configuration_json)
                                   VALUES (?, ?, ?, NULL, NULL, 'imported', ?)""",
                                (
                                    benchmark_id,
                                    session_id,
                                    benchmark_label,
                                    serialize_json({"legacy_mode": mode} if mode else {"source_file": source_path.name}),
                                ),
                            )

                        timestamp_raw, observed_at = _timestamp(row)
                        elapsed = next(
                            (_number(row.get(key)) for key in ("elapsed_seconds", "elapsed_s") if _number(row.get(key)) is not None),
                            None,
                        )
                        sample_index = next(
                            (_integer(row.get(key)) for key in ("frame_number", "frame") if _integer(row.get(key)) is not None),
                            None,
                        )
                        raw_record = {
                            ("_extra_fields" if field_name is None else field_name): value
                            for field_name, value in row.items()
                        }
                        raw_json = serialize_json(raw_record)
                        cursor = connection.execute(
                            """INSERT INTO raw_records
                               (source_id, session_id, benchmark_id, row_number, timestamp_raw,
                                observed_at, elapsed_seconds, raw_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (source_id, session_id, benchmark_id, row_number, timestamp_raw, observed_at, elapsed, raw_json),
                        )
                        raw_record_id = int(cursor.lastrowid)
                        if observed_at:
                            earliest = observed_at if earliest is None else min(earliest, observed_at)
                            latest = observed_at if latest is None else max(latest, observed_at)
                            benchmark_times[benchmark_key].append(observed_at)

                        for field_name, raw_value in row.items():
                            if field_name is None:
                                continue
                            metric = _metric_for(field_name, raw_value, fps_scope=fps_scope)
                            if metric is None:
                                continue
                            metric_name, value, unit, scope, method = metric
                            interval_ns = int(value * 1_000_000) if field_name == "interval_ms" and value >= 0 else None
                            connection.execute(
                                """INSERT INTO measurements
                                   (id, benchmark_id, raw_record_id, metric, value, unit, stream_scope,
                                    observed_at, elapsed_seconds, sample_index, interval_ns, method)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    new_id(), benchmark_id, raw_record_id, metric_name, value, unit, scope,
                                    observed_at, elapsed, sample_index, interval_ns, method,
                                ),
                            )
                            measurement_count += 1

                        _store_setting_evidence(
                            connection,
                            evidence=_setting_evidence(row, profile),
                            session_id=session_id,
                            benchmark_id=benchmark_id,
                            raw_record_id=raw_record_id,
                            observed_at=observed_at,
                            raw_json=raw_json,
                        )

                if _file_sha256(source_path) != source_hash:
                    raise RuntimeError(f"CSV changed while it was being imported: {source_path}")
                for key, benchmark_id in benchmark_ids.items():
                    times = benchmark_times[key]
                    if times:
                        connection.execute(
                            "UPDATE benchmark_runs SET started_at=?, ended_at=? WHERE id=?",
                            (min(times), max(times), benchmark_id),
                        )
                session_metadata["start_time_source"] = (
                    "row_timestamp" if earliest else "csv_filename" if filename_time else "unknown"
                )
                connection.execute(
                    "UPDATE sessions SET started_at=?, ended_at=?, metadata_json=? WHERE id=?",
                    (earliest or filename_time, latest, serialize_json(session_metadata), session_id),
                )
                connection.execute(
                    "UPDATE import_sources SET row_count=?, measurement_count=? WHERE id=?",
                    (row_count, measurement_count, source_id),
                )

        self.logger.info(
            "Imported %s rows and %s measurements from %s",
            row_count,
            measurement_count,
            source_path,
        )
        return ImportResult(
            source_path=str(source_path),
            source_id=source_id,
            session_id=session_id,
            row_count=row_count,
            measurement_count=measurement_count,
        )
