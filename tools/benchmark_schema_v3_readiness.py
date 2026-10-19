#!/usr/bin/env python3
"""Synthetic-only migration, backup, and grouped-reader resource benchmark.

All databases are generated in fresh temporary directories and deleted when a
case ends. These SQLite results do not measure camera capture performance.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_analysis import load_grouped_analysis
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
    StreamStatus,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity, MeasurementSample, StreamScope
from camera_exploration_lab.schema_migrations import (
    GROUPED_SCHEMA_VERSION,
    SYNTHETIC_DATABASE_APPLICATION_ID,
    migrate_synthetic_database_to_v3,
    validate_schema_structure,
)
from tools.schema_v3_rehearsal import (
    SYNTHETIC_MARKER,
    _check_integrity,
    _connect_readonly,
    _seed_v2_fixture,
)

BATCH_SIZE = 512
FIXED_UTC = "2026-01-02T03:04:05+00:00"


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    duration_seconds: int
    stream_a_fps: int
    stream_b_fps: int
    expected_rows: int
    persisted_rows: int
    database_bytes: int
    backup_bytes: int
    backup_seconds: float
    migration_seconds: float
    persistence_seconds: float
    persist_rows_per_second: float
    integrity_seconds: float
    integrity_result: str
    foreign_key_violations: int
    reopen_seconds: float
    legacy_history_ms: float
    grouped_history_ms: float
    grouped_analysis_ms: float
    grouped_analysis_runs_ms: tuple[float, ...]
    analysis_samples: int
    analysis_python_peak_kib: int | None
    batch_size: int


def _create_group(repository: GroupedExperimentRepository, duration: int, fps_a: int, fps_b: int):
    streams = (
        GroupedStreamSpec(
            slot="A",
            camera=CameraIdentity(
                "synthetic:readiness:camera-a", BackendKind.V4L2,
                device_path="synthetic-node-a", manufacturer="Synthetic",
                model="Synthetic High Rate", identity_source="synthetic_fixture",
            ),
            endpoint_key="synthetic:endpoint:a",
            benchmark_label=f"synthetic A {fps_a} fps",
            configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": fps_a},
        ),
        GroupedStreamSpec(
            slot="B",
            camera=CameraIdentity(
                "synthetic:readiness:camera-b", BackendKind.V4L2,
                device_path="synthetic-node-b", manufacturer="Synthetic",
                model="Synthetic Standard Rate", identity_source="synthetic_fixture",
            ),
            endpoint_key="synthetic:endpoint:b",
            benchmark_label=f"synthetic B {fps_b} fps",
            configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": fps_b},
        ),
    )
    group = repository.create_group(GroupedExperimentSpec(
        label=f"Synthetic readiness {duration}s {fps_a}+{fps_b}",
        notes="Synthetic-only persistence benchmark.",
        duration_target_seconds=float(duration),
        record_requested_at=FIXED_UTC,
        created_at=FIXED_UTC,
        clock_epoch_id=f"synthetic-readiness-{duration}-{fps_a}-{fps_b}",
        streams=streams,
    ))
    for stream in group.streams:
        repository.set_stream_status(
            group.session_id, stream.slot, StreamStatus.RECORDING,
            observed_at=FIXED_UTC, elapsed_ns=0,
        )
    repository.set_group_status(
        group.session_id, GroupStatus.RECORDING, observed_at=FIXED_UTC, elapsed_ns=0
    )
    return group


def _write_stream(repository: GroupedExperimentRepository, group, slot: str, fps: int, duration: int) -> int:
    stream = next(item for item in group.streams if item.slot == slot)
    total = fps * duration
    batch: list[MeasurementSample] = []
    previous_ns: int | None = None
    for sequence in range(total):
        elapsed_ns = sequence * 1_000_000_000 // fps
        interval_ns = elapsed_ns - previous_ns if previous_ns is not None else None
        batch.append(MeasurementSample(
            benchmark_id=stream.benchmark_id,
            metric="frame_arrival",
            value=float(fps),
            unit="frames/s",
            stream_scope=StreamScope.CAPTURE,
            observed_at=FIXED_UTC,
            elapsed_seconds=elapsed_ns / 1_000_000_000,
            sample_index=sequence,
            interval_ns=interval_ns,
            method="synthetic_monotonic_read_return",
            elapsed_ns=elapsed_ns,
            source_timestamp_ns=None,
            source_sequence=None,
            source_clock=None,
        ))
        previous_ns = elapsed_ns
        if len(batch) == BATCH_SIZE:
            repository.record_stream_measurements(group.session_id, slot, batch)
            batch.clear()
    if batch:
        repository.record_stream_measurements(group.session_id, slot, batch)
    return total


def _online_backup_v3(database: Database, destination: Path, root: Path) -> tuple[int, float]:
    if destination.exists():
        raise FileExistsError("synthetic benchmark backup target already exists")
    resolved_root = root.resolve()
    if resolved_root not in database.path.resolve().parents or resolved_root not in destination.resolve().parents:
        raise RuntimeError("synthetic benchmark path escaped its private temporary directory")
    marker = resolved_root / SYNTHETIC_MARKER
    if not marker.is_file() or marker.read_text(encoding="ascii") != "synthetic-only\n":
        raise RuntimeError("synthetic benchmark marker is missing")
    source_connection = _connect_readonly(database.path)
    try:
        if int(source_connection.execute("PRAGMA application_id").fetchone()[0]) != SYNTHETIC_DATABASE_APPLICATION_ID:
            raise RuntimeError("benchmark source is not a marked synthetic database")
        validate_schema_structure(source_connection, expected_version=GROUPED_SCHEMA_VERSION)
    finally:
        source_connection.close()
    started = time.perf_counter()
    created = False
    try:
        ownership = database.acquire_recording_ownership(
            owner_kind="synthetic_schema_v3_benchmark", check_recovery=True
        )
        try:
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)
            created = True
            source_connection = _connect_readonly(database.path)
            target_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(target_connection, pages=512, sleep=0.01)
                target_connection.commit()
            finally:
                source_connection.close()
                target_connection.close()
            file_fd = os.open(destination, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            ownership.release(ownership.owner_token)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    connection = _connect_readonly(destination)
    try:
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != SYNTHETIC_DATABASE_APPLICATION_ID:
            raise RuntimeError("synthetic benchmark backup marker was not preserved")
        validate_schema_structure(connection, expected_version=GROUPED_SCHEMA_VERSION)
        _check_integrity(connection)
    finally:
        connection.close()
    return destination.stat().st_size, time.perf_counter() - started


def run_case(duration: int, fps_a: int, fps_b: int) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="cel-schema-v3-bench-") as directory:
        root = Path(directory).resolve()
        (root / SYNTHETIC_MARKER).write_text("synthetic-only\n", encoding="ascii")
        database, _legacy_session, _legacy_benchmark = _seed_v2_fixture(root / "fixture-v2.sqlite3")
        migration_started = time.perf_counter()
        migrate_synthetic_database_to_v3(database)
        migration_seconds = time.perf_counter() - migration_started

        repository = GroupedExperimentRepository(database)
        group = _create_group(repository, duration, fps_a, fps_b)
        persistence_started = time.perf_counter()
        rows_a = _write_stream(repository, group, "A", fps_a, duration)
        rows_b = _write_stream(repository, group, "B", fps_b, duration)
        persistence_seconds = time.perf_counter() - persistence_started
        for stream in group.streams:
            repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at=FIXED_UTC, elapsed_ns=duration * 1_000_000_000,
            )
        repository.finalize_group(
            group.session_id,
            GroupStatus.COMPLETED,
            ended_at=FIXED_UTC,
            duration_by_slot={"A": float(duration), "B": float(duration)},
            termination_reason="synthetic_benchmark_complete",
        )

        integrity_started = time.perf_counter()
        connection = _connect_readonly(database.path)
        try:
            integrity, violations = _check_integrity(connection)
        finally:
            connection.close()
        integrity_seconds = time.perf_counter() - integrity_started

        backup_bytes, backup_seconds = _online_backup_v3(
            database, root / "v3-backup.sqlite3", root
        )
        reopen_started = time.perf_counter()
        reopened = Database(database.path)
        with reopened.connect_readonly() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        reopen_seconds = time.perf_counter() - reopen_started
        if version != GROUPED_SCHEMA_VERSION:
            raise RuntimeError("synthetic benchmark database reopened at an unexpected schema")

        legacy_started = time.perf_counter()
        legacy_history_count = len(database.history_entries())
        legacy_history_ms = (time.perf_counter() - legacy_started) * 1000
        grouped_started = time.perf_counter()
        groups = repository.list_groups(limit=1000)
        grouped_history_ms = (time.perf_counter() - grouped_started) * 1000
        # Time the read path without allocation tracing: tracemalloc has a
        # large per-allocation cost and previously overstated elapsed time by
        # tracing synthetic row generation and the analysis pass together.
        analysis_times_ms: list[float] = []
        analysis = None
        for _ in range(3):
            analysis_started = time.perf_counter()
            analysis = load_grouped_analysis(database, group.session_id)
            analysis_times_ms.append((time.perf_counter() - analysis_started) * 1000)
        grouped_analysis_ms = statistics.median(analysis_times_ms)

        # Bound memory measurement to the largest synthetic analysis case and
        # keep it separate from timed runs. This reports Python allocations for
        # Analysis preparation only, not fixture generation or persistence.
        analysis_python_peak_kib = None
        if duration == 1800 and fps_a == 187 and fps_b == 30:
            tracemalloc.start()
            tracemalloc.reset_peak()
            try:
                measured = load_grouped_analysis(database, group.session_id)
                _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            if sum(item.persisted_frame_samples for item in measured.streams) != rows_a + rows_b:
                raise RuntimeError("traced analysis pass returned an inconsistent row count")
            analysis_python_peak_kib = round(peak_bytes / 1024)

        with database.connect_readonly() as connection:
            run_ids = tuple(stream.benchmark_id for stream in group.streams)
            placeholders = ",".join("?" for _ in run_ids)
            persisted = int(connection.execute(
                f"SELECT COUNT(*) FROM measurements WHERE benchmark_id IN ({placeholders}) "
                "AND metric='frame_arrival' AND stream_scope='capture'",
                run_ids,
            ).fetchone()[0])
        expected = rows_a + rows_b
        if persisted != expected:
            raise RuntimeError("synthetic benchmark sample count mismatch")
        if len(groups) != 1 or sum(item.persisted_frame_samples for item in analysis.streams) != expected:
            raise RuntimeError("synthetic grouped readers returned inconsistent sample counts")
        return ScenarioResult(
            duration_seconds=duration,
            stream_a_fps=fps_a,
            stream_b_fps=fps_b,
            expected_rows=expected,
            persisted_rows=persisted,
            database_bytes=database.path.stat().st_size,
            backup_bytes=backup_bytes,
            backup_seconds=round(backup_seconds, 4),
            migration_seconds=round(migration_seconds, 4),
            persistence_seconds=round(persistence_seconds, 4),
            persist_rows_per_second=round(expected / persistence_seconds, 1),
            integrity_seconds=round(integrity_seconds, 4),
            integrity_result=integrity,
            foreign_key_violations=violations,
            reopen_seconds=round(reopen_seconds, 4),
            legacy_history_ms=round(legacy_history_ms, 3),
            grouped_history_ms=round(grouped_history_ms, 3),
            grouped_analysis_ms=round(grouped_analysis_ms, 3),
            grouped_analysis_runs_ms=tuple(round(value, 3) for value in analysis_times_ms),
            analysis_samples=sum(item.persisted_frame_samples for item in analysis.streams),
            analysis_python_peak_kib=analysis_python_peak_kib,
            batch_size=BATCH_SIZE,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--acknowledge-synthetic-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.synthetic_only or not args.acknowledge_synthetic_only:
        parser.error("both --synthetic-only and --acknowledge-synthetic-only are required")
    print("SYNTHETIC-ONLY SQLite benchmark; timings are not camera performance.", flush=True)
    for duration in (30, 300, 1800):
        for fps_a, fps_b in ((100, 24), (187, 30)):
            print(f"Running synthetic case {duration}s, {fps_a}+{fps_b} rows/s", flush=True)
            result = run_case(duration, fps_a, fps_b)
            print(json.dumps(asdict(result), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
