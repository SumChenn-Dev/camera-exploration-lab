#!/usr/bin/env python3
"""Measure bounded grouped-writer behavior using synthetic SQLite data only."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from camera_exploration_lab.db import Database  # noqa: E402
from camera_exploration_lab.grouped_recording import (  # noqa: E402
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
)
from camera_exploration_lab.grouped_recording_coordinator import (  # noqa: E402
    GroupedRecordingCoordinator,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity  # noqa: E402
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3  # noqa: E402
from grouped_test_support import create_synthetic_database  # noqa: E402


class SyntheticClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


def stream_specs(rate_b: int) -> tuple[GroupedStreamSpec, GroupedStreamSpec]:
    return (
        GroupedStreamSpec(
            slot="A",
            camera=CameraIdentity(
                "synthetic:eye-profile", BackendKind.V4L2, "synthetic-endpoint-a",
                "Synthetic", "Synthetic High Rate Camera", None, "ffff", "0001", "fixture", {},
            ),
            endpoint_key="synthetic-endpoint-a",
            benchmark_label="320x240 GRBG synthetic profile",
            configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": 187},
        ),
        GroupedStreamSpec(
            slot="B",
            camera=CameraIdentity(
                "synthetic:wide-profile", BackendKind.V4L2, "synthetic-endpoint-b",
                "Synthetic", "Synthetic Wide Camera", None, "ffff", "0002", "fixture", {},
            ),
            endpoint_key="synthetic-endpoint-b",
            benchmark_label="1280x720 MJPEG synthetic profile",
            configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": rate_b},
        ),
    )


def _producer(
    slot: str,
    fps: int,
    total: int,
    coordinator: GroupedRecordingCoordinator,
    epoch_ns: int,
    start_offset_ns: int,
    chunk_size: int,
    failures: list[str],
) -> None:
    produced = 0
    while produced < total:
        target = min(total, produced + chunk_size)
        for index in range(produced, target):
            host_ns = epoch_ns + start_offset_ns + index * 1_000_000_000 // fps
            result = coordinator.submit_frame_arrival(slot, host_monotonic_ns=host_ns)
            if not result.accepted:
                failures.append(f"{slot}: {result.reason}")
                return
        produced = target
        if not coordinator.wait_for_committed(slot, produced, timeout=30):
            failures.append(f"{slot}: writer did not commit {produced} synthetic rows")
            return


def run_case(duration_seconds: int, batch_size: int, rate_b: int = 24) -> dict[str, object]:
    epoch_ns = 50_000_000_000
    clock = SyntheticClock(epoch_ns)
    with tempfile.TemporaryDirectory(prefix="cel-synthetic-writer-") as temp_root:
        database_path = Path(temp_root) / "synthetic-v3.sqlite3"
        database, _fixture_report = create_synthetic_database(
            database_path, version=2, seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(database)
        repository = GroupedExperimentRepository(database)
        group = repository.create_group(
            GroupedExperimentSpec(
                label="Synthetic writer load",
                notes="Generated timing rows; no camera or private experiment data.",
                streams=stream_specs(rate_b),
                duration_target_seconds=float(duration_seconds),
                record_requested_at="2026-04-05T06:07:08+00:00",
                created_at="2026-04-05T06:07:08+00:00",
                clock_epoch_id="synthetic-benchmark-clock",
            )
        )
        coordinator = GroupedRecordingCoordinator(
            repository,
            group.session_id,
            queue_capacity=max(2048, batch_size * 4),
            batch_size=batch_size,
            clock_ns=clock,
            now_iso=lambda: "2026-04-05T06:07:08+00:00",
        )
        count_a = 187 * duration_seconds
        count_b = rate_b * duration_seconds
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch_ns)
        offset_b = 5_000_000
        coordinator.start_stream("B", host_monotonic_ns=epoch_ns + offset_b)
        failures: list[str] = []
        tracemalloc.start()
        cpu_start = time.process_time()
        producer_start = time.perf_counter()
        threads = (
            threading.Thread(
                target=_producer,
                args=("A", 187, count_a, coordinator, epoch_ns, 0, 512, failures),
                name="synthetic-producer-A",
                daemon=True,
            ),
            threading.Thread(
                target=_producer,
                args=("B", rate_b, count_b, coordinator, epoch_ns, offset_b, 512, failures),
                name="synthetic-producer-B",
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        producer_seconds = time.perf_counter() - producer_start
        if any(thread.is_alive() for thread in threads):
            coordinator.cancel(timeout=2)
            raise TimeoutError("synthetic producer did not finish within 60 seconds")
        if failures:
            coordinator.cancel(timeout=2)
            raise RuntimeError("synthetic producer failure: " + "; ".join(failures))

        end_a = epoch_ns + duration_seconds * 1_000_000_000
        end_b = end_a + offset_b
        clock.set(end_b + 1)
        finalization_start = time.perf_counter()
        coordinator.stop_stream("A", host_monotonic_ns=end_a)
        coordinator.stop_stream("B", host_monotonic_ns=end_b)
        final_record = coordinator.finalize(reason_code="requested_duration_elapsed")
        finalization_seconds = time.perf_counter() - finalization_start
        process_cpu_seconds = time.process_time() - cpu_start
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        snapshot = coordinator.snapshot()
        total_rows = count_a + count_b
        query_start = time.perf_counter()
        with database.connect_readonly() as connection:
            retrieved_a = connection.execute(
                "SELECT elapsed_ns, interval_ns FROM measurements "
                "WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture' "
                "ORDER BY sample_index",
                (final_record.streams[0].benchmark_id,),
            ).fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            try:
                object_sizes = dict(connection.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
                ).fetchall())
            except sqlite3.OperationalError:
                object_sizes = {}
        query_seconds = time.perf_counter() - query_start
        history_query_start = time.perf_counter()
        with database.connect_readonly() as connection:
            history_rows = connection.execute(
                "SELECT g.session_id, s.label, g.status, COUNT(bs.benchmark_id) AS stream_count "
                "FROM group_recordings g JOIN sessions s ON s.id=g.session_id "
                "LEFT JOIN benchmark_streams bs ON bs.session_id=g.session_id "
                "WHERE g.session_id=? GROUP BY g.session_id",
                (group.session_id,),
            ).fetchall()
            history_plan = [
                str(row["detail"])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT g.session_id FROM group_recordings g "
                    "WHERE g.status=? ORDER BY g.session_id",
                    ("completed",),
                ).fetchall()
            ]
        history_query_seconds = time.perf_counter() - history_query_start
        group_query_start = time.perf_counter()
        loaded = repository.get_group(group.session_id)
        group_query_seconds = time.perf_counter() - group_query_start
        database_bytes = database_path.stat().st_size
        database.close()
        if final_record.status != "completed":
            raise RuntimeError(f"synthetic group unexpectedly finalized as {final_record.status}")
        if len(retrieved_a) != count_a or foreign_keys or integrity != "ok":
            raise RuntimeError("synthetic persistence integrity check failed")
        measurement_index_names = {
            "idx_measurements_benchmark_interval",
            "idx_measurements_benchmark_metric_sample",
            "idx_measurements_metric_scope_time",
            "sqlite_autoindex_measurements_1",
        }
        return {
            "synthetic_only": True,
            "duration_seconds": duration_seconds,
            "fps_a": 187,
            "fps_b": rate_b,
            "batch_size": batch_size,
            "queue_capacity_per_stream": max(2048, batch_size * 4),
            "rows_expected": total_rows,
            "rows_committed": sum(item.committed_samples for item in snapshot.streams),
            "rows_rejected": sum(item.queue_rejections for item in snapshot.streams),
            "queue_high_water_A_B": [item.queue_high_water for item in snapshot.streams],
            "queue_depth_at_finish_A_B": [item.queue_depth for item in snapshot.streams],
            "database_bytes": database_bytes,
            "sqlite_page_bytes": page_count * page_size,
            "measurement_table_bytes": object_sizes.get("measurements"),
            "measurement_index_bytes": sum(
                int(size) for name, size in object_sizes.items() if name in measurement_index_names
            ) if object_sizes else None,
            "enqueue_and_persist_seconds": round(producer_seconds, 6),
            "approx_persisted_events_per_second": round(total_rows / producer_seconds, 1),
            "finalization_seconds": round(finalization_seconds, 6),
            "group_query_seconds": round(group_query_seconds, 6),
            "history_query_seconds": round(history_query_seconds, 6),
            "history_rows": len(history_rows),
            "history_query_plan": history_plan,
            "analysis_query_seconds": round(query_seconds, 6),
            "analysis_rows_retrieved_A": len(retrieved_a),
            "writer_thread_cpu_seconds": round(snapshot.writer_cpu_ns / 1_000_000_000, 6),
            "process_cpu_seconds": round(process_cpu_seconds, 6),
            "python_peak_tracemalloc_bytes": peak_bytes,
            "foreign_key_violations": len(foreign_keys),
            "integrity_check": integrity,
            "group_status": final_record.status,
            "group_data_complete": bool(loaded and loaded.data_complete),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--durations",
        default="30,300,1800",
        help="synthetic elapsed durations in seconds (default: 30,300,1800)",
    )
    parser.add_argument(
        "--batch-sizes",
        default="32,64,128,256",
        help="batch sizes benchmarked at the shortest duration (default: 32,64,128,256)",
    )
    parser.add_argument("--rate-b", type=int, default=24, choices=(24, 30))
    args = parser.parse_args()
    durations = [int(value) for value in args.durations.split(",") if value]
    batches = [int(value) for value in args.batch_sizes.split(",") if value]
    if not durations or any(value <= 0 for value in durations):
        parser.error("durations must be positive integers")
    if not batches or any(value < 1 or value > 1024 for value in batches):
        parser.error("batch sizes must be between 1 and 1024")
    shortest = min(durations)
    cases = [(shortest, batch) for batch in batches]
    cases.extend((duration, DEFAULT_BATCH) for duration in durations if duration != shortest)
    for duration, batch in cases:
        print(json.dumps(run_case(duration, batch, args.rate_b), sort_keys=True), flush=True)
    return 0


DEFAULT_BATCH = 128


if __name__ == "__main__":
    raise SystemExit(main())
