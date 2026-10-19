"""Synthetic-only database fixtures shared by CEL-013 Pass 2A tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from camera_exploration_lab.db import Database, serialize_json
from camera_exploration_lab.models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    MeasurementSample,
    SessionRecord,
    SettingSnapshot,
    StreamScope,
)
from camera_exploration_lab.schema_migrations import SYNTHETIC_DATABASE_APPLICATION_ID


@dataclass(frozen=True)
class LegacyFixtureIds:
    camera_id: str
    session_id: str
    benchmark_ids: tuple[str, ...]
    import_source_id: str
    raw_record_id: int


def create_synthetic_database(
    path: Path,
    *,
    version: int = 2,
    seed_legacy_data: bool = True,
) -> tuple[Database, LegacyFixtureIds | None]:
    """Create a marked, synthetic v0/v1/v2 database. Never reads user data."""
    database = Database(path)
    if version == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA application_id = {SYNTHETIC_DATABASE_APPLICATION_ID}")
            connection.execute("PRAGMA user_version = 0")
        return database, None

    database.initialize()
    fixture_ids = None
    if seed_legacy_data:
        camera = CameraIdentity(
            stable_identifier="synthetic:camera:legacy-01",
            backend=BackendKind.V4L2,
            device_path="synthetic-node-a",
            manufacturer="Synthetic Manufacturer",
            model="Synthetic Legacy Camera",
            serial_number="SYNTHETIC-NOT-REAL",
            vendor_id="ffff",
            product_id="0001",
            identity_source="synthetic_fixture",
            metadata={"fixture": True},
        )
        camera_id = database.upsert_camera(camera, seen_at="2026-01-02T03:04:05+00:00")
        session = SessionRecord(
            camera_id=camera_id,
            label="Synthetic imported experiment",
            source="imported",
            started_at="2026-01-02T03:04:05+00:00",
            notes="Synthetic fixture only",
            metadata={"synthetic": True},
        )
        database.create_session(session)
        first = BenchmarkRecord(
            session_id=session.id,
            label="synthetic first run",
            started_at=session.started_at,
            ended_at="2026-01-02T03:04:35+00:00",
            status="imported",
            configuration={"mode": "synthetic-480p"},
        )
        second = BenchmarkRecord(
            session_id=session.id,
            label="synthetic second run",
            started_at=session.started_at,
            status="imported",
            configuration={"mode": "synthetic-720p"},
        )
        database.create_benchmark(first)
        database.create_benchmark(second)
        database.record_setting(
            SettingSnapshot(
                session_id=session.id,
                benchmark_id=first.id,
                setting_key="fps",
                requested_value=60,
                reported_value=59.8,
                verification_status="reported",
                verification_method="synthetic readback",
            )
        )
        database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=first.id,
                metric="frame_arrival",
                value=1.0,
                unit="seconds",
                stream_scope=StreamScope.CAPTURE,
                observed_at="2026-01-02T03:04:06+00:00",
                elapsed_seconds=1.0,
                sample_index=1,
                interval_ns=16_666_667,
                method="synthetic legacy timing",
            ),
            MeasurementSample(
                benchmark_id=first.id,
                metric="frame_arrival",
                value=2.0,
                unit="seconds",
                stream_scope=StreamScope.CAPTURE,
                observed_at="2026-01-02T03:04:07+00:00",
                elapsed_seconds=2.0,
                sample_index=2,
                interval_ns=16_666_666,
                method="synthetic legacy timing",
            ),
            MeasurementSample(
                benchmark_id=second.id,
                metric="capture_fps",
                value=24.1,
                unit="frames/s",
                stream_scope=StreamScope.CAPTURE,
                sample_index=1,
                method="synthetic summary-only run",
            ),
        ])
        with database.connect() as connection:
            database._save_summary(
                connection,
                first.id,
                {
                    "frame_count": 2,
                    "read_failures": 0,
                    "duration_seconds": 2.0,
                    "capture_fps": 1.0,
                    "preview_fps": None,
                    "processing_fps": None,
                    "interval_count": 2,
                    "median_interval_ms": 16.6666665,
                    "p95_interval_ms": 16.666667,
                    "p99_interval_ms": 16.666667,
                    "synthetic": True,
                },
            )
            connection.execute(
                """INSERT INTO import_sources
                   (id, source_path, source_basename, sha256, source_size_bytes, imported_at,
                    session_id, row_count, measurement_count, format)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "synthetic-import-source", "synthetic://fixture.csv", "fixture.csv",
                    hashlib.sha256(b"synthetic fixture").hexdigest(), 17,
                    "2026-01-02T03:04:05+00:00", session.id, 2, 3, "csv",
                ),
            )
            connection.execute(
                """INSERT INTO raw_records
                   (source_id, session_id, benchmark_id, row_number, timestamp_raw, observed_at,
                    elapsed_seconds, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "synthetic-import-source", session.id, first.id, 1, "synthetic-ts",
                    "2026-01-02T03:04:06+00:00", 1.0,
                    serialize_json({"synthetic": True, "value": 1}),
                ),
            )
            raw_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        fixture_ids = LegacyFixtureIds(
            camera_id=camera_id,
            session_id=session.id,
            benchmark_ids=(first.id, second.id),
            import_source_id="synthetic-import-source",
            raw_record_id=raw_id,
        )

    with database.connect() as connection:
        connection.execute(f"PRAGMA application_id = {SYNTHETIC_DATABASE_APPLICATION_ID}")
        if version == 1:
            for name in (
                "idx_measurements_benchmark_metric_sample",
                "idx_measurements_benchmark_interval",
            ):
                connection.execute(f"DROP INDEX {name}")
            connection.execute("DROP TABLE benchmark_summaries")
            connection.execute("PRAGMA user_version = 1")
        elif version == 2:
            connection.execute("PRAGMA user_version = 2")
        else:
            raise ValueError("version must be 0, 1, or 2")
    return database, fixture_ids


def legacy_snapshot(database: Database) -> dict[str, str]:
    """Hash legacy table rows without including schema-v3 additions."""
    legacy_tables = (
        "cameras", "sessions", "benchmark_runs", "setting_snapshots",
        "import_sources", "raw_records", "measurements", "benchmark_summaries",
    )
    snapshots: dict[str, str] = {}
    with database.connect_readonly() as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table in legacy_tables:
            if table not in tables:
                continue
            columns = [
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                if not (
                    table == "measurements"
                    and row["name"] in {
                        "elapsed_ns", "source_timestamp_ns", "source_sequence", "source_clock"
                    }
                )
            ]
            column_sql = ", ".join(f'"{column}"' for column in columns)
            rows = [
                tuple(row)
                for row in connection.execute(
                    f'SELECT {column_sql} FROM "{table}" ORDER BY rowid'
                )
            ]
            snapshots[table] = hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":")).encode()
            ).hexdigest()
    return snapshots


def schema_fingerprint(database: Database) -> tuple[int, tuple[tuple[str, str], ...]]:
    with database.connect_readonly() as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema = tuple(connection.execute(
            """SELECT type, name FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
        ).fetchall())
        return version, tuple((str(row[0]), str(row[1])) for row in schema)
