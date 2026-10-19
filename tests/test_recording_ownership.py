from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from camera_exploration_lab.capture import CaptureTimingEvent, NegotiatedSettings
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
)
from camera_exploration_lab.grouped_recording_coordinator import GroupedRecordingCoordinator
from camera_exploration_lab.models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    SessionRecord,
)
from camera_exploration_lab.grouped_recording import RecordingLeaseBusyError
from camera_exploration_lab.models import MeasurementSample, StreamScope
from camera_exploration_lab.recording import ExperimentRecorder
from camera_exploration_lab.recording_ownership import (
    RecordingOwnershipBusyError,
    RecordingOwnershipError,
    RecordingRecoveryRequiredError,
    RecoveryClassification,
    RecoveryLockState,
    validate_recovery_candidate_readonly,
)
from camera_exploration_lab.schema_migrations import (
    SchemaMigrationError,
    migrate_synthetic_database_to_v3,
)
from grouped_test_support import create_synthetic_database
from camera_exploration_lab.models import utc_now_iso


def _camera(suffix: str = "a") -> CameraIdentity:
    return CameraIdentity(
        stable_identifier=f"synthetic:camera:{suffix}",
        backend=BackendKind.V4L2,
        device_path=f"synthetic-node-{suffix}",
        manufacturer="Synthetic",
        model=f"Mock camera {suffix}",
        vendor_id="ffff",
        product_id="0001",
        identity_source="synthetic_fixture",
    )


def _recorder(database: Database, *, suffix: str = "a") -> ExperimentRecorder:
    mode = CaptureMode("synthetic-240p", 320, 240, 187, "GRBG")
    return ExperimentRecorder(
        database,
        _camera(suffix),
        mode,
        NegotiatedSettings(320, 240, 187.0, "GRBG"),
        name="Synthetic ownership test",
        notes="Camera-free fixture",
        duration_seconds=None,
    )


def _group_spec(label: str = "Synthetic grouped ownership") -> GroupedExperimentSpec:
    streams = (
        GroupedStreamSpec(
            slot="A", camera=_camera("a"), endpoint_key="synthetic:endpoint:a",
            benchmark_label="320x240 GRBG", configuration={"requested_fps": 187},
        ),
        GroupedStreamSpec(
            slot="B", camera=_camera("b"), endpoint_key="synthetic:endpoint:b",
            benchmark_label="1280x720 MJPEG", configuration={"requested_fps": 30},
        ),
    )
    return GroupedExperimentSpec(
        label=label,
        streams=streams,
        notes="Synthetic fixture only",
        duration_target_seconds=None,
        record_requested_at="2026-10-15T12:00:00+00:00",
        created_at="2026-10-15T12:00:00+00:00",
        clock_epoch_id=f"synthetic-clock-{uuid4()}",
        session_id=f"synthetic-session-{uuid4()}",
    )


def _single_process_owner(path: str, ready, release, crash: bool = False) -> None:
    database = Database(path)
    recorder = _recorder(database, suffix="process")
    ready.set()
    if crash:
        os._exit(23)
    if release.wait(6):
        recorder.stop("synthetic_process_stop", timeout=4)
    else:
        recorder.stop("synthetic_process_timeout", timeout=2)


class _FailingFinalizationDatabase(Database):
    def record_measurements_batch(self, samples, *, ownership=None):
        raise sqlite3.OperationalError("synthetic disk-full failure")

    def finish_live_experiment(self, *args, **kwargs):
        raise sqlite3.OperationalError("synthetic finalization failure")


def _single_failed_writer_process(path: str, ready, release, results) -> None:
    database = _FailingFinalizationDatabase(path)
    recorder = _recorder(database, suffix="failing-writer")
    recorder.record_capture_event(CaptureTimingEvent(
        kind="frame_arrival",
        monotonic_ns=time.monotonic_ns() + 1_000_000,
        observed_at=utc_now_iso(),
        interval_ns=None,
        capture_fps=100.0,
    ))
    snapshot = recorder.stop("synthetic_stop", timeout=4)
    results.put((snapshot.state.value, bool(recorder._ownership)))
    ready.set()
    release.wait(6)
    os._exit(26)


