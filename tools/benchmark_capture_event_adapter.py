#!/usr/bin/env python3
"""Exercise the live capture-event adapter with synthetic data only.

This developer harness creates a fresh marked database in a private temporary
directory. It accepts no database path and has no OpenCV/device import.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from camera_exploration_lab.capture import CaptureSnapshot, CaptureState, CaptureTimingEvent  # noqa: E402
from camera_exploration_lab.capture_modes import CaptureMode  # noqa: E402
from camera_exploration_lab.capture_recording_adapter import GroupedCaptureRecordingBridge  # noqa: E402
from camera_exploration_lab.db import Database  # noqa: E402
from camera_exploration_lab.grouped_recording import (  # noqa: E402
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
)
from camera_exploration_lab.grouped_recording_coordinator import (  # noqa: E402
    CoordinatorState,
    GroupedRecordingCoordinator,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity  # noqa: E402
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3  # noqa: E402
from grouped_test_support import create_synthetic_database  # noqa: E402


SYNTHETIC_MARKER = "CEL_SYNTHETIC_ADAPTER_ONLY_V1"
SYNTHETIC_MARKER_NAME = ".cel-synthetic-only"
WALL_TIME = "2026-06-07T08:09:10+00:00"


class SyntheticClock:
    def __init__(self, value: int = 100_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


class _SyntheticSubscription:
    def __init__(self, worker: "SyntheticCaptureSource", callback) -> None:
        self.worker = worker
        self.callback = callback
        self.closed = False

    def close(self) -> bool:
        if self.closed:
            return False
        self.closed = True
        if self.worker.listener is self.callback:
            self.worker.listener = None
        return True


class SyntheticCaptureSource:
    """Timing-only fake worker. It never opens a camera or allocates frames."""

    def __init__(self) -> None:
        self.listener = None
        self.state = CaptureState.RUNNING
        self.mode = CaptureMode("synthetic", 320, 240, 30, "SYN")

    def snapshot(self) -> CaptureSnapshot:
        return CaptureSnapshot(state=self.state, requested=self.mode)

    def subscribe_timing_listener(self, callback):
        if self.listener is not None:
            raise RuntimeError("synthetic listener slot already in use")
        self.listener = callback
        return _SyntheticSubscription(self, callback)

    def emit(self, event: CaptureTimingEvent) -> None:
        listener = self.listener
        if listener is not None:
            listener(event)


def _synthetic_database(root: Path) -> tuple[Database, Path]:
    """Create a fresh marked fixture; callers cannot supply or reuse a path."""
    marker = root / SYNTHETIC_MARKER_NAME
    database_path = root / "synthetic-grouped.sqlite3"
    if marker.exists() or database_path.exists():
        raise RuntimeError("synthetic harness directory must be fresh")
    marker.write_text(SYNTHETIC_MARKER + "\n", encoding="ascii")
    if marker.read_text(encoding="ascii").strip() != SYNTHETIC_MARKER:
        raise RuntimeError("synthetic marker validation failed")
    database, _fixture = create_synthetic_database(
        database_path, version=0, seed_legacy_data=False
    )
    migrate_synthetic_database_to_v3(database)
    return database, database_path


def _stream_specs(rate_a: int, rate_b: int) -> tuple[GroupedStreamSpec, GroupedStreamSpec]:
    return (
        GroupedStreamSpec(
            slot="A",
            camera=CameraIdentity(
                "synthetic:adapter-high-rate", BackendKind.V4L2, "synthetic-endpoint-a",
                "Synthetic", "Synthetic High Rate Camera", None, "ffff", "0001", "fixture", {},
            ),
            endpoint_key="synthetic-endpoint-a",
            benchmark_label="Synthetic 320x240 GRBG",
            configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": rate_a},
        ),
        GroupedStreamSpec(
            slot="B",
            camera=CameraIdentity(
                "synthetic:adapter-wide", BackendKind.V4L2, "synthetic-endpoint-b",
                "Synthetic", "Synthetic Wide Camera", None, "ffff", "0002", "fixture", {},
            ),
            endpoint_key="synthetic-endpoint-b",
            benchmark_label="Synthetic 1280x720 MJPEG",
            configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": rate_b},
        ),
    )


def _make_recording(
    root: Path,
    rate_a: int,
    rate_b: int,
    seconds: int,
    *,
    queue_capacity: int = 2048,
    batch_size: int = 128,
):
    database, database_path = _synthetic_database(root)
    repository = GroupedExperimentRepository(database)
    clock = SyntheticClock()
    group = repository.create_group(GroupedExperimentSpec(
        label="Synthetic capture adapter benchmark",
        notes="Accelerated timing events; no video or camera data.",
        streams=_stream_specs(rate_a, rate_b),
        duration_target_seconds=float(seconds),
        record_requested_at=WALL_TIME,
        created_at=WALL_TIME,
        clock_epoch_id=f"synthetic-adapter-{rate_b}-{seconds}",
    ))
    coordinator = GroupedRecordingCoordinator(
        repository,
        group.session_id,
        queue_capacity=queue_capacity,
        batch_size=batch_size,
        clock_ns=clock,
        now_iso=lambda: WALL_TIME,
    )
    sources = {"A": SyntheticCaptureSource(), "B": SyntheticCaptureSource()}
    bridge = GroupedCaptureRecordingBridge(coordinator, clock_ns=clock, now_iso=lambda: WALL_TIME)
    report = bridge.start_recording(sources)
    if not report.fully_started:
        bridge.shutdown(timeout=2)
        raise RuntimeError("synthetic sources did not both start")
    return database, database_path, repository, clock, coordinator, bridge, sources


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def run_rate_case(rate_a: int, rate_b: int, seconds: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cel-synthetic-adapter-") as temp_root:
        root = Path(temp_root)
        database, database_path, repository, clock, coordinator, bridge, sources = _make_recording(
            root, rate_a, rate_b, seconds
        )
        epoch = clock.value
        offset_b = 5_000_000
        total_a = rate_a * seconds
        total_b = rate_b * seconds
        index_a = index_b = 0
        emitted = {"A": 0, "B": 0}
        callback_ns: list[int] = []
        callback_total_ns = 0
        total_events = total_a + total_b
        chunk_limit = 512
        tracemalloc.start()
        process_cpu_start = time.process_time()
        run_start = time.perf_counter()
        next_a = 0
        next_b = offset_b
        try:
            while index_a < total_a or index_b < total_b:
                if index_a < total_a and (index_b >= total_b or next_a <= next_b):
                    slot, rate, host_ns = "A", rate_a, epoch + next_a
                    index_a += 1
                    next_a = index_a * 1_000_000_000 // rate_a
                else:
                    slot, rate, host_ns = "B", rate_b, epoch + next_b
                    index_b += 1
                    next_b = offset_b + index_b * 1_000_000_000 // rate_b
                event = CaptureTimingEvent(
                    "frame_arrival", host_ns, WALL_TIME,
                    interval_ns=1_000_000_000 // rate,
                    capture_fps=float(rate),
                )
                start_ns = time.perf_counter_ns()
                sources[slot].emit(event)
                duration_ns = time.perf_counter_ns() - start_ns
                callback_total_ns += duration_ns
                if len(callback_ns) < 8192:
                    callback_ns.append(duration_ns)
                emitted[slot] += 1
                if sum(emitted.values()) % chunk_limit == 0:
                    for stream_slot in ("A", "B"):
                        if not coordinator.wait_for_committed(stream_slot, emitted[stream_slot], timeout=10):
                            raise RuntimeError(f"writer failed to commit synthetic stream {stream_slot}")

            for stream_slot in ("A", "B"):
                if not coordinator.wait_for_committed(stream_slot, emitted[stream_slot], timeout=10):
                    raise RuntimeError(f"writer failed to drain synthetic stream {stream_slot}")
            clock.set(epoch + seconds * 1_000_000_000)
            finalize_start = time.perf_counter()
            record = bridge.stop_recording(reason_code="requested_duration_elapsed", timeout=10)
            finalize_seconds = time.perf_counter() - finalize_start
            run_seconds = time.perf_counter() - run_start
            process_cpu_seconds = time.process_time() - process_cpu_start
            _current, peak_bytes = tracemalloc.get_traced_memory()
            snapshot = coordinator.snapshot()
            with database.connect_readonly() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                frame_rows = int(connection.execute(
                    "SELECT COUNT(*) FROM measurements WHERE metric='frame_arrival'"
                ).fetchone()[0])
            if str(record.status) != "completed" or frame_rows != total_events:
                raise RuntimeError("synthetic rate case did not persist a complete event set")
            if any(item.queue_rejections for item in snapshot.streams):
                raise RuntimeError("normal-rate synthetic workload unexpectedly overflowed a queue")
            return {
                "scenario": f"{rate_a}+{rate_b}",
                "simulated_seconds": seconds,
                "events_expected": total_events,
                "events_committed": frame_rows,
                "events_rejected": sum(item.queue_rejections + item.persistence_losses for item in snapshot.streams),
                "callback_mean_us": round(callback_total_ns / max(1, total_events) / 1000, 3),
                "callback_p50_us": round(_percentile(callback_ns, 0.50) / 1000, 3),
                "callback_p95_us": round(_percentile(callback_ns, 0.95) / 1000, 3),
                "callback_p99_us": round(_percentile(callback_ns, 0.99) / 1000, 3),
                "callback_max_us": round(max(callback_ns, default=0) / 1000, 3),
                "wall_elapsed_seconds": round(run_seconds, 4),
                "persisted_events_per_wall_second": round(frame_rows / max(run_seconds, 1e-9), 1),
                "finalization_seconds": round(finalize_seconds, 5),
                "writer_cpu_seconds": round(snapshot.writer_cpu_ns / 1_000_000_000, 5),
                "process_cpu_seconds": round(process_cpu_seconds, 5),
                "python_traced_peak_bytes": peak_bytes,
                "queue_high_water": {item.slot: item.queue_high_water for item in snapshot.streams},
                "queue_capacity_each": snapshot.streams[0].queue_capacity,
                "sqlite_size_bytes": database_path.stat().st_size,
                "sqlite_integrity": integrity,
                "foreign_key_violations": foreign_keys,
                "status": str(record.status),
            }
        finally:
            tracemalloc.stop()
            if coordinator.snapshot().state is CoordinatorState.RUNNING:
                bridge.shutdown(timeout=2)


def run_overload_case() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cel-synthetic-adapter-overload-") as temp_root:
        root = Path(temp_root)
        database, _database_path, repository, clock, coordinator, bridge, sources = _make_recording(
            root, 187, 24, 30, queue_capacity=64, batch_size=16
        )
        entered = threading.Event()
        release = threading.Event()
        original_write = repository.record_stream_measurements
        first = True

        def temporarily_slow(session_id, slot, samples, *, owner_token=None):
            nonlocal first
            if first:
                first = False
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("synthetic overload gate was not released")
            return original_write(session_id, slot, samples, owner_token=owner_token)

        repository.record_stream_measurements = temporarily_slow
        epoch = clock.value
        source_a, source_b = sources["A"], sources["B"]
        durations: list[int] = []
        start_ns = time.perf_counter_ns()
        source_a.emit(CaptureTimingEvent("frame_arrival", epoch + 1, WALL_TIME))
        if not entered.wait(2):
            raise RuntimeError("synthetic writer did not enter the controlled overload gate")
        for index in range(200):
            before = time.perf_counter_ns()
            source_a.emit(CaptureTimingEvent("frame_arrival", epoch + 2 + (index + 1) * 1_000_000, WALL_TIME))
            if len(durations) < 8192:
                durations.append(time.perf_counter_ns() - before)
        for index in range(10):
            source_b.emit(CaptureTimingEvent("frame_arrival", epoch + 3 + index * 1_000_000, WALL_TIME))
        full_snapshot = coordinator.snapshot()
        release.set()
        for item in full_snapshot.streams:
            if not coordinator.wait_for_committed(item.slot, item.accepted_samples, timeout=5):
                raise RuntimeError("writer did not recover after the synthetic slow period")
        before_later = next(item.accepted_samples for item in full_snapshot.streams if item.slot == "A")
        source_a.emit(CaptureTimingEvent("frame_arrival", epoch + 500_000_000, WALL_TIME))
        snapshot = coordinator.snapshot()
        later_a_sample_accepted = next(
            item.accepted_samples for item in snapshot.streams if item.slot == "A"
        ) > before_later
        if not coordinator.wait_for_committed("A", snapshot.streams[0].accepted_samples, timeout=5):
            raise RuntimeError("stream A did not resume after queue space became available")
        clock.set(epoch + 1_000_000_000)
        record = bridge.stop_recording(reason_code="synthetic_overload_stop", timeout=5)
        final_snapshot = coordinator.snapshot()
        elapsed = time.perf_counter_ns() - start_ns
        return {
            "scenario": "A_burst_overload_B_normal",
            "burst_events_a": 201,
            "steady_events_b": 10,
            "stream_a_queue_rejections": final_snapshot.streams[0].queue_rejections,
            "stream_b_queue_rejections": final_snapshot.streams[1].queue_rejections,
            "stream_a_high_water": final_snapshot.streams[0].queue_high_water,
            "stream_b_high_water": final_snapshot.streams[1].queue_high_water,
            "queue_capacity_each": final_snapshot.streams[0].queue_capacity,
            "a_callback_p95_us": round(_percentile(durations, 0.95) / 1000, 3),
            "submission_burst_seconds": round(elapsed / 1_000_000_000, 5),
            "later_a_sample_accepted": later_a_sample_accepted,
            "status": str(record.status),
            "data_complete": all(stream.data_complete for stream in record.streams),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic-only", action="store_true", required=True,
        help="required explicit acknowledgement; this tool never opens cameras",
    )
    parser.add_argument("--seconds", type=int, choices=(1, 30), default=30,
                        help="accelerated elapsed-time span for each nominal-rate case")
    args = parser.parse_args(argv)
    _ = args.synthetic_only
    results = [
        run_rate_case(100, 24, args.seconds),
        run_rate_case(187, 24, args.seconds),
        run_rate_case(187, 30, args.seconds),
        run_overload_case(),
    ]
    print(json.dumps({
        "synthetic_only": True,
        "database_policy": "fresh marked temporary database; no path argument or default DB fallback",
        "capture_hardware_accessed": False,
        "results": results,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
