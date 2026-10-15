from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupInvariantError,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedSetting,
    GroupedStreamSpec,
    RecordingLeaseBusyError,
    RecordingLeaseOwnershipError,
    StreamStatus,
)
from camera_exploration_lab.models import (
    BackendKind,
    CameraIdentity,
    MeasurementSample,
    SQLITE_INT_MAX,
    StreamScope,
)
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3
from grouped_test_support import create_synthetic_database


def stream_specs(*, duplicate_endpoint: bool = False):
    camera_a = CameraIdentity(
        "synthetic:camera:eye",
        BackendKind.V4L2,
        "synthetic-node-a",
        "Synthetic",
        "Synthetic Eye",
        "SYNTHETIC-EYE",
        "ffff",
        "0001",
        "synthetic_fixture",
        {"profile": "eye"},
    )
    camera_b = CameraIdentity(
        "synthetic:camera:aver",
        BackendKind.V4L2,
        "synthetic-node-b",
        "Synthetic",
        "Synthetic Wide Camera",
        "SYNTHETIC-AVER",
        "ffff",
        "0002",
        "synthetic_fixture",
        {"profile": "wide"},
    )
    endpoint_a = "synthetic:endpoint:a"
    endpoint_b = endpoint_a if duplicate_endpoint else "synthetic:endpoint:b"
    return (
        GroupedStreamSpec(
            slot="A",
            camera=camera_a,
            endpoint_key=endpoint_a,
            benchmark_label="320x240 GRBG",
            configuration={"width": 320, "height": 240, "pixel_format": "GRBG", "requested_fps": 187},
            settings=(
                GroupedSetting(
                    "fps", requested_value=187, reported_value=187,
                    verification_status="reported", verification_method="synthetic driver readback",
                ),
            ),
        ),
        GroupedStreamSpec(
            slot="B",
            camera=camera_b,
            endpoint_key=endpoint_b,
            benchmark_label="1280x720 MJPEG",
            configuration={"width": 1280, "height": 720, "pixel_format": "MJPEG", "requested_fps": 30},
            settings=(
                GroupedSetting(
                    "fps", requested_value=30, reported_value=30,
                    verification_status="reported", verification_method="synthetic driver readback",
                ),
            ),
        ),
    )


class GroupedRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database, _ = create_synthetic_database(
            Path(self.tempdir.name) / "grouped.sqlite3", version=2, seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(self.database)
        self.repository = GroupedExperimentRepository(self.database)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_group(self, **changes):
        values = {
            "label": "Synthetic paired throughput",
            "notes": "Synthetic fixture; no camera used.",
            "duration_target_seconds": 30,
            "record_requested_at": "2026-02-03T04:05:06+00:00",
            "created_at": "2026-02-03T04:05:06+00:00",
            "clock_epoch_id": "synthetic-clock-epoch",
            "streams": stream_specs(),
        }
        values.update(changes)
        return self.repository.create_group(GroupedExperimentSpec(**values))

    def start_group(self, group):
        for stream in group.streams:
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.RECORDING,
                observed_at="2026-02-03T04:05:07+00:00", elapsed_ns=0,
            )
        self.repository.set_group_status(
            group.session_id, GroupStatus.RECORDING,
            observed_at="2026-02-03T04:05:07+00:00", elapsed_ns=0,
        )

    @staticmethod
    def frame_samples(benchmark_id: str, indexes: tuple[int, ...], *, base_ns: int, step_ns: int):
        samples = []
        for offset, index in enumerate(indexes):
            elapsed_ns = base_ns + offset * step_ns
            samples.append(
                MeasurementSample(
                    benchmark_id=benchmark_id,
                    metric="frame_arrival",
                    value=1_000_000_000 / step_ns,
                    unit="frames/s",
                    stream_scope=StreamScope.CAPTURE,
                    observed_at="2026-02-03T04:05:08+00:00",
                    elapsed_seconds=elapsed_ns / 1_000_000_000,
                    sample_index=index,
                    interval_ns=None if offset == 0 else step_ns,
                    method="opencv_read_return_v1",
                    elapsed_ns=elapsed_ns,
                    # OpenCV provides no driver timestamp or source sequence.
                    source_timestamp_ns=None,
                    source_sequence=None,
                    source_clock=None,
                )
            )
        return samples

    def test_group_creation_keeps_stream_identity_settings_and_summaries_independent(self) -> None:
        group = self.create_group()

        self.assertEqual(group.source, "grouped_experiment")
        self.assertEqual(group.status, "pending")
        self.assertEqual([stream.slot for stream in group.streams], ["A", "B"])
        self.assertEqual(len({stream.benchmark_id for stream in group.streams}), 2)
        self.assertEqual(len({stream.camera_id for stream in group.streams}), 2)
        self.assertEqual(group.streams[0].identity_snapshot["model"], "Synthetic Eye")
        self.assertEqual(group.streams[1].identity_snapshot["model"], "Synthetic Wide Camera")
        self.assertEqual(group.streams[0].settings[0].requested_value, 187)
        self.assertEqual(group.streams[0].settings[0].reported_value, 187)
        self.assertEqual(group.streams[1].settings[0].requested_value, 30)
        self.assertEqual(group.streams[1].timing_method, "opencv_read_return_v1")
        self.assertEqual(group.streams[0].timestamp_source, "host_monotonic_after_read")
        self.assertEqual(group.streams[0].clock_type, "monotonic_ns")
        self.assertIsNone(group.streams[0].first_host_arrival_elapsed_ns)
        self.assertEqual(self.repository.list_groups(), (group,))
        self.assertEqual(
            len(self.repository.list_groups(camera_id="synthetic:camera:eye")), 1
        )
        with self.database.connect_readonly() as connection:
            session = connection.execute(
                "SELECT camera_id, source FROM sessions WHERE id=?", (group.session_id,)
            ).fetchone()
            rows = connection.execute(
                """SELECT slot, benchmark_id, camera_id, endpoint_key
                   FROM benchmark_streams WHERE session_id=? ORDER BY slot""",
                (group.session_id,),
            ).fetchall()
        self.assertIsNone(session["camera_id"])
        self.assertEqual(session["source"], "grouped_experiment")
        self.assertEqual(len(rows), 2)

    def test_duplicate_slots_endpoints_and_setting_keys_are_rejected_before_writes(self) -> None:
        duplicate_endpoint = stream_specs(duplicate_endpoint=True)
        with self.assertRaisesRegex(GroupInvariantError, "distinct capture endpoints"):
            self.create_group(streams=duplicate_endpoint)
        duplicate_slot = (stream_specs()[0], replace(stream_specs()[1], slot="A"))
        with self.assertRaisesRegex(GroupInvariantError, "exactly slots A and B"):
            self.create_group(streams=duplicate_slot)
        repeated_settings = replace(
            stream_specs()[0],
            settings=(GroupedSetting("fps"), GroupedSetting("fps")),
        )
        with self.assertRaisesRegex(GroupInvariantError, "setting keys must be unique"):
            self.create_group(streams=(repeated_settings, stream_specs()[1]))
        self.assertEqual(self.database.count("sessions"), 0)
        self.assertEqual(self.database.count("cameras"), 0)

    def test_group_creation_rolls_back_parent_cameras_and_first_stream_on_late_failure(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """CREATE TRIGGER synthetic_fail_stream_b
                   BEFORE INSERT ON benchmark_streams WHEN NEW.slot='B'
                   BEGIN SELECT RAISE(ABORT, 'synthetic stream failure'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic stream failure"):
            self.create_group()
        self.assertEqual(self.database.count("sessions"), 0)
        self.assertEqual(self.database.count("cameras"), 0)
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM group_recordings").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM setting_snapshots").fetchone()[0], 0)

    def test_schema_constraints_reject_duplicate_slot_cross_session_run_and_cross_group_event(self) -> None:
        first = self.create_group()
        second = self.create_group(label="Second synthetic group")
        stream_a = first.streams[0]
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO benchmark_runs
                   (id, session_id, label, status, configuration_json)
                   VALUES ('synthetic-extra-run', ?, 'extra', 'pending', '{}')""",
                (first.session_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO benchmark_streams
                       (session_id, slot, benchmark_id, camera_id, endpoint_key,
                        identity_snapshot_json, timing_method, timestamp_source,
                        clock_type, collection_point, timing_method_version, stream_status)
                       VALUES (?, 'A', 'synthetic-extra-run', ?, 'extra-endpoint',
                               '{}', 'mock', 'mock', 'monotonic_ns', 'after-read',
                               '1', 'pending')""",
                    (first.session_id, stream_a.camera_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO benchmark_streams
                       (session_id, slot, benchmark_id, camera_id, endpoint_key,
                        identity_snapshot_json, timing_method, timestamp_source,
                        clock_type, collection_point, timing_method_version, stream_status)
                       VALUES (?, 'A', ?, ?, 'cross-group-endpoint',
                               '{}', 'mock', 'mock', 'monotonic_ns', 'after-read',
                               '1', 'pending')""",
                    (second.session_id, stream_a.benchmark_id, stream_a.camera_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO experiment_events
                       (session_id, benchmark_id, event_type, observed_at)
                       VALUES (?, ?, 'wrong_child', 'synthetic')""",
                    (first.session_id, second.streams[0].benchmark_id),
                )

    def test_database_trigger_requires_both_participants_before_recording(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO sessions
                   (id, camera_id, label, source, created_at, metadata_json)
                   VALUES ('synthetic-pending-only', NULL, 'pending', 'grouped_experiment',
                           'synthetic', '{}')"""
            )
            connection.execute(
                """INSERT INTO group_recordings
                   (session_id, status, clock_epoch_id, timing_method_version)
                   VALUES ('synthetic-pending-only', 'pending', 'clock', '1')"""
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exactly two stream participants"):
                connection.execute(
                    "UPDATE group_recordings SET status='recording' WHERE session_id='synthetic-pending-only'"
                )

    def test_child_transitions_events_and_group_ready_invariant(self) -> None:
        group = self.create_group()
        with self.assertRaisesRegex(GroupInvariantError, "both child streams"):
            self.repository.validate_ready_to_record(group.session_id)
        self.repository.set_stream_status(
            group.session_id, "A", StreamStatus.RECORDING, observed_at="synthetic-a"
        )
        with self.assertRaisesRegex(GroupInvariantError, "both child streams"):
            self.repository.set_group_status(
                group.session_id, GroupStatus.RECORDING, observed_at="synthetic"
            )
        self.repository.set_stream_status(
            group.session_id, "B", StreamStatus.RECORDING, observed_at="synthetic-b"
        )
        self.repository.validate_ready_to_record(group.session_id)
        self.repository.set_group_status(
            group.session_id, GroupStatus.RECORDING, observed_at="synthetic-start"
        )
        current = self.repository.get_group(group.session_id)
        self.assertEqual(current.status, "recording")
        self.assertEqual(
            [event.event_type for event in current.events],
            ["group_created", "stream_recording", "stream_recording", "group_recording"],
        )
        with self.assertRaisesRegex(GroupInvariantError, "finalize_group"):
            self.repository.set_group_status(
                group.session_id, GroupStatus.COMPLETED, observed_at="synthetic"
            )

    def test_stream_sample_batches_preserve_integer_provenance_and_allow_sequence_gaps(self) -> None:
        group = self.create_group()
        self.start_group(group)
        stream_a = group.streams[0]
        self.repository.increment_stream_counters(
            group.session_id, "A", {"timing_queue_loss_count": 1},
            observed_at="synthetic-overflow", elapsed_ns=4_000_000_000,
        )
        samples = self.frame_samples(stream_a.benchmark_id, (10, 12), base_ns=10_000_000, step_ns=5_000_000)
        self.assertEqual(
            self.repository.record_stream_measurements(group.session_id, "A", samples), 2
        )
        self.repository.note_host_arrival_bounds(
            group.session_id, "A", first_elapsed_ns=9_000_000, last_elapsed_ns=20_000_000
        )
        with self.database.connect_readonly() as connection:
            rows = connection.execute(
                """SELECT sample_index, elapsed_ns, elapsed_seconds, interval_ns,
                          source_timestamp_ns, source_sequence, source_clock
                   FROM measurements WHERE benchmark_id=? ORDER BY sample_index""",
                (stream_a.benchmark_id,),
            ).fetchall()
            state = connection.execute(
                """SELECT last_persisted_sample_index, timing_queue_loss_count,
                          data_complete, sequence_gap_count
                   FROM benchmark_streams WHERE session_id=? AND slot='A'""",
                (group.session_id,),
            ).fetchone()
        self.assertEqual([row["sample_index"] for row in rows], [10, 12])
        self.assertEqual([row["elapsed_ns"] for row in rows], [10_000_000, 15_000_000])
        self.assertEqual(rows[1]["elapsed_seconds"], 0.015)
        self.assertEqual(rows[1]["interval_ns"], 5_000_000)
        self.assertIsNone(rows[1]["source_timestamp_ns"])
        self.assertIsNone(rows[1]["source_sequence"])
        self.assertIsNone(rows[1]["source_clock"])
        self.assertEqual(state["last_persisted_sample_index"], 12)
        self.assertEqual(state["timing_queue_loss_count"], 1)
        self.assertEqual(state["sequence_gap_count"], 1)
        self.assertEqual(state["data_complete"], 0)
        current = self.repository.get_group(group.session_id)
        self.assertFalse(current.data_complete)
        self.assertEqual(current.status, "degraded")
        self.assertEqual(current.streams[0].first_host_arrival_elapsed_ns, 9_000_000)
        self.assertEqual(current.streams[0].last_host_arrival_elapsed_ns, 20_000_000)
        self.assertEqual(current.streams[0].first_persisted_elapsed_ns, 10_000_000)
        self.assertEqual(current.streams[0].last_persisted_elapsed_ns, 15_000_000)
        self.assertEqual(current.streams[0].sequence_gap_count, 1)
        self.assertIn("sample_sequence_gap", current.streams[0].integrity_flags)

        with self.assertRaisesRegex(GroupInvariantError, "did not advance"):
            self.repository.record_stream_measurements(
                group.session_id, "A",
                self.frame_samples(stream_a.benchmark_id, (12,), base_ns=20_000_000, step_ns=5_000_000),
            )
        with self.assertRaisesRegex(GroupInvariantError, "elapsed time did not advance"):
            self.repository.record_stream_measurements(
                group.session_id, "A",
                self.frame_samples(stream_a.benchmark_id, (13,), base_ns=14_000_000, step_ns=5_000_000),
            )
        with self.assertRaisesRegex(GroupInvariantError, "another stream"):
            self.repository.record_stream_measurements(
                group.session_id, "A",
                self.frame_samples(group.streams[1].benchmark_id, (1,), base_ns=0, step_ns=1),
            )

    def test_sequence_gap_without_reported_queue_loss_marks_dataset_incomplete(self) -> None:
        group = self.create_group()
        self.start_group(group)
        stream = group.streams[0]
        self.repository.record_stream_measurements(
            group.session_id, "A",
            self.frame_samples(stream.benchmark_id, (4, 6), base_ns=1_000_000, step_ns=5_000_000),
        )
        current = self.repository.get_group(group.session_id)
        self.assertEqual(current.status, "degraded")
        self.assertFalse(current.data_complete)
        self.assertEqual(current.streams[0].timing_queue_loss_count, 0)
        self.assertEqual(current.streams[0].sequence_gap_count, 1)
        self.assertIn("sample_sequence_gap", current.integrity_flags)
        self.assertIn("sample_sequence_gap", current.streams[0].integrity_flags)
        self.assertTrue(any(
            event.event_type == "measurement_sequence_gap"
            and event.reason_code == "sample_sequence_gap"
            for event in current.events
        ))

    def test_future_driver_timestamp_values_remain_optional_exact_integers(self) -> None:
        group = self.create_group()
        self.start_group(group)
        benchmark_id = group.streams[0].benchmark_id
        sample = replace(
            self.frame_samples(benchmark_id, (7,), base_ns=123_456_789, step_ns=5_000_000)[0],
            source_timestamp_ns=8_765_432_109_876_543,
            source_sequence=9_007_199_254_741,
            source_clock="synthetic_v4l2_clock",
        )
        self.repository.record_stream_measurements(group.session_id, "A", (sample,))
        with self.database.connect_readonly() as connection:
            row = connection.execute(
                """SELECT elapsed_ns, source_timestamp_ns, source_sequence, source_clock
                   FROM measurements WHERE benchmark_id=?""",
                (benchmark_id,),
            ).fetchone()
        self.assertEqual(row["elapsed_ns"], 123_456_789)
        self.assertEqual(row["source_timestamp_ns"], 8_765_432_109_876_543)
        self.assertEqual(row["source_sequence"], 9_007_199_254_741)
        self.assertEqual(row["source_clock"], "synthetic_v4l2_clock")

    def test_completed_group_requires_both_streams_loss_free_and_full_duration(self) -> None:
        group = self.create_group()
        self.start_group(group)
        for stream, step in zip(group.streams, (5_347_594, 41_666_667)):
            self.repository.record_stream_measurements(
                group.session_id, stream.slot,
                self.frame_samples(stream.benchmark_id, (1, 2, 3), base_ns=0, step_ns=step),
            )
            self.repository.increment_stream_counters(
                group.session_id, stream.slot, {"read_failure_count": 1},
                observed_at="synthetic-read-failure",
            )
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at="synthetic-stop", elapsed_ns=30_000_000_000,
            )
        result = self.repository.finalize_group(
            group.session_id, GroupStatus.COMPLETED,
            ended_at="synthetic-end", duration_by_slot={"A": 30, "B": 30},
            termination_reason="duration_elapsed",
        )
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.data_complete)
        self.assertEqual([stream.read_failure_count for stream in result.streams], [1, 1])
        self.assertGreater(result.streams[0].summary["capture_fps"], result.streams[1].summary["capture_fps"])
        self.assertEqual([stream.summary["frame_count"] for stream in result.streams], [3, 3])
        self.assertEqual(result.streams[0].summary["duration_seconds"], 30.0)

    def test_group_sample_batch_rolls_back_when_a_later_row_violates_sql_constraints(self) -> None:
        group = self.create_group()
        self.start_group(group)
        stream = group.streams[0]
        first = self.frame_samples(stream.benchmark_id, (1,), base_ns=1_000_000, step_ns=5_000_000)[0]
        second = replace(
            self.frame_samples(stream.benchmark_id, (2,), base_ns=2_000_000, step_ns=5_000_000)[0],
            stream_scope="not-a-valid-scope",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.record_stream_measurements(
                group.session_id, "A", (first, second)
            )
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM measurements WHERE benchmark_id=?", (stream.benchmark_id,)
            ).fetchone()[0], 0)
            state = connection.execute(
                """SELECT last_persisted_sample_index, last_persisted_elapsed_ns
                   FROM benchmark_streams WHERE session_id=? AND slot='A'""",
                (group.session_id,),
            ).fetchone()
        self.assertIsNone(state["last_persisted_sample_index"])
        self.assertIsNone(state["last_persisted_elapsed_ns"])

    def test_queue_loss_prevents_completed_and_partial_outcome_is_explicit(self) -> None:
        group = self.create_group()
        self.start_group(group)
        for stream in group.streams:
            self.repository.record_stream_measurements(
                group.session_id, stream.slot,
                self.frame_samples(stream.benchmark_id, (0, 1), base_ns=0, step_ns=10_000_000),
            )
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at="synthetic-stream-end", elapsed_ns=30_000_000_000,
            )
        self.repository.increment_stream_counters(
            group.session_id, "A", {"timing_queue_loss_count": 2},
            observed_at="synthetic-queue-loss",
        )
        with self.assertRaisesRegex(GroupInvariantError, "degraded group"):
            self.repository.finalize_group(
                group.session_id, GroupStatus.COMPLETED,
                ended_at="synthetic-end", duration_by_slot={"A": 30, "B": 30},
                termination_reason="duration_elapsed",
            )
        result = self.repository.finalize_group(
            group.session_id, GroupStatus.PARTIAL,
            ended_at="synthetic-end", duration_by_slot={"A": 30, "B": 30},
            termination_reason="timing_queue_overflow",
        )
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.data_complete)
        self.assertEqual(result.streams[0].timing_queue_loss_count, 2)

    def test_completed_group_requires_each_stream_to_have_at_least_one_arrival(self) -> None:
        group = self.create_group()
        self.start_group(group)
        self.repository.record_stream_measurements(
            group.session_id, "A",
            self.frame_samples(group.streams[0].benchmark_id, (0, 1), base_ns=0, step_ns=10_000_000),
        )
        for stream in group.streams:
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at="synthetic-end", elapsed_ns=30_000_000_000,
            )
        with self.assertRaisesRegex(GroupInvariantError, "useful, completed"):
            self.repository.finalize_group(
                group.session_id, GroupStatus.COMPLETED,
                ended_at="synthetic-end", duration_by_slot={"A": 30, "B": 30},
                termination_reason="duration_elapsed",
            )

    def test_completed_group_duration_must_match_persisted_monotonic_lifecycle_events(self) -> None:
        group = self.create_group()
        self.start_group(group)
        for stream in group.streams:
            self.repository.record_stream_measurements(
                group.session_id, stream.slot,
                self.frame_samples(stream.benchmark_id, (0,), base_ns=0, step_ns=10_000_000),
            )
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at="synthetic-end", elapsed_ns=30_000_000_000,
            )
        with self.assertRaisesRegex(GroupInvariantError, "does not match its monotonic lifecycle"):
            self.repository.finalize_group(
                group.session_id, GroupStatus.COMPLETED,
                ended_at="synthetic-end", duration_by_slot={"A": 31, "B": 30},
                termination_reason="duration_elapsed",
            )

    def test_completed_group_requires_monotonic_lifecycle_events(self) -> None:
        group = self.create_group()
        self.start_group(group)
        for stream in group.streams:
            self.repository.record_stream_measurements(
                group.session_id, stream.slot,
                self.frame_samples(stream.benchmark_id, (0,), base_ns=0, step_ns=10_000_000),
            )
            self.repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.COMPLETED,
                observed_at="synthetic-end", elapsed_ns=30_000_000_000,
            )
        with self.database.connect() as connection:
            connection.execute(
                """DELETE FROM experiment_events
                   WHERE session_id=? AND benchmark_id=? AND event_type='stream_completed'""",
                (group.session_id, group.streams[0].benchmark_id),
            )
        with self.assertRaisesRegex(GroupInvariantError, "requires monotonic start and completion events"):
            self.repository.finalize_group(
                group.session_id, GroupStatus.COMPLETED,
                ended_at="synthetic-end", duration_by_slot={"A": 30, "B": 30},
                termination_reason="duration_elapsed",
            )

    def test_partial_start_failure_keeps_other_stream_independent(self) -> None:
        group = self.create_group()
        self.repository.set_stream_status(
            group.session_id, "A", StreamStatus.FAILED,
            observed_at="synthetic-failure", reason_code="open_failed",
        )
        self.repository.set_stream_status(
            group.session_id, "B", StreamStatus.RECORDING,
            observed_at="synthetic-ready",
        )
        self.repository.set_group_status(
            group.session_id, GroupStatus.DEGRADED,
            observed_at="synthetic-partial-start", reason_code="partial_start",
        )
        current = self.repository.get_group(group.session_id)
        self.assertEqual([stream.stream_status for stream in current.streams], ["failed", "recording"])
        self.assertEqual(current.status, "degraded")
        with self.assertRaisesRegex(GroupInvariantError, "both child streams"):
            self.repository.validate_ready_to_record(group.session_id)

    def test_camera_deletion_sets_fk_null_but_keeps_identity_snapshot(self) -> None:
        group = self.create_group()
        identity_before = dict(group.streams[0].identity_snapshot)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM cameras WHERE id=?", (group.streams[0].camera_id,))
        current = self.repository.get_group(group.session_id)
        self.assertIsNone(current.streams[0].camera_id)
        self.assertIsNone(current.streams[0].camera_record)
        self.assertEqual(dict(current.streams[0].identity_snapshot), identity_before)
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_event_validation_and_child_foreign_keys(self) -> None:
        group = self.create_group()
        event_id = self.repository.add_event(
            group.session_id, "synthetic_marker", observed_at="synthetic-time",
            elapsed_ns=123, reason_code="test_event", details={"counter": 2},
        )
        current = self.repository.get_group(group.session_id)
        event = next(event for event in current.events if event.event_id == event_id)
        self.assertEqual(event.details, {"counter": 2})
        self.assertEqual(event.elapsed_ns, 123)
        with self.assertRaisesRegex(ValueError, "reason_code"):
            self.repository.add_event(
                group.session_id, "bad", observed_at="synthetic",
                reason_code="bad reason code",
            )
        with self.database.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO experiment_events
                       (session_id, benchmark_id, event_type, observed_at)
                       VALUES (?, 'missing-run', 'bad_child', 'synthetic')""",
                    (group.session_id,),
                )

    def test_read_queries_are_query_only_and_do_not_change_group_or_sample_data(self) -> None:
        group = self.create_group()
        with self.database.connect_readonly() as connection:
            before = tuple(connection.execute(
                "SELECT session_id, status, data_complete FROM group_recordings"
            ).fetchall())
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "UPDATE group_recordings SET status='failed' WHERE session_id=?",
                    (group.session_id,),
                )
        self.assertEqual(self.repository.get_group(group.session_id).status, "pending")
        with self.database.connect_readonly() as connection:
            after = tuple(connection.execute(
                "SELECT session_id, status, data_complete FROM group_recordings"
            ).fetchall())
        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])

    def test_recording_lease_is_single_owner_and_release_checks_token(self) -> None:
        group = self.create_group()
        lease = self.repository.acquire_recording_lease(
            group.session_id, owner_pid=123, owner_process_start_ticks="synthetic-start",
            acquired_at="synthetic-time", owner_token="synthetic-owner-a",
        )
        self.assertEqual(self.repository.get_recording_lease(), lease)
        with self.assertRaises(RecordingLeaseBusyError):
            self.repository.acquire_recording_lease(
                group.session_id, owner_pid=456, owner_process_start_ticks="other-start",
                acquired_at="synthetic-time", owner_token="synthetic-owner-b",
            )
        with self.assertRaises(RecordingLeaseOwnershipError):
            self.repository.release_recording_lease("synthetic-owner-b")
        self.assertEqual(self.repository.get_recording_lease(), lease)
        self.repository.release_recording_lease(lease.owner_token)
        self.assertIsNone(self.repository.get_recording_lease())

    def test_measurement_nanosecond_fields_reject_float_negative_bool_and_overflow(self) -> None:
        args = {
            "benchmark_id": "synthetic-run",
            "metric": "frame_arrival",
            "value": 1,
            "unit": "frames/s",
            "stream_scope": StreamScope.CAPTURE,
        }
        with self.assertRaises(TypeError):
            MeasurementSample(**args, elapsed_ns=1.5)
        with self.assertRaises(ValueError):
            MeasurementSample(**args, elapsed_ns=-1)
        with self.assertRaises(ValueError):
            MeasurementSample(**args, source_sequence=SQLITE_INT_MAX + 1)
        with self.assertRaises(TypeError):
            MeasurementSample(**args, source_timestamp_ns=True)
        with self.assertRaisesRegex(ValueError, "source_clock is required"):
            MeasurementSample(**args, source_timestamp_ns=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