def _grouped_process_owner(path: str, ready, release, crash: bool = False) -> None:
    database = Database(path)
    repository = GroupedExperimentRepository(database)
    coordinator = GroupedRecordingCoordinator.create_and_start(
        repository,
        _group_spec("Synthetic child-process group"),
        clock_ns=lambda: 50_000_000_000,
        now_iso=lambda: "2026-10-15T12:00:00+00:00",
    )
    ready.set()
    if crash:
        os._exit(24)
    if release.wait(6):
        coordinator.cancel(timeout=4)
    else:
        coordinator.cancel(timeout=2)


def _fork_inherited_owner_check(path: str, owner_token: str, results) -> None:
    database = Database(path)
    try:
        database.require_recording_ownership(owner_token)
    except RecordingOwnershipError:
        token_rejected = True
    else:
        token_rejected = False
    try:
        database.acquire_recording_ownership(owner_kind="fork-child")
    except RecordingOwnershipBusyError:
        results.put((token_rejected, "busy"))
    else:
        results.put((token_rejected, "acquired"))


class RecordingOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.v2_path = self.root / "synthetic-v2.sqlite3"
        self.v2, _ = create_synthetic_database(
            self.v2_path, version=2, seed_legacy_data=False
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _v3(self) -> tuple[Database, GroupedExperimentRepository]:
        path = self.root / f"synthetic-v3-{uuid4()}.sqlite3"
        database, _ = create_synthetic_database(path, version=2, seed_legacy_data=False)
        migrate_synthetic_database_to_v3(database)
        return database, GroupedExperimentRepository(database)

    def test_same_process_single_recorders_are_exclusive_and_retry_after_finalize(self) -> None:
        first = _recorder(self.v2, suffix="first")
        with self.assertRaises(RecordingOwnershipBusyError):
            _recorder(self.v2, suffix="second")
        with self.v2.connect_readonly() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM benchmark_runs WHERE status='running'"
            ).fetchone()[0], 1)
        self.assertEqual(first.stop().state.value, "interrupted")
        second = _recorder(self.v2, suffix="second")
        self.assertEqual(second.stop().state.value, "interrupted")

    def test_live_run_writes_and_finalization_require_the_active_owner(self) -> None:
        recorder = _recorder(self.v2)
        sample = MeasurementSample(
            benchmark_id=recorder.snapshot().benchmark_id,
            metric="frame_arrival",
            value=0.0,
            unit="seconds",
            stream_scope=StreamScope.CAPTURE,
            sample_index=1,
        )
        with self.assertRaises(RecordingOwnershipError):
            self.v2.record_measurements_batch([sample])
        with self.assertRaises(RecordingOwnershipError):
            self.v2.finish_live_experiment(
                recorder.snapshot().benchmark_id,
                ended_at="2026-10-15T12:00:01+00:00",
                duration_seconds=1.0,
                status="interrupted",
                termination_reason="unauthorized_test",
            )
        recorder.stop()

    def test_live_run_creation_requires_owner_before_inserting_rows(self) -> None:
        camera = _camera("creation")
        session = SessionRecord(
            camera_id=camera.stable_identifier,
            label="Synthetic unowned live run",
            source="live_experiment",
            metadata={},
        )
        benchmark = BenchmarkRecord(session_id=session.id, label="synthetic")
        with self.assertRaises(RecordingOwnershipError):
            self.v2.begin_live_experiment(
                camera, session, benchmark, [], seen_at="2026-10-15T12:00:00+00:00"
            )
        with self.v2.connect_readonly() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id=?", (session.id,)
            ).fetchone()[0], 0)

    def test_symlink_and_hardlink_aliases_resolve_to_the_same_file_lock(self) -> None:
        alias = self.root / "linked-history.sqlite3"
        alias.symlink_to(self.v2_path)
        owner = self.v2.acquire_recording_ownership(owner_kind="test")
        with self.assertRaises(RecordingOwnershipBusyError):
            Database(alias).acquire_recording_ownership(owner_kind="symlink-alias")
        hardlink = self.root / "hardlinked-history.sqlite3"
        os.link(self.v2_path, hardlink)
        with self.assertRaises(RecordingOwnershipBusyError):
            Database(hardlink).acquire_recording_ownership(owner_kind="hardlink-alias")
        owner.release(owner.owner_token)

    def test_owner_token_mismatch_cannot_release_the_lock(self) -> None:
        owner = self.v2.acquire_recording_ownership(owner_kind="test")
        with self.assertRaises(RecordingOwnershipError):
            owner.release("not-the-owner")
        with self.assertRaises(RecordingOwnershipBusyError):
            Database(self.v2_path).acquire_recording_ownership(owner_kind="other")
        owner.release(owner.owner_token)
        next_owner = self.v2.acquire_recording_ownership(owner_kind="after-release")
        next_owner.release(next_owner.owner_token)

    def test_duplicate_owner_token_on_another_database_does_not_leak_its_lock(self) -> None:
        other_path = self.root / "other-synthetic-v2.sqlite3"
        other, _ = create_synthetic_database(
            other_path, version=2, seed_legacy_data=False
        )
        first = self.v2.acquire_recording_ownership(
            owner_kind="first-database", owner_token="synthetic-shared-token"
        )
        try:
            with self.assertRaises(RecordingOwnershipError):
                other.acquire_recording_ownership(
                    owner_kind="second-database", owner_token="synthetic-shared-token"
                )
            # The rejected attempt must close its descriptor and leave the
            # second database available to a distinct legitimate owner.
            second = other.acquire_recording_ownership(
                owner_kind="second-database", owner_token="synthetic-second-token"
            )
            second.release(second.owner_token)
        finally:
            first.release(first.owner_token)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires Linux fork")
    def test_forked_child_cannot_reuse_inherited_owner_handle(self) -> None:
        owner = self.v2.acquire_recording_ownership(owner_kind="parent")
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        process = context.Process(
            target=_fork_inherited_owner_check,
            args=(str(self.v2_path), owner.owner_token, results),
        )
        process.start()
        try:
            self.assertEqual(results.get(timeout=5), (True, "busy"))
        finally:
            process.join(5)
            owner.release(owner.owner_token)
        self.assertEqual(process.exitcode, 0)

    def test_recovery_inspection_is_read_only_and_tracks_owner_lock(self) -> None:
        recorder = _recorder(self.v2)
        session_id = recorder.snapshot().session_id
        before = self.v2_path.read_bytes()
        active = self.v2.inspect_recording_recovery()
        finding = next(item for item in active.findings if item.session_id == session_id)
        self.assertEqual(active.lock_state, RecoveryLockState.HELD)
        self.assertEqual(finding.classification, RecoveryClassification.ACTIVE_OWNER)
        self.assertEqual(self.v2_path.read_bytes(), before)
        recorder.stop()
        ended = self.v2.inspect_recording_recovery()
        finding = next(item for item in ended.findings if item.session_id == session_id)
        self.assertEqual(ended.lock_state, RecoveryLockState.AVAILABLE)
        self.assertEqual(finding.classification, RecoveryClassification.DELIBERATELY_INTERRUPTED)

    def test_memory_database_startup_recovery_is_a_noop(self) -> None:
        database = Database(":memory:")
        try:
            database.initialize()
            self.assertEqual(database.recover_interrupted_experiments(), 0)
        finally:
            database.close()

    def test_normal_initialize_remains_schema_v2_and_does_not_create_v3_lease_table(self) -> None:
        self.v2.initialize()
        with self.v2.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recording_leases'"
            ).fetchone())

    def test_synthetic_migration_refuses_active_owner_and_unresolved_v2_run(self) -> None:
        owner = self.v2.acquire_recording_ownership(
            owner_kind="synthetic-active-recorder", check_recovery=False
        )
        try:
            with self.assertRaises(SchemaMigrationError):
                migrate_synthetic_database_to_v3(self.v2)
        finally:
            owner.release(owner.owner_token)
        with self.v2.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

        camera = _camera("unresolved-before-migration")
        session = SessionRecord(
            camera_id=camera.stable_identifier,
            label="Synthetic unresolved live run",
            source="live_experiment",
            metadata={"owner_pid": 2_000_000_000, "owner_process_start_ticks": "1"},
        )
        self.v2.upsert_camera(camera)
        self.v2.create_session(session)
        self.v2.create_benchmark(BenchmarkRecord(
            session_id=session.id, label="synthetic pending", status="running"
        ))
        with self.assertRaises(SchemaMigrationError):
            migrate_synthetic_database_to_v3(self.v2)
        with self.v2.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_recordings'"
            ).fetchone())

    def test_single_recorder_and_grouped_coordinator_share_ownership_v3(self) -> None:
        database, repository = self._v3()
        single = _recorder(database, suffix="single")
        before = len(repository.list_groups())
        with self.assertRaises(RecordingOwnershipBusyError):
            GroupedRecordingCoordinator.create_and_start(repository, _group_spec())
        self.assertEqual(len(repository.list_groups()), before)
        single.stop()

        coordinator = GroupedRecordingCoordinator.create_and_start(
            repository,
            _group_spec(),
            clock_ns=lambda: 60_000_000_000,
            now_iso=lambda: "2026-10-15T12:00:00+00:00",
        )
        with self.assertRaises(RecordingOwnershipBusyError):
            _recorder(database, suffix="blocked-single")
        self.assertTrue(coordinator.cancel(timeout=3))
        self.assertEqual(repository.get_group(coordinator.session_id).status, GroupStatus.INTERRUPTED)

    def test_two_grouped_recorders_in_one_process_cannot_create_duplicate_groups(self) -> None:
        database, repository = self._v3()
        coordinator = GroupedRecordingCoordinator.create_and_start(
            repository,
            _group_spec("first"),
            clock_ns=lambda: 70_000_000_000,
            now_iso=lambda: "2026-10-15T12:00:00+00:00",
        )
        self.assertEqual(len(repository.list_groups()), 1)
        with self.assertRaises(RecordingOwnershipBusyError):
            GroupedRecordingCoordinator.create_and_start(repository, _group_spec("second"))
        self.assertEqual(len(repository.list_groups()), 1)
        self.assertTrue(coordinator.cancel(timeout=3))
        retry = GroupedRecordingCoordinator.create_and_start(
            repository,
            _group_spec("retry"),
            clock_ns=lambda: 80_000_000_000,
            now_iso=lambda: "2026-10-15T12:00:00+00:00",
        )
        self.assertTrue(retry.cancel(timeout=3))

    def test_existing_stale_sql_lease_blocks_group_creation_without_new_rows(self) -> None:
        database, repository = self._v3()
        first = repository.create_group(_group_spec("stale lease fixture"))
        lease = repository.acquire_recording_lease(
            first.session_id,
            owner_pid=os.getpid(),
            owner_process_start_ticks="synthetic-stale-owner",
            acquired_at="2026-10-15T12:00:00+00:00",
        )
        with self.assertRaises(RecordingRecoveryRequiredError):
            GroupedRecordingCoordinator.create_and_start(repository, _group_spec("must not exist"))
        self.assertEqual(len(repository.list_groups()), 1)
        self.assertEqual(repository.get_recording_lease().owner_token, lease.owner_token)

    def test_factory_removes_its_pending_group_if_sql_lease_acquisition_fails(self) -> None:
        database, repository = self._v3()
        original_count = len(repository.list_groups())
        with patch.object(
            repository,
            "acquire_recording_lease",
            side_effect=RecordingLeaseBusyError("synthetic transactional contention"),
        ):
            with self.assertRaises(RecordingLeaseBusyError):
                GroupedRecordingCoordinator.create_and_start(repository, _group_spec("must be removed"))
        self.assertEqual(len(repository.list_groups()), original_count)

    def test_cross_process_single_recorder_contention_uses_same_database_lease(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_single_process_owner,
            args=(str(self.v2_path), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "child recorder did not acquire ownership")
            with self.assertRaises(RecordingOwnershipBusyError):
                _recorder(self.v2, suffix="parent")
        finally:
            release.set()
            process.join(8)
        self.assertEqual(process.exitcode, 0)

    def test_cross_process_grouped_recorder_contention_prevents_new_group(self) -> None:
        database, repository = self._v3()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_grouped_process_owner,
            args=(str(database.path), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "child grouped recorder did not acquire ownership")
            with self.assertRaises(RecordingOwnershipBusyError):
                GroupedRecordingCoordinator.create_and_start(repository, _group_spec("parent attempt"))
            with self.assertRaises(RecordingOwnershipBusyError):
                _recorder(database, suffix="parent single attempt")
            self.assertEqual(len(repository.list_groups()), 1)
        finally:
            release.set()
            process.join(8)
        self.assertEqual(process.exitcode, 0)

    def test_cross_process_single_recorder_blocks_grouped_recorder_on_v3(self) -> None:
        database, repository = self._v3()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_single_process_owner,
            args=(str(database.path), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "child single recorder did not acquire ownership")
            with self.assertRaises(RecordingOwnershipBusyError):
                GroupedRecordingCoordinator.create_and_start(repository, _group_spec("blocked group"))
            self.assertEqual(repository.list_groups(), ())
        finally:
            release.set()
            process.join(8)
        self.assertEqual(process.exitcode, 0)

    def test_process_crash_leaves_single_run_indeterminate_until_explicit_startup_recovery(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_single_process_owner,
            args=(str(self.v2_path), ready, release, True),
        )
        process.start()
        self.assertTrue(ready.wait(5), "child recorder did not create its live run")
        process.join(5)
        self.assertEqual(process.exitcode, 23)
        inspection = self.v2.inspect_recording_recovery()
        finding = next(item for item in inspection.findings if item.kind == "single_camera")
        self.assertEqual(finding.classification, RecoveryClassification.INDETERMINATE_AFTER_CRASH)
        with self.assertRaises(RecordingRecoveryRequiredError):
            self.v2.acquire_recording_ownership(owner_kind="blocked-before-recovery")
        self.assertEqual(self.v2.recover_interrupted_experiments(), 1)
        recovered = self.v2.inspect_recording_recovery()
        finding = next(item for item in recovered.findings if item.session_id == finding.session_id)
        self.assertEqual(finding.classification, RecoveryClassification.INDETERMINATE_AFTER_CRASH)

    def test_single_writer_failure_keeps_owner_until_process_exit_and_recovery(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready, release, results = context.Event(), context.Event(), context.Queue()
        process = context.Process(
            target=_single_failed_writer_process,
            args=(str(self.v2_path), ready, release, results),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5), "writer failure did not reach terminal handling")
            self.assertEqual(results.get(timeout=3), ("failed", True))
            active = self.v2.inspect_recording_recovery()
            self.assertEqual(active.lock_state, RecoveryLockState.HELD)
            self.assertEqual(
                active.findings[0].classification,
                RecoveryClassification.ACTIVE_OWNER,
            )
            with self.assertRaises(RecordingOwnershipBusyError):
                self.v2.acquire_recording_ownership(owner_kind="while-writer-failed")
        finally:
            release.set()
            process.join(8)
        self.assertEqual(process.exitcode, 26)
        after_exit = self.v2.inspect_recording_recovery()
        self.assertEqual(
            after_exit.findings[0].classification,
            RecoveryClassification.INDETERMINATE_AFTER_CRASH,
        )
        self.assertEqual(self.v2.recover_interrupted_experiments(), 1)

    def test_process_crash_leaves_group_lease_and_read_only_recovery_evidence(self) -> None:
        database, repository = self._v3()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_grouped_process_owner,
            args=(str(database.path), ready, release, True),
        )
        process.start()
        self.assertTrue(ready.wait(5), "child grouped recorder did not start")
        process.join(5)
        self.assertEqual(process.exitcode, 24)
        before = database.path.read_bytes()
        inspection = database.inspect_recording_recovery()
        finding = next(item for item in inspection.findings if item.kind == "grouped")
        self.assertEqual(finding.classification, RecoveryClassification.INDETERMINATE_AFTER_CRASH)
        self.assertTrue(finding.lease_present)
        self.assertEqual(len(finding.streams), 2)
        self.assertIsNotNone(finding.last_event_type)
        self.assertEqual(database.path.read_bytes(), before)
        with self.assertRaises(RecordingRecoveryRequiredError):
            GroupedRecordingCoordinator.create_and_start(repository, _group_spec("blocked"))
        candidate = validate_recovery_candidate_readonly(database, finding.session_id)
        self.assertEqual(candidate.classification, RecoveryClassification.INDETERMINATE_AFTER_CRASH)


if __name__ == "__main__":
    unittest.main()
