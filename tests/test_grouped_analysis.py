from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.grouped_analysis import load_grouped_analysis
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    StreamStatus,
)
from camera_exploration_lab.models import MeasurementSample, StreamScope
from camera_exploration_lab.schema_migrations import migrate_synthetic_database_to_v3
from camera_exploration_lab.ui.synthetic_grouped_explorer import GroupedExperimentExplorerDialog
from grouped_test_support import create_synthetic_database
from test_grouped_recording import stream_specs


class GroupedAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database, _ = create_synthetic_database(
            Path(self.temp.name) / "synthetic-v3.sqlite3", seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(self.database)
        self.repository = GroupedExperimentRepository(self.database)
        self.group = self.make_group()
        self.start_group(self.group)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_group(self, *, duration: float = 30.0):
        return self.repository.create_group(GroupedExperimentSpec(
            label="Synthetic paired capture",
            notes="Fictional test data only.",
            duration_target_seconds=duration,
            created_at="2026-03-01T12:00:00+00:00",
            record_requested_at="2026-03-01T12:00:00+00:00",
            clock_epoch_id="synthetic-common-clock",
            streams=stream_specs(),
        ))

    def start_group(self, group) -> None:
        self.repository.add_event(
            group.session_id,
            "recording_coordinator_started",
            observed_at="2026-03-01T12:00:00+00:00",
            elapsed_ns=0,
            details={"clock_epoch_id": group.clock_epoch_id},
        )
        for slot in ("A", "B"):
            self.repository.set_stream_status(
                group.session_id, slot, StreamStatus.RECORDING,
                observed_at="2026-03-01T12:00:00+00:00", elapsed_ns=0,
            )
        self.repository.set_group_status(
            group.session_id, GroupStatus.RECORDING,
            observed_at="2026-03-01T12:00:00+00:00", elapsed_ns=0,
        )

    def insert_frames(
        self,
        group,
        slot: str,
        indexes: list[int],
        times_ns: list[int],
        *,
        direct_intervals: list[int | None] | None = None,
    ) -> None:
        stream = next(item for item in group.streams if item.slot == slot)
        direct_intervals = direct_intervals or [
            None if index == 0 else times_ns[index] - times_ns[index - 1]
            for index in range(len(indexes))
        ]
        samples = []
        for offset, (index, timestamp) in enumerate(zip(indexes, times_ns, strict=True)):
            interval = direct_intervals[offset]
            samples.append(MeasurementSample(
                benchmark_id=stream.benchmark_id,
                metric="frame_arrival",
                value=1.0,
                unit="frame",
                stream_scope=StreamScope.CAPTURE,
                observed_at="2026-03-01T12:00:01+00:00",
                elapsed_seconds=timestamp / 1e9,
                sample_index=index,
                interval_ns=interval,
                method="synthetic_host_arrival_v1",
                elapsed_ns=timestamp,
                source_timestamp_ns=None,
                source_sequence=None,
                source_clock=None,
            ))
        for start in range(0, len(samples), 1024):
            self.repository.record_stream_measurements(
                group.session_id, slot, samples[start:start + 1024]
            )

    def finish_group(self, group, *, status: GroupStatus = GroupStatus.COMPLETED, end_ns: int = 100_000_000) -> None:
        for slot in ("A", "B"):
            self.repository.set_stream_status(
                group.session_id, slot, StreamStatus.COMPLETED,
                observed_at="2026-03-01T12:00:01+00:00", elapsed_ns=end_ns,
            )
        self.repository.finalize_group(
            group.session_id,
            status,
            ended_at="2026-03-01T12:00:01+00:00",
            duration_by_slot={"A": end_ns / 1e9, "B": end_ns / 1e9},
            termination_reason="synthetic_test_complete",
        )

    def test_zero_one_two_242_and_995_rows_are_counted_separately_from_last_index(self) -> None:
        empty = self.make_group()
        empty_result = load_grouped_analysis(self.database, empty.session_id)
        self.assertEqual([s.persisted_frame_samples for s in empty_result.streams], [0, 0])

        one = self.make_group()
        self.start_group(one)
        self.insert_frames(one, "A", [0], [10_000_000])
        one_result = load_grouped_analysis(self.database, one.session_id)
        self.assertEqual(one_result.stream("A").persisted_frame_samples, 1)
        self.assertEqual(one_result.stream("A").last_sequence_index, 0)
        self.assertEqual(one_result.stream("A").interval_stats.count, 0)
        self.assertIsNone(one_result.stream("A").observed_capture_fps)

        two = self.make_group()
        self.start_group(two)
        self.insert_frames(two, "A", [0, 1], [10_000_000, 20_000_000])
        two_result = load_grouped_analysis(self.database, two.session_id)
        self.assertEqual(two_result.stream("A").persisted_frame_samples, 2)
        self.assertEqual(two_result.stream("A").last_sequence_index, 1)
        self.assertEqual(two_result.stream("A").interval_stats.count, 1)
        self.assertAlmostEqual(two_result.stream("A").interval_stats.mean or 0, 10.0)
        self.assertIsNone(two_result.stream("A").observed_capture_fps)
        three = self.make_group()
        self.start_group(three)
        self.insert_frames(three, "A", [0, 1, 2], [10_000_000, 20_000_000, 30_000_000])
        three_result = load_grouped_analysis(self.database, three.session_id)
        self.assertAlmostEqual(three_result.stream("A").observed_capture_fps or 0, 100.0)

        large = self.make_group()
        self.start_group(large)
        self.insert_frames(large, "A", list(range(995)), [10_000_000 + i * 5_000_000 for i in range(995)])
        self.insert_frames(large, "B", list(range(242)), [20_000_000 + i * 40_000_000 for i in range(242)])
        result = load_grouped_analysis(self.database, large.session_id)
        self.assertEqual(result.stream("A").persisted_frame_samples, 995)
        self.assertEqual(result.stream("A").last_sequence_index, 994)
        self.assertEqual(result.stream("B").persisted_frame_samples, 242)
        self.assertEqual(result.stream("B").last_sequence_index, 241)
        self.assertEqual(result.stream("A").interval_stats.count, 994)
        self.assertEqual(result.stream("B").interval_stats.count, 241)

    def test_sequence_gaps_are_excluded_from_adjacent_interval_series_and_annotated(self) -> None:
        self.insert_frames(self.group, "A", [0, 1, 3, 4], [10_000_000, 20_000_000, 40_000_000, 50_000_000])
        result = load_grouped_analysis(self.database, self.group.session_id)
        stream = result.stream("A")
        self.assertEqual(stream.persisted_frame_samples, 4)
        self.assertEqual(stream.last_sequence_index, 4)
        self.assertEqual(stream.observed_sequence_gap_count, 1)
        self.assertEqual(stream.interval_stats.count, 2)
        self.assertEqual(stream.interval_stats.maximum, 10.0)
        self.assertFalse(stream.effective_data_complete)
        self.assertTrue(any("do not prove camera sensor frame drops" in note for note in stream.warnings))

    def test_zero_and_negative_intervals_are_reported_not_plotted_as_positive_samples(self) -> None:
        self.insert_frames(self.group, "A", [0, 1, 2], [10_000_000, 10_000_000, 20_000_000])
        stream_id = self.group.streams[0].benchmark_id
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE measurements SET elapsed_ns=5000000, elapsed_seconds=0.005 WHERE benchmark_id=? AND sample_index=2 AND metric='frame_arrival'",
                (stream_id,),
            )
        result = load_grouped_analysis(self.database, self.group.session_id)
        stream = result.stream("A")
        self.assertEqual(stream.zero_interval_count, 1)
        self.assertEqual(stream.negative_interval_count, 1)
        self.assertEqual(stream.interval_stats.count, 0)
        self.assertEqual(stream.interval_histogram.zero_sample_count, 1)
        self.assertTrue(any("negative elapsed interval" in note for note in stream.warnings))

    def test_shared_clock_alignment_uses_recorded_group_elapsed_and_offsets(self) -> None:
        self.insert_frames(self.group, "A", [0, 1, 2], [10_000_000, 20_000_000, 30_000_000])
        self.insert_frames(self.group, "B", [0, 1, 2], [60_000_000, 100_000_000, 140_000_000])
        result = load_grouped_analysis(self.database, self.group.session_id)
        self.assertTrue(result.shared_time_available)
        self.assertAlmostEqual(result.shared_start_offset_seconds or 0, 0.05)
        self.assertEqual(result.shared_time_reason, None)
        a = result.stream("A")
        b = result.stream("B")
        self.assertGreater(b.interval_points_shared[0][0], a.interval_points_shared[0][0])
        self.assertAlmostEqual(a.interval_points_stream[0][0], 0.01)
        self.assertAlmostEqual(b.interval_points_stream[0][0], 0.04)

    def test_shared_alignment_is_disabled_without_integer_monotonic_timestamps(self) -> None:
        stream = self.group.streams[0]
        self.insert_frames(self.group, "A", [0], [10_000_000])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE measurements SET elapsed_ns=NULL WHERE benchmark_id=? AND metric='frame_arrival'",
                (stream.benchmark_id,),
            )
        result = load_grouped_analysis(self.database, self.group.session_id)
        self.assertFalse(result.shared_time_available)
        self.assertIn("integer elapsed timestamps", result.shared_time_reason or "")
        self.assertFalse(result.stream("A").effective_data_complete)

    def test_per_stream_plot_uses_compatibility_seconds_when_nanoseconds_are_incomplete(self) -> None:
        stream = self.group.streams[0]
        self.insert_frames(self.group, "A", [0, 1, 2], [10_000_000, 20_000_000, 30_000_000])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE measurements SET elapsed_ns=NULL WHERE benchmark_id=? AND sample_index=0 AND metric='frame_arrival'",
                (stream.benchmark_id,),
            )
        result = load_grouped_analysis(self.database, self.group.session_id)
        self.assertFalse(result.shared_time_available)
        points = result.stream("A").interval_points_stream
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0][0], 0.01)
        self.assertAlmostEqual(points[1][0], 0.02)

    def test_persisted_failure_rows_are_distinct_from_arrival_rows(self) -> None:
        self.insert_frames(self.group, "A", [0, 1], [10_000_000, 20_000_000])
        stream = self.group.streams[0]
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO measurements
                   (id, benchmark_id, metric, value, unit, stream_scope, observed_at, elapsed_seconds, method, elapsed_ns)
                   VALUES ('synthetic-failure', ?, 'read_failure', 1, 'failure', 'capture',
                           '2026-03-01T12:00:01+00:00', 0.03, 'synthetic_read_failure', 30000000)""",
                (stream.benchmark_id,),
            )
        result = load_grouped_analysis(self.database, self.group.session_id).stream("A")
        self.assertEqual(result.persisted_frame_samples, 2)
        self.assertEqual(result.persisted_read_failure_samples, 1)
        self.assertEqual(result.persisted_timing_sample_count, 3)

    def test_queue_rejection_and_persistence_loss_are_visible_and_incomplete(self) -> None:
        self.repository.increment_stream_counters(
            self.group.session_id, "A", {"timing_queue_loss_count": 2},
            observed_at="2026-03-01T12:00:01+00:00", elapsed_ns=30_000_000,
        )
        self.insert_frames(self.group, "A", [0, 1], [10_000_000, 20_000_000])
        analysis = load_grouped_analysis(self.database, self.group.session_id)
        stream = analysis.stream("A")
        self.assertFalse(stream.effective_data_complete)
        self.assertIsNone(stream.observed_capture_fps)
        self.assertTrue(any("rejected before persistence" in warning for warning in stream.warnings))
        self.assertEqual(analysis.display_status, "Degraded · timing data loss")

    def test_partial_stream_failure_is_distinguished_from_empty_or_completed(self) -> None:
        self.insert_frames(self.group, "A", [0, 1], [10_000_000, 20_000_000])
        self.repository.set_stream_status(
            self.group.session_id, "B", StreamStatus.FAILED,
            observed_at="2026-03-01T12:00:01+00:00", elapsed_ns=40_000_000,
            reason_code="synthetic_camera_unavailable",
        )
        self.repository.set_stream_status(
            self.group.session_id, "A", StreamStatus.COMPLETED,
            observed_at="2026-03-01T12:00:01+00:00", elapsed_ns=80_000_000,
        )
        self.repository.finalize_group(
            self.group.session_id,
            GroupStatus.PARTIAL,
            ended_at="2026-03-01T12:00:01+00:00",
            duration_by_slot={"A": 0.08, "B": 0.04},
            termination_reason="synthetic_partial_test",
        )
        result = load_grouped_analysis(self.database, self.group.session_id)
        self.assertEqual(result.display_status, "Partial · incomplete data")
        self.assertEqual(result.stream("B").stream.stream_status, "failed")

    def test_abandoned_recording_lease_is_reported_without_clearing_it(self) -> None:
        ownership = self.database.acquire_recording_ownership(
            owner_kind="synthetic-test", check_recovery=False,
        )
        lease = self.repository.acquire_recording_lease(
            self.group.session_id,
            owner_pid=987654,
            owner_process_start_ticks="synthetic-owner",
            acquired_at="2026-03-01T12:00:00+00:00",
            owner_token=ownership.owner_token,
        )
        ownership.release(ownership.owner_token)
        before = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        result = load_grouped_analysis(self.database, self.group.session_id)
        after = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        self.assertEqual(result.display_status, "Indeterminate after crash")
        self.assertEqual(before, after)
        with self.database.connect_readonly() as connection:
            row = connection.execute(
                "SELECT owner_token FROM recording_leases WHERE lease_key=1"
            ).fetchone()
        self.assertEqual(row["owner_token"], lease.owner_token)

    def test_read_only_analysis_does_not_change_database_bytes_or_allow_updates(self) -> None:
        self.insert_frames(self.group, "A", [0, 1, 2], [10_000_000, 20_000_000, 30_000_000])
        before = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        result = load_grouped_analysis(self.database, self.group.session_id)
        self.assertEqual(result.group.session_id, self.group.session_id)
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(Exception):
                connection.execute("UPDATE group_recordings SET status='failed' WHERE session_id=?", (self.group.session_id,))
        after = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_read_group_snapshot_rejects_a_writable_connection(self) -> None:
        with self.database.connect() as connection:
            with self.assertRaisesRegex(Exception, "query-only"):
                self.repository.read_group_snapshot(connection, self.group.session_id)

    def test_incomplete_group_and_zero_sample_state_are_exposed(self) -> None:
        empty = self.make_group()
        result = load_grouped_analysis(self.database, empty.session_id)
        self.assertEqual(result.display_status, "Empty · not started")
        self.assertEqual(result.stream("A").persisted_frame_samples, 0)
        self.assertFalse(result.stream("A").interval_histogram.centers_ms)
        self.assertTrue(result.stream("A").warnings)

    def test_large_plot_series_are_bounded_and_preserve_interval_extrema(self) -> None:
        count = 12_000
        times = [10_000_000 + i * 10_000_000 for i in range(count)]
        times[6_100:] = [value + 50_990_000_000 for value in times[6_100:]]
        self.insert_frames(self.group, "A", list(range(count)), times)
        result = load_grouped_analysis(self.database, self.group.session_id, max_plot_points=64)
        stream = result.stream("A")
        self.assertEqual(stream.persisted_frame_samples, count)
        self.assertLessEqual(len(stream.interval_points_shared), 64)
        self.assertLessEqual(len(stream.fps_points_shared), 64)
        self.assertAlmostEqual(stream.interval_stats.maximum or 0, 51_000.0, delta=1.0)
        self.assertAlmostEqual(stream.interval_stats.mean or 0, (11_999 * 10 + 51_000) / 11_999, delta=0.1)


class GroupedExplorerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database, _ = create_synthetic_database(
            Path(self.temp.name) / "synthetic-ui-v3.sqlite3", seed_legacy_data=False
        )
        migrate_synthetic_database_to_v3(self.database)
        self.repository = GroupedExperimentRepository(self.database)
        self.group = self.repository.create_group(GroupedExperimentSpec(
            label="Synthetic 187 plus 24 FPS", duration_target_seconds=0.1,
            clock_epoch_id="synthetic-ui-clock", streams=stream_specs(),
        ))
        self.repository.add_event(
            self.group.session_id, "recording_coordinator_started",
            observed_at="2026-03-01T12:00:00+00:00", elapsed_ns=0,
            details={"clock_epoch_id": self.group.clock_epoch_id},
        )
        for slot in ("A", "B"):
            self.repository.set_stream_status(
                self.group.session_id, slot, StreamStatus.RECORDING,
                observed_at="2026-03-01T12:00:00+00:00", elapsed_ns=0,
            )
        self.repository.set_group_status(
            self.group.session_id, GroupStatus.RECORDING,
            observed_at="2026-03-01T12:00:00+00:00", elapsed_ns=0,
        )
        for stream, start, step, count in zip(self.group.streams, (10_000_000, 50_000_000), (5_000_000, 40_000_000), (200, 50)):
            samples = []
            for i in range(count):
                timestamp = start + i * step
                samples.append(MeasurementSample(
                    benchmark_id=stream.benchmark_id, metric="frame_arrival", value=1.0,
                    unit="frame", stream_scope=StreamScope.CAPTURE,
                    observed_at="2026-03-01T12:00:01+00:00",
                    elapsed_seconds=timestamp / 1e9, sample_index=i,
                    interval_ns=None if i == 0 else step,
                    method="synthetic_host_arrival_v1", elapsed_ns=timestamp,
                ))
            self.repository.record_stream_measurements(self.group.session_id, stream.slot, samples)
        self.model = load_grouped_analysis(self.database, self.group.session_id)
        self.window = GroupedExperimentExplorerDialog(self.model)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        if self.window is not None:
            self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_grouped_history_and_analysis_show_two_streams_and_row_counts(self) -> None:
        self.assertEqual(self.window.tabs.count(), 2)
        self.assertIn("Synthetic 187 plus 24 FPS", self.window.windowTitle())
        self.assertEqual(self.window.events_table.columnCount(), 4)
        self.assertEqual(self.window.stats_table.item(0, 1).text(), "Synthetic Eye")
        self.assertEqual(self.window.stats_table.item(0, 2).text(), "Synthetic Wide Camera")
        self.assertEqual(self.model.stream("A").persisted_frame_samples, 200)
        self.assertEqual(self.model.stream("B").persisted_frame_samples, 50)
        self.assertEqual(self.model.stream("A").last_sequence_index, 199)
        self.assertEqual(self.model.stream("A").requested_capture_mode, "320×240 GRBG · 187 FPS")
        self.assertAlmostEqual(self.model.stream("A").observed_capture_fps or 0, 200.0)
        self.assertEqual(self.model.stream("A").interval_histogram.positive_sample_count, 199)
        self.assertAlmostEqual(sum(self.model.stream("A").interval_histogram.fractions), 1.0)
        self.assertEqual(len(self.window.histogram_panel.plot.plotItem.listDataItems()), 2)
        self.assertTrue(self.window.time_alignment.isEnabled())
        self.window.open_analysis_button.click()
        self.app.processEvents()
        self.assertEqual(self.window.tabs.currentWidget(), self.window.analysis_page)

    def test_rate_display_switching_keeps_curves_visible_and_uses_zero_baselines(self) -> None:
        self.assertEqual(len(self.window.rate_panel.plot.plotItem.listDataItems()), 2)
        self.assertEqual(len(self.window.rate_panel.plot.plotItem.legend.items), 2)
        self.window.rate_mode.setCurrentIndex(1)
        self.app.processEvents()
        curves = self.window.rate_panel.plot.plotItem.listDataItems()
        self.assertEqual(len(curves), 2)
        legend = self.window.rate_panel.plot.plotItem.legend
        self.assertEqual(len(legend.items), 2)
        self.assertEqual(len({label.text for _sample, label in legend.items}), 2)
        for curve in curves:
            _x, y = curve.getData()
            self.assertTrue(len(y))
            self.assertLess(max(abs(float(value)) for value in y), 0.01)
        self.assertEqual(self.window.rate_panel.plot.plotItem.getAxis("left").suffix, "%")
        self.assertEqual(self.window.rate_panel.plot.plotItem.getAxis("left").labelText, "Deviation from own mean (%)")
        self.window.rate_mode.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(len(self.window.rate_panel.plot.plotItem.listDataItems()), 2)

    def test_empty_series_have_explicit_state_and_history_never_mutates_rows(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM measurements WHERE benchmark_id IN (?,?)", tuple(s.benchmark_id for s in self.group.streams))
        # Analysis view is a snapshot; reloading it reads the now-empty synthetic fixture.
        before = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        empty_model = load_grouped_analysis(self.database, self.group.session_id)
        empty_window = GroupedExperimentExplorerDialog(empty_model)
        empty_window.show()
        self.app.processEvents()
        self.assertEqual(empty_window.rate_panel.stack.currentWidget(), empty_window.rate_panel.empty_label)
        self.assertIn("No capture-rate windows", empty_window.rate_panel.empty_label.text())
        empty_window.close()
        after = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)  # Opening and reading the viewer made no further database changes.

    def test_navigation_expand_restore_and_responsive_sizes(self) -> None:
        for width, expected in ((1024, Qt.Orientation.Vertical), (1366, Qt.Orientation.Horizontal), (1920, Qt.Orientation.Horizontal)):
            self.window.resize(width, 768)
            self.app.processEvents()
            self.assertEqual(self.window.interval_splitter.orientation(), expected)
        controls = self.window.rate_panel.controls
        controls.expand_button.click()
        self.app.processEvents()
        self.assertTrue(controls.expand_button.isChecked())
        self.assertIn("group-rate", self.window._expanded)
        focused = self.window._expanded["group-rate"]
        focused.close()
        self.app.processEvents()
        self.assertFalse(controls.expand_button.isChecked())
        self.assertNotIn("group-rate", self.window._expanded)
        self.assertTrue(self.window.rate_panel.reset_view())
        self.assertTrue(self.window.rate_panel.fit_to_data())

    def test_default_capture_rate_x_range_uses_the_full_host_recording_duration(self) -> None:
        for stream in self.group.streams:
            self.repository.add_event(
                self.group.session_id,
                "stream_completed",
                benchmark_id=stream.benchmark_id,
                observed_at="2026-03-01T12:00:30+00:00",
                elapsed_ns=30_000_000_000,
                reason_code="synthetic_duration_elapsed",
            )
        model = load_grouped_analysis(self.database, self.group.session_id)
        dialog = GroupedExperimentExplorerDialog(model)
        self.assertEqual(dialog.rate_panel._x_range, (0.0, 30.0))
        self.assertEqual(model.stream("A").recording_start_elapsed_ns, 0)
        self.assertEqual(model.stream("A").recording_end_elapsed_ns, 30_000_000_000)
        self.assertEqual(model.stream("A").recording_duration_seconds, 30.0)
        dialog.close()

    def test_dark_and_light_palette_labels_remain_theme_aware(self) -> None:
        for window_color, base_color, text_color in (
            ("#20242a", "#191d22", "#f2f3f5"),
            ("#ffffff", "#ffffff", "#202124"),
        ):
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(window_color))
            palette.setColor(QPalette.ColorRole.Base, QColor(base_color))
            palette.setColor(QPalette.ColorRole.Text, QColor(text_color))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(text_color))
            self.window.setPalette(palette)
            self.window._apply_theme()
            self.app.processEvents()
            self.assertIn("palette(text)", self.window.status_label.styleSheet())
            axis_color = self.window.rate_panel.plot.getAxis("left").textPen().color()
            self.assertEqual(axis_color.name().lower(), text_color.lower())
            title_color = QColor(self.window.rate_panel.plot.plotItem.titleLabel.opts["color"])
            label_color = self.window.rate_panel.plot.getAxis("left").label.defaultTextColor()
            self.assertEqual(title_color.name().lower(), text_color.lower())
            self.assertEqual(label_color.name().lower(), text_color.lower())
            legend = self.window.rate_panel.plot.plotItem.legend
            self.assertIsNotNone(legend)
            self.assertTrue(all(label.opts["color"].lower() == text_color.lower() for _sample, label in legend.items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
