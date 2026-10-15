from __future__ import annotations

import sqlite3
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
    RecordingLeaseBusyError,
    RecordingLeaseOwnershipError,
    StreamStatus,
)
from camera_exploration_lab.grouped_recording_coordinator import (
    CoordinatorError,
    CoordinatorState,
    CoordinatorStateError,
    CoordinatedStreamState,
    GroupedRecordingCoordinator,
)
from camera_exploration_lab.models import BackendKind, CameraIdentity
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3
from grouped_test_support import create_synthetic_database


def _compete_for_lease(database_path: str, session_id: str, results) -> None:
    database = Database(database_path)
    repository = GroupedExperimentRepository(database)
    try:
        repository.acquire_recording_lease(
            session_id,
            owner_pid=os.getpid(),
            owner_process_start_ticks="synthetic-child-process",
            acquired_at="2026-03-04T05:06:08+00:00",
        )
    except RecordingLeaseBusyError:
        results.put("busy")
    except BaseException as exc:
        results.put(f"unexpected:{type(exc).__name__}")
    else:
        results.put("acquired")


class FakeClock:
    def __init__(self, value: int = 10_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


class GroupedCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "synthetic-grouped.sqlite3"
        self.database, _ = create_synthetic_database(
            self.db_path, version=2, seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(self.database)
        self.repository = GroupedExperimentRepository(self.database)
        self.clock = FakeClock()
        self._coordinators: list[GroupedRecordingCoordinator] = []

    def tearDown(self) -> None:
        for coordinator in self._coordinators:
            if coordinator.snapshot().state == CoordinatorState.RUNNING:
                coordinator.cancel(timeout=2)
            coordinator.wait_closed(timeout=2)
        self.tempdir.cleanup()

    def make_group(self, *, duration: float | None = 30.0):
        streams = (
            GroupedStreamSpec(
                slot="A",
                camera=CameraIdentity(
                    "synthetic:eye", BackendKind.V4L2, "synthetic-endpoint-a",
                    "Synthetic", "Synthetic Eye", None, "ffff", "0001", "fixture", {},
                ),
                endpoint_key="synthetic-endpoint-a",
                benchmark_label="320x240 GRBG",
                configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": 187},
            ),
            GroupedStreamSpec(
                slot="B",
                camera=CameraIdentity(
                    "synthetic:wide", BackendKind.V4L2, "synthetic-endpoint-b",
                    "Synthetic", "Synthetic Wide Camera", None, "ffff", "0002", "fixture", {},
                ),
                endpoint_key="synthetic-endpoint-b",
                benchmark_label="1280x720 MJPEG",
                configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": 30},
            ),
        )
        return self.repository.create_group(
            GroupedExperimentSpec(
                label="Synthetic grouped timing",
                notes="Camera-free test fixture.",
                streams=streams,
                duration_target_seconds=duration,
                record_requested_at="2026-03-04T05:06:07+00:00",
                created_at="2026-03-04T05:06:07+00:00",
                clock_epoch_id="synthetic-epoch-a",
                session_id=f"synthetic-session-{time.monotonic_ns()}",
            )
        )

    def make_coordinator(self, *, duration: float | None = 30.0, queue_capacity: int = 64, batch_size: int = 16):
        group = self.make_group(duration=duration)
        coordinator = GroupedRecordingCoordinator(
            self.repository,
            group.session_id,
            queue_capacity=queue_capacity,
            batch_size=batch_size,
            clock_ns=self.clock,
            now_iso=lambda: "2026-03-04T05:06:07+00:00",
        )
        self._coordinators.append(coordinator)
        return group, coordinator

    def start_both(self, coordinator: GroupedRecordingCoordinator, *, a_offset: int = 100, b_offset: int = 200) -> None:
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=self.clock.value + a_offset)
        coordinator.start_stream("B", host_monotonic_ns=self.clock.value + b_offset)

    def stop_and_finalize(
        self,
        coordinator: GroupedRecordingCoordinator,
        *,
        end_a: int,
        end_b: int,
        reason: str = "manual_stop",
    ):
        epoch = self.clock.value
        self.clock.set(epoch + max(end_a, end_b) + 1)
        coordinator.stop_stream("A", host_monotonic_ns=epoch + end_a)
        coordinator.stop_stream("B", host_monotonic_ns=epoch + end_b)
        return coordinator.finalize(reason_code=reason)

    def test_independent_stream_offsets_and_host_timestamp_provenance(self) -> None:
        group, coordinator = self.make_coordinator()
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch + 10_000_000)
        coordinator.start_stream("B", host_monotonic_ns=epoch + 80_000_000)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 20_000_000).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 30_000_000).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 90_000_000).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 110_000_000).accepted)
        record = self.stop_and_finalize(
            coordinator, end_a=30_010_000_000, end_b=30_080_000_000,
            reason="requested_duration_elapsed",
        )
        self.assertEqual(record.status, GroupStatus.COMPLETED)
        self.assertEqual([stream.stream_status for stream in record.streams], ["completed", "completed"])
        self.assertEqual(record.streams[0].first_host_arrival_elapsed_ns, 20_000_000)
        self.assertEqual(record.streams[1].first_host_arrival_elapsed_ns, 90_000_000)
        self.assertEqual(record.streams[0].capture_frame_count, 2)
        self.assertEqual(record.streams[1].capture_frame_count, 2)
        with self.database.connect_readonly() as connection:
            rows = connection.execute(
                "SELECT benchmark_id, elapsed_ns, source_timestamp_ns, source_sequence, source_clock "
                "FROM measurements WHERE metric='frame_arrival' ORDER BY benchmark_id, sample_index"
            ).fetchall()
            events = connection.execute(
                "SELECT benchmark_id, event_type, elapsed_ns FROM experiment_events "
                "WHERE event_type='stream_recording' ORDER BY benchmark_id"
            ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["source_timestamp_ns"] is None for row in rows))
        self.assertTrue(all(row["source_sequence"] is None for row in rows))
        self.assertTrue(all(row["source_clock"] is None for row in rows))
        starts = {row["benchmark_id"]: row["elapsed_ns"] for row in events}
        self.assertEqual(starts[group.streams[0].benchmark_id], 10_000_000)
        self.assertEqual(starts[group.streams[1].benchmark_id], 80_000_000)
        self.assertFalse(coordinator.snapshot().lease_held)

    def test_starting_one_stream_late_does_not_rebase_or_synchronize_timestamps(self) -> None:
        _group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch + 1_000_000)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 2_000_000).accepted)
        coordinator.start_stream("B", host_monotonic_ns=epoch + 500_000_000)
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 700_000_000).accepted)
        result = self.stop_and_finalize(
            coordinator, end_a=1_000_000_000, end_b=1_100_000_000
        )
        self.assertEqual(result.status, GroupStatus.PARTIAL)
        starts = {event.benchmark_id: event.elapsed_ns for event in result.events if event.event_type == "stream_recording"}
        self.assertEqual(starts[result.streams[0].benchmark_id], 1_000_000)
        self.assertEqual(starts[result.streams[1].benchmark_id], 500_000_000)

    def test_partial_start_failure_keeps_healthy_stream_and_finalizes_partial(self) -> None:
        group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch + 10)
        coordinator.fail_stream_start("B", "device_unavailable", host_monotonic_ns=epoch + 20)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 30).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 40).accepted)
        self.clock.set(epoch + 1_000)
        self.assertEqual(
            coordinator.stop_stream("A", host_monotonic_ns=epoch + 900),
            CoordinatedStreamState.INTERRUPTED,
        )
        result = coordinator.finalize(reason_code="partial_start")
        self.assertEqual(result.status, GroupStatus.PARTIAL)
        self.assertEqual(self.repository.get_group(group.session_id).streams[1].stream_status, "failed")
        self.assertEqual(self.repository.get_group(group.session_id).streams[0].capture_frame_count, 2)

    def test_never_started_participants_are_terminally_failed_without_invented_samples(self) -> None:
        group, coordinator = self.make_coordinator(duration=30)
        epoch = self.clock.value
        coordinator.start()
        coordinator.fail_stream_start("A", "camera_a_unavailable", host_monotonic_ns=epoch + 10)
        coordinator.fail_stream_start("B", "camera_b_unavailable", host_monotonic_ns=epoch + 20)
        result = coordinator.finalize(reason_code="startup_failed")
        self.assertEqual(result.status, GroupStatus.FAILED)
        self.assertTrue(all(stream.stream_status == "failed" for stream in result.streams))
        self.assertTrue(all(stream.capture_frame_count == 0 for stream in result.streams))
        self.assertTrue(all(stream.summary["frame_count"] == 0 for stream in result.streams))
        self.assertFalse(coordinator.snapshot().lease_held)
        self.assertIsNone(self.repository.get_recording_lease())

    def test_one_stream_can_stop_early_while_sibling_continues(self) -> None:
        _group, coordinator = self.make_coordinator(duration=30)
        epoch = self.clock.value
        self.start_both(coordinator)
        coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 1_000_000)
        coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 2_000_000)
        self.clock.set(epoch + 100_000_000)
        self.assertEqual(
            coordinator.stop_stream("A", host_monotonic_ns=epoch + 100_000_000),
            CoordinatedStreamState.INTERRUPTED,
        )
        self.assertEqual(coordinator.snapshot().streams[1].state, CoordinatedStreamState.RECORDING)
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 200_000_000).accepted)
        self.clock.set(epoch + 300_000_000)
        coordinator.stop_stream("B", host_monotonic_ns=epoch + 300_000_000)
        result = coordinator.finalize()
        self.assertEqual(result.status, GroupStatus.PARTIAL)
        self.assertEqual(result.streams[1].capture_frame_count, 2)

    def test_read_failures_are_separate_from_timing_queue_loss(self) -> None:
        _group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        self.start_both(coordinator)
        self.assertTrue(coordinator.submit_read_failure("A", host_monotonic_ns=epoch + 1_000).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 2_000).accepted)
        self.assertEqual(coordinator.snapshot().streams[0].observed_read_failures, 1)
        self.clock.set(epoch + 1_000_000)
        coordinator.stop_stream("A", host_monotonic_ns=epoch + 900_000)
        coordinator.stop_stream("B", host_monotonic_ns=epoch + 910_000)
        result = coordinator.finalize()
        self.assertEqual(result.status, GroupStatus.PARTIAL)
        self.assertEqual(result.streams[0].read_failure_count, 1)
        self.assertEqual(result.streams[0].timing_queue_loss_count, 0)
        self.assertEqual(result.streams[0].summary["read_failures"], 1)

    def test_writer_full_queue_counts_every_rejection_and_resumes_future_attempts(self) -> None:
        _group, coordinator = self.make_coordinator(duration=None, queue_capacity=2, batch_size=1)
        entered = threading.Event()
        release = threading.Event()
        write_order: list[str] = []
        original = self.repository.record_stream_measurements
        first = True

        def temporarily_slow(session_id, slot, samples, *, owner_token=None):
            nonlocal first
            write_order.append(slot)
            if first:
                first = False
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test writer barrier was not released")
            return original(session_id, slot, samples, owner_token=owner_token)

        self.repository.record_stream_measurements = temporarily_slow
        epoch = self.clock.value
        self.start_both(coordinator)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 1).accepted)
        self.assertTrue(entered.wait(2), "writer did not enter the controlled slow write")
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 2).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 3).accepted)
        rejected = coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 4)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "queue_full")
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 5).accepted)
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot.streams[0].queue_depth, 2)
        self.assertEqual(snapshot.streams[0].queue_high_water, 2)
        self.assertEqual(snapshot.streams[0].queue_rejections, 1)
        self.assertEqual(snapshot.streams[1].queue_rejections, 0)
        release.set()
        self.assertTrue(coordinator.wait_for_committed("A", 3, timeout=2))
        self.assertTrue(coordinator.wait_for_committed("B", 1, timeout=2))
        resumed = coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 5_000)
        self.assertTrue(resumed.accepted)
        self.assertTrue(coordinator.wait_for_committed("A", 4, timeout=2))
        self.assertGreaterEqual(len(write_order), 3)
        self.assertEqual(write_order[:3], ["A", "B", "A"])
        self.clock.set(epoch + 1_000_000)
        coordinator.stop_stream("A", host_monotonic_ns=epoch + 900_000)
        coordinator.stop_stream("B", host_monotonic_ns=epoch + 910_000)
        result = coordinator.finalize()
        self.assertEqual(result.status, GroupStatus.PARTIAL)
        self.assertEqual(result.streams[0].timing_queue_loss_count, 1)
        self.assertEqual(result.streams[0].sequence_gap_count, 1)
        with self.database.connect_readonly() as connection:
            indexes = [row[0] for row in connection.execute(
                "SELECT sample_index FROM measurements WHERE benchmark_id=? AND metric='frame_arrival' ORDER BY sample_index",
                (result.streams[0].benchmark_id,),
            )]
        self.assertEqual(indexes, [0, 1, 2, 4])
        self.assertEqual(result.streams[1].timing_queue_loss_count, 0)

    def test_transaction_failure_marks_incomplete_stops_writes_and_retains_lease(self) -> None:
        first_group, coordinator = self.make_coordinator(duration=None)
        _second_group, blocked = self.make_coordinator(duration=None)
        original = self.repository.record_stream_measurements

        def fail_first_batch(session_id, slot, samples, *, owner_token=None):
            raise sqlite3.OperationalError("database or disk is full")

        self.repository.record_stream_measurements = fail_first_batch
        epoch = self.clock.value
        self.start_both(coordinator)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 1_000).accepted)
        self.assertTrue(coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 2_000).accepted)
        self.assertTrue(coordinator.wait_closed(timeout=2))
        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot.state, CoordinatorState.FAILED)
        self.assertFalse(snapshot.writer_healthy)
        self.assertTrue(snapshot.lease_held)
        self.assertTrue(snapshot.shared_owner_held)
        self.assertGreater(snapshot.streams[0].persistence_losses, 0)
        self.assertIn("OperationalError", snapshot.error)
        self.assertNotIn(str(self.db_path), snapshot.error)
        rejected = coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 3_000)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "writer_failed")
        record = self.repository.get_group(first_group.session_id)
        self.assertNotEqual(record.status, GroupStatus.COMPLETED)
        self.assertFalse(record.data_complete)
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0], 0)
        with self.assertRaises(RecordingLeaseBusyError):
            # The persisted owner is not displaced, even if it appears old.
            blocked.start()
        # start() raises before creating the second coordinator's thread.
        self.assertIsNone(blocked._thread)
        self.repository.record_stream_measurements = original

    def test_active_lease_blocks_unauthorized_repository_writes(self) -> None:
        group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch + 1)
        with self.assertRaises(RecordingLeaseOwnershipError):
            self.repository.increment_stream_counters(
                group.session_id,
                "A",
                {"read_failure_count": 1},
                observed_at="2026-03-04T05:06:08+00:00",
            )
        coordinator.cancel(timeout=2)

    def test_recording_lease_blocks_a_separate_process_on_the_same_database(self) -> None:
        group, coordinator = self.make_coordinator(duration=None)
        coordinator.start()
        context = multiprocessing.get_context("spawn")
        results = context.Queue(maxsize=1)
        process = context.Process(
            target=_compete_for_lease,
            args=(str(self.db_path), group.session_id, results),
            name="synthetic-lease-contender",
            daemon=True,
        )
        process.start()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        self.assertFalse(process.is_alive(), "lease contender subprocess did not finish")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=1), "busy")
        results.close()
        results.join_thread()
        coordinator.cancel(timeout=2)

    def test_cancel_shutdown_is_bounded_and_releases_lease_only_after_finalization(self) -> None:
        group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        self.start_both(coordinator)
        coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 10)
        coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 20)
        self.clock.set(epoch + 1_000)
        self.assertTrue(coordinator.cancel(timeout=2))
        self.assertTrue(coordinator.wait_closed(timeout=1))
        record = self.repository.get_group(group.session_id)
        self.assertEqual(record.status, GroupStatus.INTERRUPTED)
        self.assertFalse(coordinator.snapshot().lease_held)
        self.assertIsNone(self.repository.get_recording_lease())

    def test_shutdown_deadline_returns_promptly_while_blocked_writer_can_finish_after_release(self) -> None:
        _group, coordinator = self.make_coordinator(duration=None)
        entered = threading.Event()
        release = threading.Event()
        original = self.repository.record_stream_measurements

        def gated_write(session_id, slot, samples, *, owner_token=None):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test writer gate was not released")
            return original(session_id, slot, samples, owner_token=owner_token)

        self.repository.record_stream_measurements = gated_write
        epoch = self.clock.value
        self.start_both(coordinator)
        coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 10)
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertFalse(coordinator.cancel(timeout=0.02))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(coordinator.snapshot().lease_held)
        release.set()
        self.assertTrue(coordinator.wait_closed(timeout=2))
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.FINALIZED)
        self.assertFalse(coordinator.snapshot().lease_held)
        self.assertEqual(self.repository.get_group(coordinator.session_id).status, GroupStatus.INTERRUPTED)

    def test_shutdown_reuses_an_already_queued_stream_stop(self) -> None:
        group, coordinator = self.make_coordinator(duration=None)
        entered = threading.Event()
        release = threading.Event()
        terminal_enqueued = threading.Event()
        original_write = self.repository.record_stream_measurements
        original_enqueue = coordinator._enqueue_command_locked
        stop_errors: list[BaseException] = []

        def gated_write(session_id, slot, samples, *, owner_token=None):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test writer gate was not released")
            return original_write(session_id, slot, samples, owner_token=owner_token)

        def observe_terminal(command):
            original_enqueue(command)
            if command.operation == "terminal" and command.slot == "A":
                terminal_enqueued.set()

        self.repository.record_stream_measurements = gated_write
        coordinator._enqueue_command_locked = observe_terminal
        epoch = self.clock.value
        self.start_both(coordinator)
        self.assertTrue(coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 10).accepted)
        self.assertTrue(entered.wait(2))

        def stop_a() -> None:
            try:
                coordinator.stop_stream("A", host_monotonic_ns=epoch + 100)
            except BaseException as exc:
                stop_errors.append(exc)

        stopping = threading.Thread(target=stop_a, name="synthetic-stop-A", daemon=True)
        stopping.start()
        self.assertTrue(terminal_enqueued.wait(2))
        self.assertFalse(coordinator.cancel(timeout=0.02))
        release.set()
        stopping.join(timeout=2)
        self.assertFalse(stopping.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertTrue(coordinator.wait_closed(timeout=2))
        self.assertEqual(self.repository.get_group(group.session_id).status, GroupStatus.INTERRUPTED)
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.FINALIZED)
        self.assertFalse(coordinator.snapshot().lease_held)

        self.repository.record_stream_measurements = original_write
        coordinator._enqueue_command_locked = original_enqueue

    def test_failed_completion_validation_rolls_back_and_keeps_lease(self) -> None:
        group, coordinator = self.make_coordinator(duration=30)
        epoch = self.clock.value
        self.start_both(coordinator)
        coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 1_000)
        coordinator.submit_frame_arrival("B", host_monotonic_ns=epoch + 2_000)
        self.clock.set(epoch + 10_000_000)
        coordinator.stop_stream("A", host_monotonic_ns=epoch + 5_000_000)
        coordinator.stop_stream("B", host_monotonic_ns=epoch + 6_000_000)
        with self.assertRaises(CoordinatorError):
            coordinator.finalize(outcome=GroupStatus.COMPLETED)
        record = self.repository.get_group(group.session_id)
        self.assertNotEqual(record.status, GroupStatus.COMPLETED)
        self.assertFalse(record.data_complete)
        self.assertTrue(coordinator.snapshot().lease_held)
        self.assertEqual(coordinator.snapshot().state, CoordinatorState.FAILED)
        self.assertTrue(coordinator.wait_closed(timeout=1))

    def test_repeated_start_and_stop_calls_are_rejected_or_idempotent(self) -> None:
        _group, coordinator = self.make_coordinator(duration=None)
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch + 10)
        with self.assertRaises(CoordinatorStateError):
            coordinator.start_stream("A", host_monotonic_ns=epoch + 20)
        coordinator.submit_frame_arrival("A", host_monotonic_ns=epoch + 30)
        self.clock.set(epoch + 1_000)
        first = coordinator.stop_stream("A", host_monotonic_ns=epoch + 900)
        second = coordinator.stop_stream("A", host_monotonic_ns=epoch + 950)
        self.assertEqual(first, CoordinatedStreamState.INTERRUPTED)
        self.assertEqual(second, CoordinatedStreamState.INTERRUPTED)
        coordinator.fail_stream_start("B", "not_started", host_monotonic_ns=epoch + 960)
        self.assertEqual(coordinator.finalize().status, GroupStatus.PARTIAL)
        with self.assertRaises(CoordinatorStateError):
            coordinator.start()

    def test_zero_sample_and_sparse_runs_never_claim_complete(self) -> None:
        _group, empty = self.make_coordinator(duration=30)
        epoch = self.clock.value
        self.start_both(empty)
        self.clock.set(epoch + 1_000_000)
        empty.stop_stream("A", host_monotonic_ns=epoch + 900_000)
        empty.stop_stream("B", host_monotonic_ns=epoch + 910_000)
        self.assertEqual(empty.finalize().status, GroupStatus.FAILED)

        _group2, sparse = self.make_coordinator(duration=30)
        epoch2 = self.clock.value
        self.start_both(sparse)
        sparse.submit_frame_arrival("A", host_monotonic_ns=epoch2 + 1_000)
        sparse.submit_frame_arrival("B", host_monotonic_ns=epoch2 + 2_000)
        self.clock.set(epoch2 + 1_000_000)
        sparse.stop_stream("A", host_monotonic_ns=epoch2 + 900_000)
        sparse.stop_stream("B", host_monotonic_ns=epoch2 + 910_000)
        self.assertEqual(sparse.finalize().status, GroupStatus.PARTIAL)

        _group3, full_duration_empty = self.make_coordinator(duration=1)
        epoch3 = self.clock.value
        self.start_both(full_duration_empty)
        self.clock.set(epoch3 + 1_000_000_001)
        full_duration_empty.stop_stream("A", host_monotonic_ns=epoch3 + 1_000_000_000)
        full_duration_empty.stop_stream("B", host_monotonic_ns=epoch3 + 1_000_000_000)
        self.assertEqual(full_duration_empty.finalize().status, GroupStatus.FAILED)
        self.assertEqual(full_duration_empty.snapshot().state, CoordinatorState.FINALIZED)
        self.assertFalse(full_duration_empty.snapshot().lease_held)

    def test_30_second_187_and_24_fps_synthetic_run_stays_bounded_and_complete(self) -> None:
        group, coordinator = self.make_coordinator(duration=30, queue_capacity=256, batch_size=128)
        epoch = self.clock.value
        coordinator.start()
        coordinator.start_stream("A", host_monotonic_ns=epoch)
        coordinator.start_stream("B", host_monotonic_ns=epoch + 5_000_000)
        counts = {"A": 187 * 30, "B": 24 * 30}
        rates = {"A": 187, "B": 24}
        submitted = {"A": 0, "B": 0}
        while submitted["A"] < counts["A"] or submitted["B"] < counts["B"]:
            targets: dict[str, int] = {}
            for slot in ("A", "B"):
                stop = min(counts[slot], submitted[slot] + 128)
                for index in range(submitted[slot], stop):
                    start_offset = 0 if slot == "A" else 5_000_000
                    host_ns = epoch + start_offset + index * 1_000_000_000 // rates[slot]
                    self.assertTrue(coordinator.submit_frame_arrival(slot, host_monotonic_ns=host_ns).accepted)
                submitted[slot] = stop
                targets[slot] = stop
            for slot in ("A", "B"):
                self.assertTrue(coordinator.wait_for_committed(slot, targets[slot], timeout=5))
        epoch_end = epoch + 30_000_000_000
        self.clock.set(epoch_end + 1)
        coordinator.stop_stream("A", host_monotonic_ns=epoch_end)
        coordinator.stop_stream("B", host_monotonic_ns=epoch_end + 5_000_000)
        result = coordinator.finalize(reason_code="requested_duration_elapsed")
        self.assertEqual(result.status, GroupStatus.COMPLETED)
        self.assertEqual(result.streams[0].capture_frame_count, 5610)
        self.assertEqual(result.streams[1].capture_frame_count, 720)
        self.assertEqual(result.streams[0].capture_frame_count + result.streams[1].capture_frame_count, 6330)
        snapshot = coordinator.snapshot()
        self.assertGreater(snapshot.writer_cpu_ns, 0)
        self.assertEqual(snapshot.streams[0].queue_rejections, 0)
        self.assertEqual(snapshot.streams[1].queue_rejections, 0)
        self.assertLessEqual(snapshot.streams[0].queue_high_water, 256)
        self.assertLessEqual(snapshot.streams[1].queue_high_water, 256)
        self.assertEqual(self.repository.get_group(group.session_id).data_complete, True)

    def test_reopened_database_does_not_steal_stale_looking_lease(self) -> None:
        group = self.make_group(duration=None)
        lease = self.repository.acquire_recording_lease(
            group.session_id,
            owner_pid=123456,
            owner_process_start_ticks="synthetic-old-process",
            acquired_at="2000-01-01T00:00:00+00:00",
            owner_token="synthetic-owner-token",
        )
        for stream in group.streams:
            self.repository.set_stream_status(
                group.session_id,
                stream.slot,
                StreamStatus.RECORDING,
                observed_at="2026-03-04T05:06:08+00:00",
                elapsed_ns=1,
                owner_token=lease.owner_token,
            )
        self.repository.set_group_status(
            group.session_id,
            GroupStatus.RECORDING,
            observed_at="2026-03-04T05:06:08+00:00",
            elapsed_ns=1,
            owner_token=lease.owner_token,
        )
        reopened = GroupedExperimentRepository(type(self.database)(self.db_path))
        self.assertEqual(reopened.get_recording_lease(), lease)
        self.assertEqual(reopened.get_group(group.session_id).status, GroupStatus.RECORDING)
        another = self.make_group(duration=None)
        with self.assertRaises(RecordingLeaseBusyError):
            reopened.acquire_recording_lease(
                another.session_id,
                owner_pid=999,
                owner_process_start_ticks=None,
                acquired_at="2026-03-04T05:06:08+00:00",
            )
        reopened.release_recording_lease(lease.owner_token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
