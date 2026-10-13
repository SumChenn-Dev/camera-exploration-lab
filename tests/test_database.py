from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.db import Database, SCHEMA_VERSION
from camera_exploration_lab.models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    MeasurementSample,
    SessionRecord,
    SettingSnapshot,
    StreamScope,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_schema_and_core_records_round_trip(self) -> None:
        camera = CameraIdentity(
            stable_identifier="/dev/v4l/by-id/usb-mock-video-index0",
            backend=BackendKind.V4L2,
            device_path="/dev/video5",
            manufacturer="Mock",
            model="Mock Camera",
            identity_source="v4l_by_id",
        )
        camera_id = self.database.upsert_camera(camera, seen_at="2026-10-13T12:00:00+00:00")
        session = SessionRecord(camera_id=camera_id, label="synthetic run")
        self.database.create_session(session)
        benchmark = BenchmarkRecord(session_id=session.id, label="640x480 @ 60")
        self.database.create_benchmark(benchmark)

        self.database.record_setting(
            SettingSnapshot(
                session_id=session.id,
                benchmark_id=benchmark.id,
                setting_key="frame_rate",
                requested_value=60,
                reported_value=59.9,
                verification_status="reported",
                verification_method="mock driver readback",
            )
        )
        self.database.record_setting(
            SettingSnapshot(
                session_id=session.id,
                benchmark_id=benchmark.id,
                setting_key="exposure",
                requested_value=50,
                verified_value=50,
                verification_status="verified",
                verification_method="mock camera control query",
            )
        )
        self.database.record_measurement(
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="capture_fps",
                value=60.02,
                unit="frames/s",
                stream_scope=StreamScope.CAPTURE,
                sample_index=601,
                method="mock capture API timestamps",
            )
        )
        self.database.record_measurement(
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="preview_fps",
                value=30.0,
                unit="frames/s",
                stream_scope=StreamScope.PREVIEW,
            )
        )
        self.database.record_measurement(
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="processing_fps",
                value=28.0,
                unit="frames/s",
                stream_scope=StreamScope.PROCESSING,
            )
        )

        with self.database.connect() as connection:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            settings = connection.execute(
                "SELECT setting_key, requested_value_json, reported_value_json, "
                "verified_value_json, verification_status FROM setting_snapshots ORDER BY setting_key"
            ).fetchall()
            measurements = connection.execute(
                "SELECT metric, stream_scope FROM measurements ORDER BY metric"
            ).fetchall()

        self.assertEqual(schema_version, SCHEMA_VERSION)
        self.assertEqual(settings[0]["verified_value_json"], "50")
        self.assertEqual(settings[0]["verification_status"], "verified")
        self.assertEqual(settings[1]["requested_value_json"], "60")
        self.assertEqual(settings[1]["reported_value_json"], "59.9")
        self.assertIsNone(settings[1]["verified_value_json"])
        self.assertEqual(settings[1]["verification_status"], "reported")
        self.assertEqual([(row["metric"], row["stream_scope"]) for row in measurements], [
            ("capture_fps", "capture"),
            ("preview_fps", "preview"),
            ("processing_fps", "processing"),
        ])

    def test_foreign_keys_reject_measurements_without_a_benchmark(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.record_measurement(
                MeasurementSample(benchmark_id="missing", metric="capture_fps", value=1, unit="frames/s")
            )

    def test_initialize_is_repeatable(self) -> None:
        self.database.initialize()
        self.database.initialize()
        self.assertEqual(self.database.count("cameras"), 0)

    def test_schema_v1_upgrade_preserves_existing_imported_measurements(self) -> None:
        camera_id = self.database.upsert_camera(
            CameraIdentity("legacy-camera", BackendKind.UNKNOWN, identity_source="inferred")
        )
        session = SessionRecord(camera_id=camera_id, label="Imported old run", source="imported")
        self.database.create_session(session)
        benchmark = BenchmarkRecord(session_id=session.id, label="old mode", status="imported")
        self.database.create_benchmark(benchmark)
        self.database.record_measurement(
            MeasurementSample(benchmark.id, "capture_fps", 23.9, "frames/s", StreamScope.CAPTURE)
        )
        before = self.database.count("measurements")

        # Model the additive table/index changes that an existing v1 file lacks.
        with self.database.connect() as connection:
            connection.execute("DROP INDEX idx_measurements_benchmark_metric_sample")
            connection.execute("DROP INDEX idx_measurements_benchmark_interval")
            connection.execute("DROP TABLE benchmark_summaries")
            connection.execute("PRAGMA user_version = 1")

        self.database.initialize()

        with self.database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            summary_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='benchmark_summaries'"
            ).fetchone()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIsNotNone(summary_table)
        self.assertEqual(self.database.count("measurements"), before)
        self.assertEqual(self.database.history_entries()[0]["session_label"], "Imported old run")

    def test_history_lists_imported_sources_and_separates_scope_summary(self) -> None:
        session = SessionRecord(camera_id=None, label="Imported: old.csv", source="imported")
        self.database.create_session(session)
        benchmark = BenchmarkRecord(session_id=session.id, label="legacy 720p", status="imported")
        self.database.create_benchmark(benchmark)
        self.database.record_measurement(
            MeasurementSample(benchmark.id, "capture_fps", 23.9, "frames/s", StreamScope.CAPTURE)
        )
        self.database.record_measurement(
            MeasurementSample(benchmark.id, "preview_fps", 30.0, "frames/s", StreamScope.PREVIEW)
        )

        entries = self.database.history_entries()
        detail = self.database.history_detail(benchmark.id, session.id)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "imported")
        self.assertAlmostEqual(entries[0]["capture_fps"], 23.9)
        self.assertAlmostEqual(entries[0]["preview_fps"], 30.0)
        self.assertEqual({(row["metric"], row["stream_scope"]) for row in detail["metrics"]}, {
            ("capture_fps", "capture"), ("preview_fps", "preview")
        })

    def test_startup_recovery_marks_dead_live_run_interrupted_using_last_committed_sample(self) -> None:
        camera = CameraIdentity(
            "recovery-camera", BackendKind.V4L2, identity_source="synthetic"
        )
        session = SessionRecord(
            camera_id=camera.stable_identifier,
            label="Crash interrupted run",
            started_at="2026-10-13T12:00:00+00:00",
            metadata={"owner_pid": 2_000_000_000, "owner_process_start_ticks": "1"},
        )
        benchmark = BenchmarkRecord(session_id=session.id, label="mock", started_at=session.started_at)
        self.database.begin_live_experiment(camera, session, benchmark, [], seen_at=session.started_at)
        self.database.record_measurement(
            MeasurementSample(
                benchmark.id, "frame_arrival", 0.5, "seconds", StreamScope.CAPTURE,
                elapsed_seconds=0.5, sample_index=1,
            )
        )

        recovered = self.database.recover_interrupted_experiments(
            ended_at="2026-10-13T12:01:00+00:00"
        )

        self.assertEqual(recovered, 1)
        entry = next(row for row in self.database.history_entries() if row["benchmark_id"] == benchmark.id)
        self.assertEqual(entry["status"], "interrupted")
        self.assertAlmostEqual(entry["duration_seconds"], 0.5)
        detail = self.database.history_detail(benchmark.id, session.id)
        self.assertIn("last_committed_sample", detail["entry"]["summary_json"])

    def test_in_memory_database_survives_connection_boundaries(self) -> None:
        database = Database(":memory:")
        try:
            database.initialize()
            database.create_session(SessionRecord(camera_id=None, label="in-memory test"))
            self.assertEqual(database.count("sessions"), 1)
        finally:
            database.close()

    def test_initialize_refuses_a_newer_database_schema(self) -> None:
        with self.database.connect() as connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            self.database.initialize()


if __name__ == "__main__":
    unittest.main(verbosity=2)
