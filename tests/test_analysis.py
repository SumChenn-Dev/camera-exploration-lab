from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.analysis import (
    compare_methodology,
    filter_experiments,
    list_experiments,
    load_analysis,
    rate_deviation_percent,
)
from camera_exploration_lab.db import Database
from camera_exploration_lab.models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    MeasurementSample,
    SessionRecord,
    SettingSnapshot,
    StreamScope,
)


class ExperimentAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "analysis.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.camera = CameraIdentity(
            stable_identifier="/dev/v4l/by-id/usb-synthetic-camera-video-index0",
            backend=BackendKind.V4L2,
            manufacturer="Synthetic",
            model="Synthetic Capture Camera",
            identity_source="v4l_by_id",
        )
        self.camera_id = self.database.upsert_camera(self.camera)

    def tearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    def _create_live_run(
        self,
        label: str,
        intervals_ms: list[float],
        *,
        failures: int = 0,
        status: str = "completed",
        method: str = "difference between successive host monotonic read-return timestamps",
        fps: float = 187.0,
        reported_fps: float | None = None,
    ) -> tuple[str, str]:
        started = "2026-10-13T12:00:00+00:00"
        session = SessionRecord(
            camera_id=self.camera_id,
            label=label,
            started_at=started,
            source="live_experiment",
            notes="synthetic lab notes",
        )
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id,
            label="Synthetic mode",
            started_at=started,
            status=status,
            configuration={
                "requested": {
                    "width": 320,
                    "height": 240,
                    "pixel_format": "GRBG",
                    "fps": fps,
                },
                "driver_reported": {
                    "width": 320,
                    "height": 240,
                    "pixel_format": "GRBG",
                    "fps": fps if reported_fps is None else reported_fps,
                },
                "capture_timing_method": "host monotonic timestamp after read",
            },
        )
        self.database.create_benchmark(benchmark)
        self.database.record_setting(SettingSnapshot(
            session_id=session.id,
            benchmark_id=benchmark.id,
            setting_key="fps",
            requested_value=fps,
            reported_value=fps if reported_fps is None else reported_fps,
            verification_status="reported",
        ))

        elapsed = 0.0
        samples: list[MeasurementSample] = []
        for index in range(len(intervals_ms) + 1):
            if index:
                elapsed += intervals_ms[index - 1] / 1000.0
            interval_ns = int(intervals_ms[index - 1] * 1_000_000) if index else None
            samples.append(MeasurementSample(
                benchmark_id=benchmark.id,
                metric="frame_arrival",
                value=elapsed,
                unit="seconds",
                stream_scope=StreamScope.CAPTURE,
                observed_at=f"2026-10-13T12:00:{index:02d}.000+00:00",
                elapsed_seconds=elapsed,
                sample_index=index + 1,
                interval_ns=interval_ns,
                method="host monotonic timestamp immediately after OpenCV read returned",
            ))
            if index:
                interval = intervals_ms[index - 1]
                samples.append(MeasurementSample(
                    benchmark_id=benchmark.id,
                    metric="frame_interval_ms",
                    value=interval,
                    unit="ms",
                    stream_scope=StreamScope.CAPTURE,
                    observed_at=f"2026-10-13T12:00:{index:02d}.000+00:00",
                    elapsed_seconds=elapsed,
                    sample_index=index + 1,
                    interval_ns=interval_ns,
                    method=method,
                ))
        for index in range(failures):
            samples.append(MeasurementSample(
                benchmark_id=benchmark.id,
                metric="read_failure",
                value=1,
                unit="count",
                stream_scope=StreamScope.CAPTURE,
                elapsed_seconds=elapsed + index * 0.01,
                sample_index=len(intervals_ms) + 1,
                method="unsuccessful OpenCV VideoCapture.read call",
            ))
        samples.extend((
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="preview_fps",
                value=31.2,
                unit="frames/s",
                stream_scope=StreamScope.PREVIEW,
                elapsed_seconds=0.5,
                method="Qt preview cadence",
            ),
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="preview_fps",
                value=30.8,
                unit="frames/s",
                stream_scope=StreamScope.PREVIEW,
                elapsed_seconds=1.0,
                method="Qt preview cadence",
            ),
        ))
        self.database.record_measurements_batch(samples)
        self.database.finish_live_experiment(
            benchmark.id,
            ended_at="2026-10-13T12:00:02+00:00",
            duration_seconds=max(elapsed, 0.001),
            status=status,
            termination_reason="synthetic_end",
        )
        return session.id, benchmark.id

    def _create_imported_run(self, label: str) -> tuple[str, str]:
        session = SessionRecord(
            camera_id=None,
            label=label,
            source="imported",
            notes="legacy notes",
        )
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id,
            label="Legacy 640x480",
            status="imported",
            configuration={"legacy_mode": "640x480"},
        )
        self.database.create_benchmark(benchmark)
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="capture_frame_interval_ms",
                value=value,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                sample_index=index,
                method="legacy_csv:interval_ms",
            )
            for index, value in enumerate((40.0, 41.0, 120.0), start=1)
        ])
        return session.id, benchmark.id

    def test_interval_percentiles_capture_fps_and_separate_preview_rate(self) -> None:
        session_id, benchmark_id = self._create_live_run(
            "Five synthetic intervals", [10, 20, 30, 40, 50], failures=2,
            fps=187, reported_fps=180,
        )
        result = load_analysis(self.database, benchmark_id, session_id)

        self.assertEqual(result.interval_stats.count, 5)
        self.assertEqual(result.interval_stats.minimum, 10.0)
        self.assertEqual(result.interval_stats.maximum, 50.0)
        self.assertEqual(result.interval_stats.mean, 30.0)
        self.assertEqual(result.interval_stats.median, 30.0)
        self.assertEqual(result.interval_stats.p95, 50.0)
        self.assertEqual(result.interval_stats.p99, 50.0)
        self.assertAlmostEqual(result.capture_fps_stats.mean or 0.0, 1000 / 30)
        self.assertAlmostEqual(result.capture_fps_stats.median or 0.0, 1000 / 30)
        self.assertAlmostEqual(result.preview_fps_mean or 0.0, 31.0)
        self.assertEqual(result.processing_fps_mean, None)
        self.assertEqual(result.frame_count, 6)
        self.assertEqual(result.read_failures, 2)
        self.assertAlmostEqual(result.duration_seconds or 0.0, 0.15)
        self.assertEqual(result.requested_settings["fps"], 187)
        self.assertEqual(result.driver_settings["fps"], 180)
        self.assertEqual(result.notes, "synthetic lab notes")
        self.assertTrue(result.fps_points)
        self.assertIn("non-overlapping", result.fps_method)
        self.assertIn("read-return", result.timing_method or "")

    def test_empty_and_single_sample_series_are_graceful(self) -> None:
        empty_session, empty_benchmark = self._create_live_run("No intervals", [])
        one_session, one_benchmark = self._create_live_run("One interval", [16.7])
        zero_session, zero_benchmark = self._create_live_run("Zero and positive", [0.0, 10.0])
        empty = load_analysis(self.database, empty_benchmark, empty_session)
        one = load_analysis(self.database, one_benchmark, one_session)
        zero = load_analysis(self.database, zero_benchmark, zero_session)

        self.assertEqual(empty.interval_stats.count, 0)
        self.assertEqual(empty.interval_points, ())
        self.assertIsNone(empty.capture_fps_stats.mean)
        self.assertEqual(one.interval_stats.count, 1)
        self.assertEqual(one.interval_stats.median, 16.7)
        self.assertEqual(one.interval_stats.p95, 16.7)
        self.assertAlmostEqual(one.capture_fps_stats.mean or 0.0, 1000 / 16.7)
        self.assertEqual(zero.interval_stats.count, 2)
        self.assertEqual(zero.interval_stats.minimum, 0.0)
        self.assertAlmostEqual(zero.capture_fps_stats.mean or 0.0, 200.0)
        self.assertAlmostEqual(zero.fps_points[0][1], 200.0)
        self.assertEqual(zero.interval_histogram.zero_sample_count, 1)
        self.assertEqual(zero.interval_histogram.positive_sample_count, 1)
        self.assertAlmostEqual(sum(zero.interval_histogram.fractions), 1.0)

    def test_interrupted_session_and_read_failures_remain_interrupted(self) -> None:
        session_id, benchmark_id = self._create_live_run(
            "Interrupted synthetic run", [5.3, 5.4, 14.0], failures=3, status="interrupted"
        )
        result = load_analysis(self.database, benchmark_id, session_id)
        self.assertEqual(result.saved_status, "interrupted")
        self.assertEqual(result.read_failures, 3)
        self.assertEqual(result.termination_reason, "synthetic_end")

    def test_imported_run_uses_legacy_interval_method_and_shows_unknown_fields(self) -> None:
        session_id, benchmark_id = self._create_imported_run("Imported synthetic CSV")
        result = load_analysis(self.database, benchmark_id, session_id)
        self.assertEqual(result.option.source, "imported")
        self.assertEqual(result.interval_stats.count, 3)
        self.assertEqual(result.interval_stats.p95, 120.0)
        self.assertIn("legacy_csv", result.interval_method or "")
        self.assertIsNone(result.duration_seconds)
        self.assertIsNone(result.frame_count)
        self.assertIsNone(result.read_failures)
        self.assertAlmostEqual(result.capture_fps_stats.mean or 0.0, 1000 / 67)
        self.assertEqual(result.configuration_note, "Imported mode: 640x480")

    def test_comparison_calls_out_different_import_timing_method(self) -> None:
        live_session, live_benchmark = self._create_live_run("Live", [5.3, 5.4])
        imported_session, imported_benchmark = self._create_imported_run("Imported")
        live = load_analysis(self.database, live_benchmark, live_session)
        imported = load_analysis(self.database, imported_benchmark, imported_session)
        messages = [notice.message for notice in compare_methodology(live, imported)]
        self.assertTrue(any("methods differ" in message for message in messages))
        self.assertTrue(any("imported history" in message.lower() for message in messages))

    def test_different_requested_modes_do_not_replace_delivered_rates(self) -> None:
        slow_session, slow_benchmark = self._create_live_run(
            "Requested 60", [16.7, 16.6, 16.7], fps=60, reported_fps=60
        )
        fast_session, fast_benchmark = self._create_live_run(
            "Requested 187", [5.3, 5.4, 5.3], fps=187, reported_fps=187
        )
        slow = load_analysis(self.database, slow_benchmark, slow_session)
        fast = load_analysis(self.database, fast_benchmark, fast_session)
        self.assertEqual(slow.requested_settings["fps"], 60)
        self.assertEqual(fast.requested_settings["fps"], 187)
        self.assertAlmostEqual(slow.capture_fps_stats.mean or 0.0, 1000 / 16.6666666667, places=2)
        self.assertAlmostEqual(fast.capture_fps_stats.mean or 0.0, 1000 / 5.3333333333, places=1)
        self.assertGreater(fast.capture_fps_stats.mean or 0.0, slow.capture_fps_stats.mean or 0.0)

    def test_experiment_filters_use_camera_mode_and_requested_fps(self) -> None:
        slow_session, _ = self._create_live_run(
            "PS3 Eye 60 FPS baseline", [16.7, 16.6], fps=60, reported_fps=60
        )
        self._create_live_run("PS3 Eye 187 FPS run", [5.3, 5.4], fps=187, reported_fps=187)
        options = list_experiments(self.database)
        slow = next(option for option in options if option.session_id == slow_session)
        filtered = filter_experiments(
            options,
            camera_key=slow.camera_key,
            mode_label=slow.mode_label,
            requested_fps=60,
            search="baseline",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].session_id, slow_session)
        self.assertEqual(filtered[0].requested_fps, 60.0)
        self.assertIn("320×240", filtered[0].mode_label)
        self.assertNotEqual(filtered[0].mode_label, "driver-reported 60 FPS")

    def test_camera_family_browsing_keeps_distinct_identity_provenance(self) -> None:
        legacy_camera = CameraIdentity(
            "legacy:sony-playstation-eye:1415:2000", BackendKind.V4L2,
            manufacturer="Sony", model="PlayStation Eye", vendor_id="1415",
            product_id="2000", identity_source="inferred_from_legacy_path",
        )
        discovered_camera = CameraIdentity(
            "/dev/v4l/by-id/usb-synthetic-ps3eye", BackendKind.V4L2,
            manufacturer="Sony", model="PlayStation Eye", vendor_id="1415",
            product_id="2000", identity_source="v4l_by_id",
        )
        legacy_id = self.database.upsert_camera(legacy_camera)
        discovered_id = self.database.upsert_camera(discovered_camera)
        pairs = []
        for camera_id, label, source in (
            (legacy_id, "Imported Eye", "imported"),
            (discovered_id, "Native Eye", "live_experiment"),
        ):
            session = SessionRecord(camera_id=camera_id, label=label, source=source)
            self.database.create_session(session)
            benchmark = BenchmarkRecord(session_id=session.id, label="Synthetic mode", status="completed")
            self.database.create_benchmark(benchmark)
            pairs.append((session, benchmark))

        options = list_experiments(self.database)
        eye_options = [option for option in options if option.camera == "PlayStation Eye"]
        self.assertEqual(len(eye_options), 2)
        self.assertNotEqual(eye_options[0].camera_key, eye_options[1].camera_key)
        self.assertEqual(eye_options[0].camera_group_key, eye_options[1].camera_group_key)
        self.assertEqual(
            {option.identity_source for option in eye_options},
            {"inferred_from_legacy_path", "v4l_by_id"},
        )
        grouped = filter_experiments(options, camera_group_key="usb-family:1415:2000")
        self.assertEqual({option.session_id for option in grouped}, {pair[0].id for pair in pairs})

    def test_requested_mode_filters_fall_back_to_setting_snapshots(self) -> None:
        session_id, benchmark_id = self._create_live_run(
            "Snapshot-only requested mode", [16.7], fps=60, reported_fps=59.9
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE benchmark_runs SET configuration_json='{}' WHERE id=?",
                (benchmark_id,),
            )
        option = next(o for o in list_experiments(self.database) if o.session_id == session_id)
        self.assertEqual(option.requested_fps, 60.0)
        self.assertIn("60 FPS requested", option.mode_label)

    def test_normalized_rate_curve_preserves_samples_without_interpolation(self) -> None:
        points = ((0.4, 60.0), (1.5, 66.0), (4.0, 54.0))
        normalized = rate_deviation_percent(points, 60.0)
        self.assertEqual(len(normalized), len(points))
        self.assertEqual(tuple(x for x, _ in normalized), tuple(x for x, _ in points))
        self.assertAlmostEqual(normalized[0][1], 0.0)
        self.assertAlmostEqual(normalized[1][1], 10.0)
        self.assertAlmostEqual(normalized[2][1], -10.0)
        self.assertEqual(rate_deviation_percent(points, None), ())
        self.assertEqual(rate_deviation_percent(points, 0.0), ())

        high_rate = rate_deviation_percent(
            ((0.5, 187.35), (15.0, 187.50), (29.5, 187.20)), 187.35
        )
        self.assertAlmostEqual(high_rate[0][1], 0.0)
        self.assertAlmostEqual(high_rate[1][1], 100.0 * (187.50 - 187.35) / 187.35)
        self.assertAlmostEqual(high_rate[2][1], 100.0 * (187.20 - 187.35) / 187.35)
        valid = rate_deviation_percent(
            ((0.0, 60.0), (1.0, 60.6), (2.0, 59.4), (3.0, float("nan")), (float("inf"), 60.0)),
            60.0,
        )
        self.assertEqual(len(valid), 3)
        self.assertAlmostEqual(valid[0][1], 0.0)
        self.assertAlmostEqual(valid[1][1], 1.0)
        self.assertAlmostEqual(valid[2][1], -1.0)
        extreme = rate_deviation_percent(((1.0, 300.0),), 187.35)
        self.assertAlmostEqual(extreme[0][1], 100.0 * (300.0 - 187.35) / 187.35)

    def test_large_series_is_bounded_and_preserves_global_outliers(self) -> None:
        session = SessionRecord(
            camera_id=self.camera_id,
            label="Large synthetic run",
            source="live_experiment",
            started_at="2026-10-13T12:00:00+00:00",
        )
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id,
            label="Large timing fixture",
            status="interrupted",
        )
        self.database.create_benchmark(benchmark)
        count = 12_000
        samples = []
        for index in range(count):
            value = 5.0 + (index % 11) * 0.01
            if index == 3:
                value = 0.4
            if index == 9_875:
                value = 4000.0
            samples.append(MeasurementSample(
                benchmark_id=benchmark.id,
                metric="frame_interval_ms",
                value=value,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                elapsed_seconds=index * value / 1000.0,
                sample_index=index + 1,
                interval_ns=int(value * 1_000_000),
                method="synthetic elapsed intervals",
            ))
        self.database.record_measurements_batch(samples)

        result = load_analysis(
            self.database, benchmark.id, session.id, max_plot_points=240
        )
        self.assertEqual(result.interval_stats.count, count)
        self.assertLessEqual(len(result.interval_points), 240)
        values = [point[1] for point in result.interval_points]
        self.assertIn(0.4, values)
        self.assertIn(4000.0, values)
        self.assertLessEqual(len(result.fps_points), 240)

    def test_analysis_uses_query_only_connection_and_does_not_change_database(self) -> None:
        session_id, benchmark_id = self._create_live_run("Read-only fixture", [8.0, 9.0])
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM sessions")
        load_analysis(self.database, benchmark_id, session_id)
        list_experiments(self.database)
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_summary_only_import_remains_discoverable_without_invented_series(self) -> None:
        session = SessionRecord(camera_id=None, label="Summary only", source="imported")
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id, label="Old summary", status="imported",
            configuration={"legacy_mode": "mode missing timings"},
        )
        self.database.create_benchmark(benchmark)
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric=metric,
                value=value,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                method=f"legacy_csv:{metric}",
            )
            for metric, value in (
                ("frame_interval_median_ms", 16.0),
                ("frame_interval_p95_ms", 20.0),
                ("frame_interval_p99_ms", 25.0),
            )
        ])
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO benchmark_summaries
                   (benchmark_id, frame_count, read_failures, duration_seconds,
                    interval_count, median_interval_ms, p95_interval_ms, p99_interval_ms)
                   VALUES (?, 400, 2, 12.5, 399, 16.0, 20.0, 25.0)""",
                (benchmark.id,),
            )
        result = load_analysis(self.database, benchmark.id, session.id)
        self.assertEqual(result.interval_stats.count, 399)
        self.assertTrue(result.interval_stats.from_saved_summary)
        self.assertEqual(result.interval_stats.p95, 20.0)
        self.assertEqual(result.interval_points, ())
        self.assertEqual(result.frame_count, 400)
        self.assertEqual(result.read_failures, 2)
        self.assertEqual(result.duration_seconds, 12.5)
        self.assertFalse(result.interval_data_available)
        self.assertEqual(
            result.reported_interval_summary["p95 interval field"].mean,
            20.0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
