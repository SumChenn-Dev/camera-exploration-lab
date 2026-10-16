#!/usr/bin/env python3
"""Synthetic-only grouped analysis volume and query benchmark.

Creates one fresh, marked schema-v3 database per scenario in a temporary
directory. Measurement rows are generated and inserted in bounded batches.
No camera, user database, or arbitrary database path is accepted.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_analysis import (
    _exact_interval_percentiles,
    _shared_time_supports,
    load_grouped_analysis,
)
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
    StreamStatus,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity, MeasurementSample, StreamScope
from camera_exploration_lab.schema_migrations import (
    SYNTHETIC_DATABASE_APPLICATION_ID,
    migrate_synthetic_database_to_v3,
)

BATCH_SIZE = 1024
UTC_MARKER = "2026-03-01T12:00:00+00:00"
SCENARIOS = ((30, 100, 24), (30, 187, 30), (300, 100, 24), (300, 187, 30), (1800, 100, 24), (1800, 187, 30))


def _build_group(repository: GroupedExperimentRepository, duration: int, fps_a: int, fps_b: int):
    streams = []
    for slot, model, fps, pixel, width, height in (
        ("A", "Synthetic High Rate", fps_a, "GRBG", 320, 240),
        ("B", "Synthetic Standard Rate", fps_b, "MJPEG", 1280, 720),
    ):
        streams.append(GroupedStreamSpec(
            slot=slot,
            camera=CameraIdentity(
                stable_identifier=f"synthetic:group-analysis:{slot.lower()}:{duration}:{fps_b}",
                backend=BackendKind.V4L2,
                manufacturer="Synthetic",
                model=model,
                identity_source="synthetic_fixture",
                metadata={"synthetic_only": True},
            ),
            endpoint_key=f"synthetic-analysis-{duration}-{fps_b}-{slot.lower()}",
            benchmark_label=f"{width}x{height} {pixel} synthetic timing",
            configuration={
                "width": width,
                "height": height,
                "pixel_format": pixel,
                "requested_fps": fps,
                "synthetic_target_fps": fps,
            },
            timing_method="synthetic_host_arrival_v1",
            timestamp_source="host_monotonic_synthetic_event",
            clock_type="monotonic_ns",
            collection_point="synthetic_event_producer",
        ))
    group = repository.create_group(GroupedExperimentSpec(
        label=f"Synthetic analysis {duration}s · {fps_a}+{fps_b} events/s",
        duration_target_seconds=float(duration),
        created_at=UTC_MARKER,
        record_requested_at=UTC_MARKER,
        clock_epoch_id=f"synthetic-analysis-clock-{duration}-{fps_a}-{fps_b}",
        streams=(streams[0], streams[1]),
    ))
    repository.add_event(
        group.session_id,
        "recording_coordinator_started",
        observed_at=UTC_MARKER,
        elapsed_ns=0,
        details={"clock_epoch_id": group.clock_epoch_id},
    )
    for slot in ("A", "B"):
        repository.set_stream_status(
            group.session_id, slot, StreamStatus.RECORDING,
            observed_at=UTC_MARKER, elapsed_ns=0,
        )
    repository.set_group_status(
        group.session_id, GroupStatus.RECORDING,
        observed_at=UTC_MARKER, elapsed_ns=0,
    )
    return group


def _insert_stream(
    repository: GroupedExperimentRepository,
    group,
    slot: str,
    fps: int,
    duration: int,
) -> int:
    stream = next(item for item in group.streams if item.slot == slot)
    total = fps * duration
    batch: list[MeasurementSample] = []
    previous_elapsed_ns: int | None = None
    for sequence in range(total):
        elapsed_ns = sequence * 1_000_000_000 // fps
        batch.append(MeasurementSample(
            benchmark_id=stream.benchmark_id,
            metric="frame_arrival",
            value=1.0,
            unit="frame",
            stream_scope=StreamScope.CAPTURE,
            observed_at=UTC_MARKER,
            elapsed_seconds=elapsed_ns / 1_000_000_000,
            sample_index=sequence,
            interval_ns=(elapsed_ns - previous_elapsed_ns) if previous_elapsed_ns is not None else None,
            method="synthetic_host_arrival_v1",
            elapsed_ns=elapsed_ns,
            source_timestamp_ns=None,
            source_sequence=None,
            source_clock=None,
        ))
        previous_elapsed_ns = elapsed_ns
        if len(batch) == BATCH_SIZE:
            repository.record_stream_measurements(group.session_id, slot, batch)
            batch.clear()
    if batch:
        repository.record_stream_measurements(group.session_id, slot, batch)
    return total


def _measure_analysis_stages(database: Database, session_id: str, model) -> dict[str, object]:
    """Measure read phases independently after the uncontaminated load timings."""
    repository = GroupedExperimentRepository(database)
    started = time.perf_counter()
    with database.connect_readonly() as connection:
        group = repository.read_group_snapshot(connection, session_id)
    connection_snapshot_ms = (time.perf_counter() - started) * 1000
    if group is None:
        raise RuntimeError("synthetic grouped snapshot disappeared during profiling")

    per_stream_scan_ms: dict[str, float] = {}
    scanned_rows: dict[str, int] = {}
    percentile_started = time.perf_counter()
    with database.connect_readonly() as connection:
        for stream in group.streams:
            scan_started = time.perf_counter()
            cursor = connection.cursor()
            cursor.row_factory = None
            cursor.execute(
                """SELECT sample_index, elapsed_ns, elapsed_seconds, interval_ns
                   FROM measurements
                   WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
                   ORDER BY sample_index,id""",
                (stream.benchmark_id,),
            )
            count = 0
            for _sample_index, _elapsed_ns, _elapsed_seconds, _interval_ns in cursor:
                count += 1
            per_stream_scan_ms[stream.slot] = round((time.perf_counter() - scan_started) * 1000, 3)
            scanned_rows[stream.slot] = count
        percentile_started = time.perf_counter()
        for stream in group.streams:
            valid_count = model.stream(stream.slot).interval_stats.count
            _exact_interval_percentiles(connection, stream.benchmark_id, valid_count)
        percentile_ms = round((time.perf_counter() - percentile_started) * 1000, 3)

    recovery_started = time.perf_counter()
    database.inspect_recording_recovery()
    recovery_ms = round((time.perf_counter() - recovery_started) * 1000, 3)

    alignment_started = time.perf_counter()
    for _ in range(1000):
        _shared_time_supports(group, model.streams)
    alignment_1000_ms = round((time.perf_counter() - alignment_started) * 1000, 3)
    return {
        "connect_and_group_snapshot_ms": round(connection_snapshot_ms, 3),
        "measurement_scan_and_tuple_materialization_ms_by_stream": per_stream_scan_ms,
        "scanned_rows_by_stream": scanned_rows,
        "exact_percentile_sql_ms_both_streams": percentile_ms,
        "read_only_recovery_inspection_ms": recovery_ms,
        "time_alignment_metadata_1000_calls_ms": alignment_1000_ms,
        "note": "phase probes are separate warm-cache passes and are not additive to full load time",
    }


def run_scenario(
    duration: int,
    fps_a: int,
    fps_b: int,
    *,
    repeats: int = 3,
    measure_memory: bool = False,
    profile: bool = False,
    measure_offscreen_gui: bool = False,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cel-grouped-analysis-bench-") as directory:
        path = Path(directory) / "synthetic-v3.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA application_id = {SYNTHETIC_DATABASE_APPLICATION_ID}")
            connection.execute("PRAGMA user_version = 0")
        database = Database(path)
        migrate_synthetic_database_to_v3(database)
        repository = GroupedExperimentRepository(database)
        group = _build_group(repository, duration, fps_a, fps_b)
        started = time.perf_counter()
        rows_a = _insert_stream(repository, group, "A", fps_a, duration)
        rows_b = _insert_stream(repository, group, "B", fps_b, duration)
        insert_seconds = time.perf_counter() - started

        for slot in ("A", "B"):
            repository.set_stream_status(
                group.session_id, slot, StreamStatus.COMPLETED,
                observed_at=UTC_MARKER, elapsed_ns=duration * 1_000_000_000,
            )
        repository.finalize_group(
            group.session_id,
            GroupStatus.COMPLETED,
            ended_at=UTC_MARKER,
            duration_by_slot={"A": float(duration), "B": float(duration)},
            termination_reason="synthetic_duration_elapsed",
        )
        database_bytes = path.stat().st_size
        analysis_runs: list[float] = []
        model = None
        for _ in range(repeats):
            started = time.perf_counter()
            model = load_grouped_analysis(database, group.session_id)
            analysis_runs.append(time.perf_counter() - started)
        analysis_seconds = statistics.median(analysis_runs)

        analysis_python_peak_kib = None
        if measure_memory:
            # Isolate allocation tracing from all timed runs. tracemalloc can
            # dominate wall time in allocation-heavy Python loops.
            tracemalloc.start()
            tracemalloc.reset_peak()
            try:
                measured_model = load_grouped_analysis(database, group.session_id)
                _current, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            if sum(item.persisted_frame_samples for item in measured_model.streams) != rows_a + rows_b:
                raise RuntimeError("memory-profile pass returned an inconsistent row count")
            analysis_python_peak_kib = round(peak_bytes / 1024)

        stage_probes = _measure_analysis_stages(database, group.session_id, model) if profile else None
        profile_summary = None
        if profile:
            profiler = cProfile.Profile()
            profiler.enable()
            load_grouped_analysis(database, group.session_id)
            profiler.disable()
            report = io.StringIO()
            pstats.Stats(profiler, stream=report).strip_dirs().sort_stats("cumulative").print_stats(18)
            profile_summary = report.getvalue()

        gui_timings = None
        if measure_offscreen_gui:
            from PySide6.QtWidgets import QApplication
            from camera_exploration_lab.ui.synthetic_grouped_explorer import GroupedExperimentExplorerDialog

            app = QApplication.instance() or QApplication([])
            construct_started = time.perf_counter()
            dialog = GroupedExperimentExplorerDialog(model)
            construct_seconds = time.perf_counter() - construct_started
            display_started = time.perf_counter()
            dialog.show()
            app.processEvents()
            first_display_seconds = time.perf_counter() - display_started
            dialog.close()
            app.processEvents()
            gui_timings = {
                "plot_widget_construction_ms": round(construct_seconds * 1000, 3),
                "offscreen_first_show_and_process_events_ms": round(first_display_seconds * 1000, 3),
                "note": "offscreen Qt timings; not desktop frame-rate measurements",
            }

        with database.connect_readonly() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "synthetic_only": True,
            "duration_seconds": duration,
            "stream_a_events_per_second": fps_a,
            "stream_b_events_per_second": fps_b,
            "rows": {"A": rows_a, "B": rows_b, "total": rows_a + rows_b},
            "database_bytes": database_bytes,
            "insert_seconds": round(insert_seconds, 3),
            "insert_rows_per_second": round((rows_a + rows_b) / insert_seconds, 1),
            "analysis_load_seconds_median": round(analysis_seconds, 3),
            "analysis_load_runs_seconds": [round(value, 3) for value in analysis_runs],
            "analysis_python_peak_kib": analysis_python_peak_kib,
            "analysis_profile_top_cumulative": profile_summary,
            "analysis_stage_probes": stage_probes,
            "offscreen_gui_timings": gui_timings,
            "analysis_row_counts": {
                "A": model.stream("A").persisted_frame_samples,
                "B": model.stream("B").persisted_frame_samples,
            },
            "plot_points": {
                "fps_a": len(model.stream("A").fps_points_shared),
                "fps_b": len(model.stream("B").fps_points_shared),
                "interval_a": len(model.stream("A").interval_points_shared),
                "interval_b": len(model.stream("B").interval_points_shared),
            },
            "statistics_interval_counts": {
                "A": model.stream("A").interval_stats.count,
                "B": model.stream("B").interval_stats.count,
            },
            "sqlite_integrity": integrity,
            "foreign_key_issues": len(foreign_key_issues),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--measure-memory", action="store_true",
                        help="run one separate traced Analysis pass for the largest synthetic case")
    parser.add_argument("--profile-largest", action="store_true",
                        help="print a separate cProfile report for the largest synthetic case")
    parser.add_argument("--offscreen-gui-largest", action="store_true",
                        help="measure Qt plot construction/show under the offscreen platform")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if not args.synthetic_only:
        parser.error("--synthetic-only is required")
    print("Synthetic timing rows only; no camera or user database is opened.", flush=True)
    for duration, fps_a, fps_b in SCENARIOS:
        is_largest = duration == 1800 and fps_a == 187 and fps_b == 30
        print(json.dumps(run_scenario(
            duration,
            fps_a,
            fps_b,
            repeats=args.repeats,
            measure_memory=args.measure_memory and is_largest,
            profile=args.profile_largest and is_largest,
            measure_offscreen_gui=args.offscreen_gui_largest and is_largest,
        ), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
