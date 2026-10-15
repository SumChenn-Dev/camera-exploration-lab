from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from camera_exploration_lab.capture import CaptureSnapshot, CaptureState, CaptureTimingEvent
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.capture_recording_adapter import (
    AdapterShutdownTimeout,
    AdapterState,
    BridgeState,
    CaptureAdapterError,
    GroupedCaptureRecordingBridge,
    GroupedRecordingPersistenceError,
)
from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedRepositoryError,
    GroupedStreamSpec,
    RecordingLeaseBusyError,
)
from camera_exploration_lab.grouped_recording_coordinator import (
    CoordinatorState,
    GroupedRecordingCoordinator,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3
from grouped_test_support import create_synthetic_database


class FakeClock:
    def __init__(self, value: int = 50_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


class FakeSubscription:
    def __init__(self, worker: "FakeTimingWorker", callback) -> None:
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


class FakeTimingWorker:
    """Synthetic capture source; it never opens OpenCV or a device node."""

    def __init__(self, state: CaptureState = CaptureState.RUNNING) -> None:
        self.state = state
        self.listener = None
        self.read_count = 0
        self.read_failure_count = 0
        self.preview_update_count = 0
        self._previous_ns: int | None = None

    def snapshot(self) -> CaptureSnapshot:
        return CaptureSnapshot(
            state=self.state,
            requested=CaptureMode("synthetic", 320, 240, 30, "SYN"),
        )

    def subscribe_timing_listener(self, listener):
        if self.listener is not None:
            raise RuntimeError("synthetic listener slot is already occupied")
        self.listener = listener
        return FakeSubscription(self, listener)

    def emit_frame(self, timestamp_ns: int, *, capture_fps: float = 30.0) -> None:
        self.read_count += 1
        interval = None if self._previous_ns is None else timestamp_ns - self._previous_ns
        self._previous_ns = timestamp_ns
        callback = self.listener
        if callback is not None:
            callback(CaptureTimingEvent(
                "frame_arrival", timestamp_ns, "2026-05-06T07:08:09+00:00",
                interval_ns=interval, capture_fps=capture_fps,
            ))

    def emit_read_failure(self, timestamp_ns: int) -> None:
        self.read_failure_count += 1
        callback = self.listener
        if callback is not None:
            callback(CaptureTimingEvent(
                "read_failure", timestamp_ns, "2026-05-06T07:08:10+00:00",
            ))

    def emit_preview_update(self) -> None:
        self.preview_update_count += 1


class CaptureRecordingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="cel-synthetic-adapter-")
        self.db_path = Path(self.tempdir.name) / "synthetic-v3.sqlite3"
        self.database, _ = create_synthetic_database(
            self.db_path, version=0, seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(self.database)
        self.repository = GroupedExperimentRepository(self.database)
        self.clock = FakeClock()
        self.coordinators: list[GroupedRecordingCoordinator] = []
        self.bridges: list[GroupedCaptureRecordingBridge] = []

    def tearDown(self) -> None:
        for bridge in self.bridges:
            state, _adapters = bridge.snapshot()
            if state in {BridgeState.RECORDING, BridgeState.STARTING}:
                bridge.shutdown(timeout=2)
        for coordinator in self.coordinators:
            if coordinator.snapshot().state is CoordinatorState.RUNNING:
                coordinator.cancel(timeout=2)
            coordinator.wait_closed(timeout=2)
        self.tempdir.cleanup()

    def make_coordinator(
        self,
        *,
        duration: float | None = None,
        queue_capacity: int = 64,
        batch_size: int = 16,
    ) -> GroupedRecordingCoordinator:
        streams = (
            GroupedStreamSpec(
                slot="A",
                camera=CameraIdentity(
                    "synthetic:adapter-eye", BackendKind.V4L2, "synthetic-endpoint-a",
                    "Synthetic", "Synthetic High Rate Camera", None, "ffff", "0001", "fixture", {},
                ),
                endpoint_key="synthetic-endpoint-a",
                benchmark_label="Synthetic 320x240 GRBG",
                configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": 187},
            ),
            GroupedStreamSpec(
                slot="B",
                camera=CameraIdentity(
                    "synthetic:adapter-wide", BackendKind.V4L2, "synthetic-endpoint-b",
                    "Synthetic", "Synthetic Wide Camera", None, "ffff", "0002", "fixture", {},
                ),
                endpoint_key="synthetic-endpoint-b",
                benchmark_label="Synthetic 1280x720 MJPEG",
                configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": 24},
            ),
        )
        group = self.repository.create_group(GroupedExperimentSpec(
            label="Synthetic capture adapter run",
            notes="Fake timing events only.",
            streams=streams,
            duration_target_seconds=duration,
            record_requested_at="2026-05-06T07:08:00+00:00",
            created_at="2026-05-06T07:08:00+00:00",
            clock_epoch_id=f"synthetic-adapter-clock-{len(self.coordinators)}",
        ))
        coordinator = GroupedRecordingCoordinator(
            self.repository,
            group.session_id,
            queue_capacity=queue_capacity,
            batch_size=batch_size,
            clock_ns=self.clock,
            now_iso=lambda: "2026-05-06T07:08:00+00:00",
        )
        self.coordinators.append(coordinator)
        return coordinator

    def make_bridge(self, coordinator: GroupedRecordingCoordinator) -> GroupedCaptureRecordingBridge:
        bridge = GroupedCaptureRecordingBridge(
            coordinator,
            clock_ns=self.clock,
            now_iso=lambda: "2026-05-06T07:08:00+00:00",
        )
        self.bridges.append(bridge)
        return bridge

    def measurement_rows(self, metric: str = "frame_arrival"):
        with self.database.connect_readonly() as connection:
            return connection.execute(
                """SELECT benchmark_id, sample_index, elapsed_ns, elapsed_seconds,
                          interval_ns, source_timestamp_ns, source_sequence, source_clock
                   FROM measurements WHERE metric=? ORDER BY benchmark_id, sample_index""",
                (metric,),
            ).fetchall()

    def test_dual_adapter_persists_one_scalar_sample_per_arrival_with_independent_sequences(self) -> None:
        coordinator = self.make_coordinator(duration=1.0)
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()

        # Preview was already live. Those events are not retroactively recorded.
        worker_a.emit_frame(self.clock.value - 20_000_000)
        worker_b.emit_frame(self.clock.value - 40_000_000)
        report = bridge.start_recording({"A": worker_a, "B": worker_b})
        self.assertTrue(report.fully_started)

        epoch = self.clock.value
        for index in range(6):
            worker_a.emit_frame(epoch + 100_000_000 + index * 10_000_000, capture_fps=100.0)
            if index in {0, 2, 4}:
                worker_b.emit_frame(epoch + 150_000_000 + index * 40_000_000, capture_fps=24.0)
        worker_a.emit_read_failure(epoch + 250_000_000)
        worker_a.emit_preview_update()
        self.clock.set(epoch + 1_000_000_000)

        record = bridge.stop_recording(reason_code="requested_duration_elapsed")
        self.assertEqual(record.status, GroupStatus.COMPLETED)
        self.assertTrue(worker_a.snapshot().state is CaptureState.RUNNING)
        self.assertTrue(worker_b.snapshot().state is CaptureState.RUNNING)
        self.assertEqual(worker_a.read_count, 7)  # one pre-recording + six recorded arrivals
        self.assertEqual(worker_b.read_count, 4)  # one pre-recording + three recorded arrivals
        self.assertEqual(worker_a.preview_update_count, 1)

        rows = self.measurement_rows()
        run_a, run_b = (stream.benchmark_id for stream in record.streams)
        arrivals_a = [row for row in rows if row[0] == run_a]
        arrivals_b = [row for row in rows if row[0] == run_b]
        self.assertEqual([row[1] for row in arrivals_a], list(range(6)))
        self.assertEqual([row[1] for row in arrivals_b], list(range(3)))
        self.assertEqual([row[2] for row in arrivals_a], [100_000_000 + i * 10_000_000 for i in range(6)])
        self.assertEqual([row[2] for row in arrivals_b], [150_000_000 + i * 80_000_000 for i in range(3)])
        self.assertIsNone(arrivals_a[0][4])
        self.assertTrue(all(row[5:] == (None, None, None) for row in rows))
        failures = self.measurement_rows("read_failure")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], run_a)
        self.assertEqual(record.streams[0].read_failure_count, 1)

    def test_separate_capture_threads_submit_concurrently_with_independent_sequences(self) -> None:
        coordinator = self.make_coordinator(queue_capacity=1024, batch_size=32)
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        epoch = self.clock.value
        count = 250
        start_gate = threading.Barrier(3)
        failures: list[BaseException] = []

        def emit_series(worker: FakeTimingWorker, offset_ns: int, step_ns: int) -> None:
            try:
                start_gate.wait(timeout=2)
                for index in range(count):
                    worker.emit_frame(epoch + offset_ns + index * step_ns)
            except BaseException as exc:
                failures.append(exc)

        threads = (
            threading.Thread(target=emit_series, args=(worker_a, 10, 50_000)),
            threading.Thread(target=emit_series, args=(worker_b, 30, 80_000)),
        )
        for thread in threads:
            thread.start()
        start_gate.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertTrue(coordinator.wait_for_committed("A", count, timeout=2))
        self.assertTrue(coordinator.wait_for_committed("B", count, timeout=2))

        record = bridge.stop_recording()
        self.assertEqual(record.status, GroupStatus.PARTIAL)
        rows = self.measurement_rows()
        run_ids = {stream.slot: stream.benchmark_id for stream in record.streams}
        for slot in ("A", "B"):
            stream_rows = [row for row in rows if row[0] == run_ids[slot]]
            self.assertEqual([row[1] for row in stream_rows], list(range(count)))
        self.assertEqual(len(rows), count * 2)

    def test_stop_recording_leaves_preview_capture_running_and_rejects_stale_callback(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        epoch = self.clock.value
        worker_a.emit_frame(epoch + 10)
        worker_b.emit_frame(epoch + 20)
        stale = worker_a.listener
        record = bridge.stop_recording()
        self.assertTrue(worker_a.snapshot().state is CaptureState.RUNNING)
        self.assertTrue(worker_b.snapshot().state is CaptureState.RUNNING)
        before = len(self.measurement_rows())
        stale(CaptureTimingEvent("frame_arrival", epoch + 30, "2026-05-06T07:08:11+00:00"))
        worker_a.emit_frame(epoch + 40)
        worker_b.emit_frame(epoch + 50)
        self.assertEqual(len(self.measurement_rows()), before)
        state, adapter_snapshots = bridge.snapshot()
        self.assertEqual(state, BridgeState.FINALIZED)
        self.assertEqual(adapter_snapshots[0].state, AdapterState.DETACHED)
        self.assertEqual(adapter_snapshots[0].ignored_events, 1)
        self.assertEqual(bridge.stop_recording(), record)

    def test_one_unavailable_camera_is_partial_without_stopping_the_healthy_capture(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a = FakeTimingWorker()
        report = bridge.start_recording({"A": worker_a, "B": None})
        self.assertEqual(report.started_slots, ("A",))
        self.assertEqual(report.failed_slots, ("B",))
        worker_a.emit_frame(self.clock.value + 10)
        record = bridge.stop_recording()
        self.assertEqual(record.status, GroupStatus.PARTIAL)
        self.assertEqual(worker_a.snapshot().state, CaptureState.RUNNING)
        self.assertEqual(record.streams[1].stream_status, "failed")

    def test_failure_of_one_capture_does_not_stop_sibling_recording(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        epoch = self.clock.value
        worker_a.emit_frame(epoch + 1_000)
        worker_b.emit_frame(epoch + 2_000)
        state = bridge.capture_ended("A", failed=True, reason_code="synthetic_disconnect")
        self.assertIsNotNone(state)
        worker_a.state = CaptureState.UNAVAILABLE
        worker_b.emit_frame(epoch + 3_000)
        worker_b.emit_frame(epoch + 4_000)
        self.assertEqual(worker_b.listener is not None, True)
        record = bridge.stop_recording()
        self.assertEqual(record.status, GroupStatus.PARTIAL)
        rows_b = [row for row in self.measurement_rows() if row[0] == record.streams[1].benchmark_id]
        self.assertEqual(len(rows_b), 3)  # first arrival plus two after A failed

    def test_repeated_start_is_rejected_and_individual_stop_is_idempotent(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        with self.assertRaises(CaptureAdapterError):
            bridge.start_recording({"A": worker_a, "B": worker_b})
        state_a = bridge.capture_ended("A", failed=False, reason_code="slot_stopped")
        self.assertIsNotNone(state_a)
        self.assertEqual(bridge.capture_ended("A", failed=False, reason_code="slot_stopped"), state_a)
        worker_b.emit_frame(self.clock.value + 100)
        record = bridge.stop_recording()
        self.assertNotEqual(record.status, GroupStatus.COMPLETED)

    def test_shutdown_cancels_group_and_closes_adapter_before_stale_callback(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        worker_a.emit_frame(self.clock.value + 10)
        stale = worker_a.listener
        self.clock.set(self.clock.value + 100)
        self.assertTrue(bridge.shutdown(timeout=2))
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.FINALIZED)
        self.assertEqual(self.repository.get_group(coordinator.session_id).status, GroupStatus.INTERRUPTED)
        before = len(self.measurement_rows())
        stale(CaptureTimingEvent("frame_arrival", self.clock.value + 20, "2026-05-06T07:08:12+00:00"))
        self.assertEqual(len(self.measurement_rows()), before)

    def test_shutdown_before_first_frame_records_interruption(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        record = bridge.shutdown(timeout=2)
        self.assertTrue(record)
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.FINALIZED)
        group = self.repository.get_group(coordinator.session_id)
        self.assertEqual(group.status, GroupStatus.INTERRUPTED)
        self.assertEqual(
            sum(1 for row in self.measurement_rows() if row[0] in {
                stream.benchmark_id for stream in group.streams
            }),
            0,
        )

    def test_stop_waits_for_inflight_callback_before_finalizing(self) -> None:
        coordinator = self.make_coordinator(duration=1.0)
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})
        entered = threading.Event()
        release = threading.Event()
        # Seed B before blocking A's coordinator submission so the test gate is
        # scoped to one capture callback only.
        worker_b.emit_frame(self.clock.value + 20)
        original_submit = coordinator.submit_frame_arrival

        def blocked_submit(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("synthetic callback gate was not released")
            return original_submit(*args, **kwargs)

        coordinator.submit_frame_arrival = blocked_submit
        callback = threading.Thread(
            target=worker_a.emit_frame,
            args=(self.clock.value + 10,),
            name="synthetic-capture-callback",
        )
        callback.start()
        self.assertTrue(entered.wait(2))
        self.clock.set(self.clock.value + 1_000_000_000)
        with self.assertRaises(AdapterShutdownTimeout):
            bridge.stop_recording(timeout=0.01)
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.RUNNING)

        release.set()
        callback.join(timeout=2)
        self.assertFalse(callback.is_alive())
        record = bridge.stop_recording(
            reason_code="requested_duration_elapsed", timeout=2
        )
        self.assertEqual(record.status, GroupStatus.COMPLETED)
        self.assertEqual(len(self.measurement_rows()), 2)

    def test_writer_failure_is_terminal_and_never_reports_success(self) -> None:
        coordinator = self.make_coordinator()
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        bridge.start_recording({"A": worker_a, "B": worker_b})

        def fail_write(*_args, **_kwargs):
            raise sqlite3.OperationalError("database or disk is full")

        self.repository.record_stream_measurements = fail_write
        worker_a.emit_frame(self.clock.value + 10)
        self.assertTrue(coordinator.wait_closed(timeout=2))
        with self.assertRaises(GroupedRecordingPersistenceError):
            bridge.stop_recording()
        group = self.repository.get_group(coordinator.session_id)
        self.assertNotEqual(group.status, GroupStatus.COMPLETED)
        self.assertFalse(group.data_complete)
        self.assertIsNotNone(self.repository.get_recording_lease())
        self.assertTrue(bridge.shutdown(timeout=2))

    def test_same_process_lease_conflict_rejects_second_bridge_without_disturbing_first(self) -> None:
        first_coordinator = self.make_coordinator()
        first_bridge = self.make_bridge(first_coordinator)
        first_a, first_b = FakeTimingWorker(), FakeTimingWorker()
        first_bridge.start_recording({"A": first_a, "B": first_b})

        second_coordinator = self.make_coordinator()
        second_bridge = self.make_bridge(second_coordinator)
        second_a, second_b = FakeTimingWorker(), FakeTimingWorker()
        with self.assertRaises(RecordingLeaseBusyError):
            second_bridge.start_recording({"A": second_a, "B": second_b})
        self.assertEqual(second_bridge.snapshot()[0], BridgeState.FAILED)
        self.assertIsNone(second_a.listener)
        self.assertIsNone(second_b.listener)

        first_a.emit_frame(self.clock.value + 100)
        first_b.emit_frame(self.clock.value + 200)
        record = first_bridge.stop_recording()
        self.assertEqual(len([row for row in self.measurement_rows() if row[0] in {
            stream.benchmark_id for stream in record.streams
        }]), 2)

    def test_full_queue_rejects_without_blocking_and_later_samples_resume_with_gap(self) -> None:
        coordinator = self.make_coordinator(queue_capacity=1, batch_size=1)
        bridge = self.make_bridge(coordinator)
        worker_a, worker_b = FakeTimingWorker(), FakeTimingWorker()
        entered = threading.Event()
        release = threading.Event()
        original = self.repository.record_stream_measurements
        first = True

        def slow_write(session_id, slot, samples, *, owner_token=None):
            nonlocal first
            if first:
                first = False
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("synthetic writer gate was not released")
            return original(session_id, slot, samples, owner_token=owner_token)

        self.repository.record_stream_measurements = slow_write
        bridge.start_recording({"A": worker_a, "B": worker_b})
        epoch = self.clock.value
        worker_a.emit_frame(epoch + 1)
        self.assertTrue(entered.wait(2))
        worker_a.emit_frame(epoch + 2)
        callback_start = time.perf_counter()
        worker_a.emit_frame(epoch + 3)
        callback_elapsed = time.perf_counter() - callback_start
        worker_b.emit_frame(epoch + 4)
        self.assertLess(callback_elapsed, 0.1)
        a_snapshot = next(item for item in bridge.snapshot()[1] if item.slot == "A")
        self.assertEqual(a_snapshot.rejected_submissions, 1)
        release.set()
        self.assertTrue(coordinator.wait_for_committed("A", 2, timeout=2))
        self.assertTrue(coordinator.wait_for_committed("B", 1, timeout=2))
        worker_a.emit_frame(epoch + 5)
        self.assertTrue(coordinator.wait_for_committed("A", 3, timeout=2))
        self.clock.set(epoch + 1_000_000)
        record = bridge.stop_recording()
        self.assertEqual(record.status, GroupStatus.PARTIAL)
        a_rows = [row for row in self.measurement_rows() if row[0] == record.streams[0].benchmark_id]
        self.assertEqual([row[1] for row in a_rows], [0, 1, 3])
        self.assertEqual(record.streams[0].timing_queue_loss_count, 1)
        self.assertEqual(record.streams[1].timing_queue_loss_count, 0)

    def test_v2_startup_remains_v2_and_grouped_repository_cannot_use_it(self) -> None:
        path = Path(self.tempdir.name) / "ordinary-synthetic-v2.sqlite3"
        database = Database(path)
        database.initialize()
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        repository = GroupedExperimentRepository(database)
        with self.assertRaises(GroupedRepositoryError):
            repository.list_groups()
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_developer_harness_requires_synthetic_ack_and_rejects_database_paths(self) -> None:
        harness = Path(__file__).resolve().parents[1] / "tools" / "benchmark_capture_event_adapter.py"
        no_ack = subprocess.run(
            [sys.executable, str(harness)],
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(no_ack.returncode, 2)
        self.assertIn("--synthetic-only", no_ack.stderr)
        arbitrary_db = subprocess.run(
            [sys.executable, str(harness), "--synthetic-only", "--database", "synthetic-placeholder.sqlite3"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(arbitrary_db.returncode, 2)
        self.assertIn("unrecognized arguments", arbitrary_db.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
