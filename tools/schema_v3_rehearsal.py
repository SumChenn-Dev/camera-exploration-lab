#!/usr/bin/env python3
"""Disposable, synthetic-only v2 backup and v3 migration rehearsal.

This utility intentionally accepts no database path. Every source, backup and
working copy is created in a newly allocated private temporary directory and
removed on exit. It is not a production migration or restore command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from camera_exploration_lab.analysis import load_analysis
from camera_exploration_lab.db import Database
from camera_exploration_lab.recording_ownership import RecordingOwnershipBusyError
from camera_exploration_lab.schema_migrations import (
    GROUPED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SYNTHETIC_DATABASE_APPLICATION_ID,
    SchemaMigrationError,
    migrate_synthetic_database_to_v3,
    validate_schema_structure,
)

SYNTHETIC_MARKER = ".cel-synthetic-rehearsal"
FIXED_UTC = "2026-01-02T03:04:05+00:00"
V2_MEASUREMENT_COLUMNS = (
    "id", "benchmark_id", "raw_record_id", "metric", "value", "unit",
    "stream_scope", "observed_at", "elapsed_seconds", "sample_index",
    "interval_ns", "method",
)
LEGACY_TABLES = (
    "cameras", "sessions", "benchmark_runs", "setting_snapshots",
    "import_sources", "raw_records", "measurements", "benchmark_summaries",
)


@dataclass(frozen=True, slots=True)
class RehearsalReport:
    fixture_schema: int
    migrated_schema: int
    legacy_digest_before: str
    legacy_digest_after: str
    legacy_rows_before: int
    legacy_rows_after: int
    analysis_unchanged: bool
    backup_sha256: str
    backup_bytes: int
    backup_seconds: float
    migration_seconds: float
    integrity_check: str
    foreign_key_violations: int
    restore_verified: bool
    failure_rollback_verified: bool
    postcheck_failure_rollback_verified: bool


def _fsync_file_and_parent(path: Path) -> None:
    file_fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _exclusive_empty_file(path: Path) -> None:
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    os.close(descriptor)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect_readonly(path)
    try:
        yield connection
    finally:
        connection.close()


def _validate_synthetic_v2(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_root not in resolved_path.parents:
        raise RuntimeError("rehearsal target is outside its private synthetic directory")
    marker = resolved_root / SYNTHETIC_MARKER
    if not marker.is_file() or marker.read_text(encoding="ascii") != "synthetic-only\n":
        raise RuntimeError("synthetic rehearsal marker is missing")
    if not path.is_file():
        raise RuntimeError("synthetic rehearsal database is missing")
    with _readonly(path) as connection:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != SYNTHETIC_DATABASE_APPLICATION_ID or version != SCHEMA_VERSION:
            raise RuntimeError("rehearsal source is not a marked schema-v2 synthetic fixture")
        validate_schema_structure(connection, expected_version=SCHEMA_VERSION)
        _check_integrity(connection)


def _check_integrity(connection: sqlite3.Connection) -> tuple[str, int]:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or violations:
        raise RuntimeError("synthetic database integrity verification failed")
    return integrity, len(violations)


def _backup_consistently(
    source: Database,
    destination: Path,
    root: Path,
    *,
    progress: Callable[[int, int, int], None] | None = None,
) -> tuple[str, int, float]:
    """Online-backup under the cooperative inode owner; destination is new."""
    _validate_synthetic_v2(source.path, root)
    if destination.exists():
        raise FileExistsError("backup target already exists")
    started = time.perf_counter()
    created_by_call = False
    try:
        ownership = source.acquire_recording_ownership(
            owner_kind="synthetic_backup_rehearsal", check_recovery=True
        )
        try:
            _exclusive_empty_file(destination)
            created_by_call = True
            with _readonly(source.path) as source_connection:
                target_connection = sqlite3.connect(destination)
                try:
                    source_connection.backup(
                        target_connection, pages=512, progress=progress, sleep=0.01
                    )
                    target_connection.commit()
                finally:
                    target_connection.close()
            _fsync_file_and_parent(destination)
        finally:
            ownership.release(ownership.owner_token)
    except BaseException:
        # The destination is known to have been created by this call. It is
        # safe to remove a partial synthetic artifact, never an existing file.
        if created_by_call:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    try:
        _validate_synthetic_v2(destination, root)
        digest = _sha256(destination)
        return digest, destination.stat().st_size, time.perf_counter() - started
    except BaseException:
        # Verification failure applies only to the fresh backup created above.
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_snapshot(database: Database) -> tuple[str, int]:
    digest = hashlib.sha256()
    row_count = 0
    with database.connect_readonly() as connection:
        validate_schema_structure(connection)
        for table in LEGACY_TABLES:
            if table == "measurements":
                columns = V2_MEASUREMENT_COLUMNS
            else:
                columns = tuple(
                    str(row["name"])
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                )
            names = ",".join(f'"{name}"' for name in columns)
            rows = connection.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid')
            for row in rows:
                digest.update(json.dumps(
                    tuple(row), ensure_ascii=False, default=str, separators=(",", ":")
                ).encode("utf-8"))
                digest.update(b"\n")
                row_count += 1
    return digest.hexdigest(), row_count


def _analysis_digest(database: Database, benchmark_id: str, session_id: str) -> tuple[object, ...]:
    result = load_analysis(database, benchmark_id, session_id)
    return (
        result.option.session_id,
        result.option.benchmark_id,
        result.saved_status,
        result.duration_seconds,
        result.frame_count,
        result.read_failures,
        result.interval_stats,
        result.capture_fps_stats,
        result.interval_histogram,
        result.fps_points,
    )


def _seed_v2_fixture(path: Path) -> tuple[Database, str, str]:
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute(f"PRAGMA application_id={SYNTHETIC_DATABASE_APPLICATION_ID}")
        cameras = (
            ("synthetic-camera-a", "synthetic:camera:a", "Synthetic Eye"),
            ("synthetic-camera-b", "synthetic:camera:b", "Synthetic Wide Camera"),
        )
        for camera_id, stable_id, model in cameras:
            connection.execute(
                """INSERT INTO cameras
                   (id,stable_identifier,backend,device_path,manufacturer,model,serial_number,
                    vendor_id,product_id,identity_source,first_seen_at,last_seen_at,metadata_json)
                   VALUES (?,?, 'v4l2', ?, 'Synthetic', ?, 'SYNTHETIC-ONLY', 'ffff','0001',
                           'synthetic_fixture', ?, ?, '{}')""",
                (camera_id, stable_id, f"synthetic-node-{camera_id[-1]}", model, FIXED_UTC, FIXED_UTC),
            )
        session_rows = (
            ("synthetic-import-session", "synthetic import — café", "imported", "synthetic-camera-a", "csv import example"),
            ("synthetic-live-session", "synthetic ordinary run", "live_experiment", "synthetic-camera-b", "two independent historical runs"),
            ("synthetic-interrupted-session", "synthetic interrupted run", "live_experiment", "synthetic-camera-a", "terminal incomplete fixture"),
        )
        for session_id, label, source, camera_id, notes in session_rows:
            connection.execute(
                """INSERT INTO sessions
                   (id,camera_id,label,source,started_at,ended_at,created_at,notes,metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (session_id, camera_id, label, source, FIXED_UTC, FIXED_UTC, FIXED_UTC, notes,
                 '{"synthetic_fixture":true,"unicode":"cámara"}'),
            )
        run_rows = (
            ("synthetic-import-run-1", "synthetic-import-session", "Imported profile A", "imported", '{"requested":{"fps":60}}'),
            ("synthetic-import-run-2", "synthetic-import-session", "Imported profile B", "imported", '{"requested":{"fps":30}}'),
            ("synthetic-live-run-1", "synthetic-live-session", "native sparse", "completed", '{"requested":{"fps":24}}'),
            ("synthetic-live-run-2", "synthetic-live-session", "native summary", "completed", '{"requested":{"fps":60}}'),
            ("synthetic-interrupted-run", "synthetic-interrupted-session", "interrupted", "interrupted", '{}'),
        )
        for run_id, session_id, label, status, configuration in run_rows:
            connection.execute(
                """INSERT INTO benchmark_runs
                   (id,session_id,label,started_at,ended_at,status,configuration_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, session_id, label, FIXED_UTC, FIXED_UTC, status, configuration),
            )
        connection.execute(
            """INSERT INTO import_sources
               (id,source_path,source_basename,sha256,source_size_bytes,imported_at,
                session_id,row_count,measurement_count,format)
               VALUES ('synthetic-source','synthetic://fixture.csv','fixture.csv',?,128,?,
                       'synthetic-import-session',3,3,'csv')""",
            (hashlib.sha256(b"synthetic csv content").hexdigest(), FIXED_UTC),
        )
        connection.execute(
            """INSERT INTO raw_records
               (source_id,session_id,benchmark_id,row_number,timestamp_raw,observed_at,
                elapsed_seconds,raw_json)
               VALUES ('synthetic-source','synthetic-import-session','synthetic-import-run-1',
                       1,'synthetic-time',?,0.0,'{"value":1,"label":"synthetic"}')""",
            (FIXED_UTC,),
        )
        raw_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """INSERT INTO setting_snapshots
               (id,session_id,benchmark_id,raw_record_id,setting_key,requested_value_json,
                reported_value_json,verified_value_json,verification_status,requested_at,
                checked_at,verification_method,raw_json)
               VALUES ('synthetic-setting','synthetic-import-session','synthetic-import-run-1',
                       NULL,'fps','60','59.8',NULL,'reported',?,?, 'synthetic readback','{}')""",
            (FIXED_UTC, FIXED_UTC),
        )
        connection.execute(
            """INSERT INTO benchmark_summaries
               (benchmark_id,frame_count,read_failures,duration_seconds,capture_fps,preview_fps,
                processing_fps,interval_count,median_interval_ms,p95_interval_ms,p99_interval_ms,summary_json)
               VALUES ('synthetic-import-run-1',3,0,0.04,60,30,NULL,2,16.7,16.8,16.8,'{"source":"synthetic"}')"""
        )
        for index in range(3):
            connection.execute(
                """INSERT INTO measurements
                   (id,benchmark_id,raw_record_id,metric,value,unit,stream_scope,observed_at,
                    elapsed_seconds,sample_index,interval_ns,method)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"synthetic-import-measurement-{index}", "synthetic-import-run-1",
                 raw_id if index == 0 else None, "frame_arrival", float(index), "frame",
                 "capture", FIXED_UTC, index / 60.0, index, None if index == 0 else 16_666_667,
                 "synthetic_import_v1"),
            )
        connection.execute(
            """INSERT INTO measurements
               (id,benchmark_id,metric,value,unit,stream_scope,observed_at,elapsed_seconds,
                sample_index,interval_ns,method)
               VALUES ('synthetic-sparse-measurement','synthetic-live-run-1','frame_arrival',
                       1,'frame','capture',?,1.0,2,40000000,'synthetic_sparse_v1')""",
            (FIXED_UTC,),
        )
        connection.execute(
            """INSERT INTO measurements
               (id,benchmark_id,metric,value,unit,stream_scope,observed_at,elapsed_seconds,
                sample_index,interval_ns,method)
               VALUES ('synthetic-summary-measurement','synthetic-live-run-2','capture_fps',
                       24,'frames/s','capture',?,1.0,1,NULL,'synthetic_summary_v1')""",
            (FIXED_UTC,),
        )
    # Stream a bounded number of realistic legacy timing rows in chunks. These
    # are rows in a synthetic file, never a capture or imported user dataset.
    with database.connect() as connection:
        for base in range(0, 5000, 250):
            connection.executemany(
                """INSERT INTO measurements
                   (id,benchmark_id,metric,value,unit,stream_scope,observed_at,
                    elapsed_seconds,sample_index,interval_ns,method)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (f"synthetic-large-{index:06d}", "synthetic-live-run-2", "frame_arrival",
                     1.0, "frame", "capture", FIXED_UTC, index / 60.0, index,
                     None if index == 0 else 16_666_667, "synthetic_large_v1")
                    for index in range(base, base + 250)
                ],
            )
    return database, "synthetic-import-session", "synthetic-import-run-1"


def _copy_restore_from_backup(backup: Path, destination: Path, root: Path) -> None:
    if destination.exists():
        raise FileExistsError("restore destination already exists")
    _exclusive_empty_file(destination)
    try:
        source_connection = _connect_readonly(backup)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection, pages=512, sleep=0.01)
            destination_connection.commit()
        finally:
            source_connection.close()
            destination_connection.close()
        _fsync_file_and_parent(destination)
        _validate_synthetic_v2(destination, root)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def run_rehearsal() -> RehearsalReport:
    with tempfile.TemporaryDirectory(prefix="cel-schema-v3-rehearsal-") as directory:
        root = Path(directory).resolve()
        (root / SYNTHETIC_MARKER).write_text("synthetic-only\n", encoding="ascii")
        source_path = root / "source-v2.sqlite3"
        source, session_id, benchmark_id = _seed_v2_fixture(source_path)
        original_digest, original_rows = _legacy_snapshot(source)
        analysis_before = _analysis_digest(source, benchmark_id, session_id)

        backup_path = root / "consistent-backup.sqlite3"
        backup_digest, backup_bytes, backup_seconds = _backup_consistently(
            source, backup_path, root
        )
        backup_db = Database(backup_path)
        if _legacy_snapshot(backup_db) != (original_digest, original_rows):
            raise RuntimeError("backup does not preserve synthetic v2 fixture content")

        work_path = root / "migration-work-copy.sqlite3"
        _copy_restore_from_backup(backup_path, work_path, root)
        work = Database(work_path)
        migration_start = time.perf_counter()
        migrate_synthetic_database_to_v3(work)
        migration_seconds = time.perf_counter() - migration_start
        migrated_digest, migrated_rows = _legacy_snapshot(work)
        if (migrated_digest, migrated_rows) != (original_digest, original_rows):
            raise RuntimeError("migration changed synthetic legacy data")
        analysis_after = _analysis_digest(work, benchmark_id, session_id)
        if analysis_after != analysis_before:
            raise RuntimeError("legacy Analysis output changed across synthetic migration")
        with work.connect_readonly() as connection:
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != GROUPED_SCHEMA_VERSION:
                raise RuntimeError("synthetic migration did not produce schema v3")
            integrity, violations = _check_integrity(connection)
            validate_schema_structure(connection, expected_version=GROUPED_SCHEMA_VERSION)
        # Migration failure injection must roll back to exactly the backup's v2
        # contents. The source remains unchanged throughout the rehearsal.
        rollback_path = root / "rollback-copy.sqlite3"
        _copy_restore_from_backup(backup_path, rollback_path, root)
        rollback_db = Database(rollback_path)
        initial_schema = _schema_digest(rollback_db)
        def fail_after_schema_change(step: str) -> None:
            if step == "v2_to_v3:create_group_recordings":
                raise RuntimeError("synthetic interrupted migration")
        try:
            migrate_synthetic_database_to_v3(
                rollback_db, _after_step=fail_after_schema_change
            )
        except RuntimeError as exc:
            if str(exc) != "synthetic interrupted migration":
                raise
        else:
            raise RuntimeError("injected synthetic migration failure did not occur")
        rollback_verified = (
            _schema_digest(rollback_db) == initial_schema
            and _legacy_snapshot(rollback_db) == (original_digest, original_rows)
        )
        if not rollback_verified:
            raise RuntimeError("transactional migration rollback did not restore v2 state")

        postcheck_path = root / "postcheck-copy.sqlite3"
        _copy_restore_from_backup(backup_path, postcheck_path, root)
        postcheck_db = Database(postcheck_path)
        postcheck_schema = _schema_digest(postcheck_db)
        from unittest.mock import patch
        import camera_exploration_lab.schema_migrations as migrations
        try:
            with patch.object(
                migrations, "_check_integrity",
                side_effect=SchemaMigrationError("synthetic post-migration check failure"),
            ):
                migrate_synthetic_database_to_v3(postcheck_db)
        except SchemaMigrationError:
            pass
        else:
            raise RuntimeError("injected post-migration integrity failure did not occur")
        postcheck_rollback_verified = (
            _schema_digest(postcheck_db) == postcheck_schema
            and _legacy_snapshot(postcheck_db) == (original_digest, original_rows)
        )
        if not postcheck_rollback_verified:
            raise RuntimeError("post-migration failure did not roll back transactionally")

        restore_path = root / "restored-after-failure.sqlite3"
        _copy_restore_from_backup(backup_path, restore_path, root)
        restored = Database(restore_path)
        restore_verified = _legacy_snapshot(restored) == (original_digest, original_rows)
        if not restore_verified:
            raise RuntimeError("backup restore verification failed")
        if _legacy_snapshot(source) != (original_digest, original_rows):
            raise RuntimeError("original synthetic source changed during rehearsal")
        return RehearsalReport(
            fixture_schema=SCHEMA_VERSION,
            migrated_schema=GROUPED_SCHEMA_VERSION,
            legacy_digest_before=original_digest,
            legacy_digest_after=migrated_digest,
            legacy_rows_before=original_rows,
            legacy_rows_after=migrated_rows,
            analysis_unchanged=analysis_after == analysis_before,
            backup_sha256=backup_digest,
            backup_bytes=backup_bytes,
            backup_seconds=backup_seconds,
            migration_seconds=migration_seconds,
            integrity_check=integrity,
            foreign_key_violations=violations,
            restore_verified=restore_verified,
            failure_rollback_verified=rollback_verified,
            postcheck_failure_rollback_verified=postcheck_rollback_verified,
        )


def _schema_digest(database: Database) -> str:
    digest = hashlib.sha256()
    with database.connect_readonly() as connection:
        for row in connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ):
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        digest.update(str(connection.execute("PRAGMA user_version").fetchone()[0]).encode())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--acknowledge-synthetic-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.synthetic_only or not args.acknowledge_synthetic_only:
        parser.error("both --synthetic-only and --acknowledge-synthetic-only are required")
    print("SYNTHETIC-ONLY migration rehearsal; no existing database path is accepted.", flush=True)
    report = run_rehearsal()
    print(json.dumps(asdict(report), sort_keys=True, indent=2), flush=True)
    print("Temporary databases and backups were removed after verification.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
