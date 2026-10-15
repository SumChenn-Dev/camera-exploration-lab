#!/usr/bin/env python3
"""Synthetic SQLite size and query benchmark for the CEL grouped schema.

All databases are created in a temporary directory and removed on exit. This
measures Python/SQLite persistence only; it does not measure camera capture.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from camera_exploration_lab.db import Database
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

BATCH_SIZE = 512
UTC_MARKER = "2026-01-01T00:00:00+00:00"


def build_group(
    repository: GroupedExperimentRepository,
    duration: int,
    stream_b_fps: int,
    *,
    suffix: str = "primary",
):
    camera_a = CameraIdentity(
        f"synthetic:benchmark:{suffix}:stream-a", BackendKind.V4L2,
        manufacturer="Synthetic", model="Synthetic High Rate", identity_source="synthetic",
    )
    camera_b = CameraIdentity(
        f"synthetic:benchmark:{suffix}:stream-b", BackendKind.V4L2,
        manufacturer="Synthetic", model="Synthetic Standard Rate", identity_source="synthetic",
    )
    spec = GroupedExperimentSpec(
        label=f"Synthetic SQLite volume {duration}s / B{stream_b_fps} / {suffix}",
        duration_target_seconds=float(duration),
        record_requested_at=UTC_MARKER,
        created_at=UTC_MARKER,
        clock_epoch_id=f"synthetic-clock-{duration}-{stream_b_fps}-{suffix}",
        streams=(
            GroupedStreamSpec(
                slot="A", camera=camera_a, endpoint_key=f"synthetic-endpoint-{suffix}-a",
                benchmark_label=f"synthetic 320x240 GRBG {suffix}",
                configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "fps": 187},
            ),
            GroupedStreamSpec(
                slot="B", camera=camera_b, endpoint_key=f"synthetic-endpoint-{suffix}-b",
                benchmark_label=f"synthetic 1280x720 MJPEG {suffix}",
                configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "fps": stream_b_fps},
            ),
        ),
    )
    group = repository.create_group(spec)
    for stream in group.streams:
        repository.set_stream_status(
            group.session_id, stream.slot, StreamStatus.RECORDING,
            observed_at=UTC_MARKER, elapsed_ns=0,
        )
    repository.set_group_status(
        group.session_id, GroupStatus.RECORDING, observed_at=UTC_MARKER, elapsed_ns=0
    )
    return group


def populate_history_groups(repository: GroupedExperimentRepository, count: int) -> None:
    """Add metadata-only synthetic groups to exercise History query scaling."""
    for index in range(count):
        build_group(repository, 30, 24, suffix=f"history-{index:04d}")


def write_stream(
    repository: GroupedExperimentRepository,
    session_id: str,
    slot: str,
    benchmark_id: str,
    fps: int,
    duration: int,
) -> int:
    count = fps * duration
    batch: list[MeasurementSample] = []
    previous_elapsed = None
    for sequence in range(count):
        elapsed_ns = sequence * 1_000_000_000 // fps
        interval_ns = (
            elapsed_ns - previous_elapsed if previous_elapsed is not None else None
        )
        batch.append(
            MeasurementSample(
                benchmark_id=benchmark_id,
                metric="frame_arrival",
                value=float(fps),
                unit="frames/s",
                stream_scope=StreamScope.CAPTURE,
                observed_at=UTC_MARKER,
                elapsed_seconds=elapsed_ns / 1_000_000_000,
                sample_index=sequence,
                interval_ns=interval_ns,
                method="synthetic_monotonic_read_return",
                elapsed_ns=elapsed_ns,
                # The current OpenCV source supplies no driver clock/sequence.
                source_timestamp_ns=None,
                source_sequence=None,
                source_clock=None,
            )
        )
        previous_elapsed = elapsed_ns
        if len(batch) == BATCH_SIZE:
            repository.record_stream_measurements(session_id, slot, batch)
            batch.clear()
    if batch:
        repository.record_stream_measurements(session_id, slot, batch)
    return count


def run_scenario(duration: int, stream_b_fps: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cel-grouped-sqlite-bench-") as directory:
        path = Path(directory) / "synthetic-history.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                f"PRAGMA application_id = {SYNTHETIC_DATABASE_APPLICATION_ID}"
            )
            connection.execute("PRAGMA user_version = 0")
        database = Database(path)
        migrate_synthetic_database_to_v3(database)
        repository = GroupedExperimentRepository(database)
        group = build_group(repository, duration, stream_b_fps)
        started = time.perf_counter()
        rows_a = write_stream(
            repository, group.session_id, "A", group.streams[0].benchmark_id, 187, duration
        )
        rows_b = write_stream(
            repository, group.session_id, "B", group.streams[1].benchmark_id, stream_b_fps, duration
        )
        write_seconds = time.perf_counter() - started

        for stream in group.streams:
            repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at=UTC_MARKER, elapsed_ns=duration * 1_000_000_000,
            )
        repository.finalize_group(
            group.session_id, GroupStatus.COMPLETED,
            ended_at=UTC_MARKER,
            duration_by_slot={"A": float(duration), "B": float(duration)},
            termination_reason="synthetic_duration_elapsed",
        )
        db_bytes = path.stat().st_size

        # Keep size reporting for the measurement-bearing group separate from
        # metadata-only rows used to exercise History list scaling.
        history_group_count = 100
        populate_history_groups(repository, history_group_count)
        started = time.perf_counter()
        fetched_group = repository.get_group(group.session_id)
        group_lookup_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        listed_groups = repository.list_groups(limit=1000)
        grouped_history_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        camera_filtered_groups = repository.list_groups(
            camera_id="synthetic:benchmark:primary:stream-a", limit=1000
        )
        camera_filter_query_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        legacy_history = database.history_entries()
        history_query_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        retrieved = 0
        with database.connect_readonly() as connection:
            cursor = connection.execute(
                """SELECT elapsed_ns, interval_ns
                   FROM measurements
                   WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
                   ORDER BY sample_index""",
                (group.streams[0].benchmark_id,),
            )
            for _row in cursor:
                retrieved += 1
        analysis_sample_query_ms = (time.perf_counter() - started) * 1000

        with database.connect_readonly() as connection:
            measurement_plan = [
                str(row["detail"]) for row in connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT elapsed_ns, interval_ns FROM measurements
                       WHERE benchmark_id=? AND metric='frame_arrival'
                         AND stream_scope='capture' ORDER BY sample_index""",
                    (group.streams[0].benchmark_id,),
                )
            ]
            camera_plan = [
                str(row["detail"]) for row in connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT session_id FROM benchmark_streams
                       WHERE camera_id=? ORDER BY session_id""",
                    ("synthetic:benchmark:primary:stream-a",),
                )
            ]
            event_plan = [
                str(row["detail"]) for row in connection.execute(
                    """EXPLAIN QUERY PLAN
                       SELECT event_id FROM experiment_events
                       WHERE session_id=? ORDER BY elapsed_ns, event_id""",
                    (group.session_id,),
                )
            ]

        total_rows = rows_a + rows_b
        return {
            "duration_seconds": duration,
            "stream_a_fps": 187,
            "stream_b_fps": stream_b_fps,
            "measurement_rows": total_rows,
            "database_bytes": db_bytes,
            "theoretical_150_300_bytes_per_row": [total_rows * 150, total_rows * 300],
            "insert_seconds": round(write_seconds, 3),
            "insert_rows_per_second": round(total_rows / write_seconds, 1),
            "group_lookup_ms": round(group_lookup_ms, 3),
            "grouped_history_ms": round(grouped_history_ms, 3),
            "camera_filtered_history_ms": round(camera_filter_query_ms, 3),
            "legacy_history_query_ms": round(history_query_ms, 3),
            "analysis_sample_retrieval_ms": round(analysis_sample_query_ms, 3),
            "analysis_samples_retrieved": retrieved,
            "group_query_result_count": len(listed_groups),
            "camera_filtered_group_count": len(camera_filtered_groups),
            "metadata_only_history_groups": history_group_count,
            "legacy_history_row_count": len(legacy_history),
            "group_contains_two_streams": fetched_group is not None and len(fetched_group.streams) == 2,
            "measurement_query_plan": measurement_plan,
            "camera_filter_query_plan": camera_plan,
            "event_query_plan": event_plan,
            "batch_size": BATCH_SIZE,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--durations", default="30,300,1800")
    parser.add_argument("--stream-b-fps", default="24,30")
    args = parser.parse_args()
    durations = [int(item) for item in args.durations.split(",") if item]
    b_rates = [int(item) for item in args.stream_b_fps.split(",") if item]
    if not durations or any(value <= 0 for value in durations):
        parser.error("durations must be positive integers")
    if not b_rates or any(value <= 0 for value in b_rates):
        parser.error("stream-b-fps must be positive integers")
    print("Synthetic SQLite only; no physical camera or user database is used.", flush=True)
    for duration in durations:
        for b_fps in b_rates:
            print(json.dumps(run_scenario(duration, b_fps), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
