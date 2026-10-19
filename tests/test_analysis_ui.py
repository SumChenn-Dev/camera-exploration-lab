from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from camera_exploration_lab.analysis import (
    MetricStats,
    load_analysis as real_load_analysis,
)
from camera_exploration_lab.db import Database
from camera_exploration_lab.models import BackendKind, BenchmarkRecord, CameraIdentity, MeasurementSample, SessionRecord, StreamScope
from camera_exploration_lab.ui.analysis_workspace import (
    AnalysisWorkspace,
    _MetricCard,
    _RateAxisItem,
    contrast_ratio,
)


class AnalysisWorkspaceResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._workspaces: list[AnalysisWorkspace] = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "large-analysis.sqlite3")
        self.database.initialize()
        session = SessionRecord(camera_id=None, label="Large synthetic analysis", source="live_experiment")
        self.database.create_session(session)
        self.benchmark = BenchmarkRecord(
            session_id=session.id,
            label="Synthetic high-rate capture",
            status="interrupted",
            configuration={"requested": {"width": 320, "height": 240, "fps": 187}},
        )
        self.database.create_benchmark(self.benchmark)
        count = 12_000
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=self.benchmark.id,
                metric="frame_interval_ms",
                value=5.333 if index != 6500 else 55.0,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                elapsed_seconds=index * 0.005333,
                sample_index=index + 1,
                interval_ns=5_333_000 if index != 6500 else 55_000_000,
                method="synthetic monotonic arrival intervals",
            )
            for index in range(count)
        ])

    def tearDown(self) -> None:
        for workspace in self._workspaces:
            workspace.stop_background_work()
            workspace.close()
        self.application.processEvents()
        self.database.close()
        self.tempdir.cleanup()

    def _track(self, workspace: AnalysisWorkspace) -> AnalysisWorkspace:
        self._workspaces.append(workspace)
        return workspace

    def _process_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        self.application.processEvents()
        return predicate()

    def test_large_analysis_runs_off_ui_thread_and_keeps_event_loop_responsive(self) -> None:
        worker_threads: list[int] = []

        def delayed_analysis(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            time.sleep(0.15)
            return real_load_analysis(*args, **kwargs)

        with patch(
            "camera_exploration_lab.ui.analysis_workspace.load_analysis",
            side_effect=delayed_analysis,
        ):
            workspace = self._track(AnalysisWorkspace(self.database))
            workspace.show()
            self.assertTrue(self._process_until(
                lambda: workspace.status_label.text().startswith("Loading stored timings")
            ))
            timer_fired: list[bool] = []
            QTimer.singleShot(5, lambda: timer_fired.append(True))
            self.assertTrue(self._process_until(lambda: bool(timer_fired), timeout=0.1))
            self.assertIsNone(workspace._primary)
            self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))

        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        self.assertLessEqual(len(workspace._primary.interval_points), 2400)
        self.assertLessEqual(len(workspace._primary.fps_points), 2400)
        plotted_intervals = workspace.interval_plot.plotItem.listDataItems()[0].getData()[1]
        self.assertIn(55.0, plotted_intervals)
        workspace.close()

    def test_two_experiments_populate_comparison_metrics_and_distributions(self) -> None:
        second_session = SessionRecord(
            camera_id=None,
            label="Second synthetic mode",
            source="live_experiment",
        )
        self.database.create_session(second_session)
        second_run = BenchmarkRecord(
            session_id=second_session.id,
            label="640x480 MJPEG",
            status="completed",
            configuration={
                "requested": {"width": 640, "height": 480, "pixel_format": "MJPG", "fps": 60},
                "driver_reported": {"width": 640, "height": 480, "pixel_format": "MJPG", "fps": 60},
            },
        )
        self.database.create_benchmark(second_run)
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=second_run.id,
                metric="frame_interval_ms",
                value=40.0 + index,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                elapsed_seconds=(index + 1) * 0.04,
                sample_index=index + 1,
                interval_ns=(40 + index) * 1_000_000,
                method="synthetic monotonic arrival intervals",
            )
            for index in range(4)
        ])

        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        primary_data = workspace.experiment_combo.currentData()
        other_index = next(
            index for index in range(1, workspace.compare_combo.count())
            if workspace.compare_combo.itemData(index) != primary_data
        )
        workspace.compare_combo.setCurrentIndex(other_index)
        expected_comparison = workspace.compare_combo.currentData()
        self.assertTrue(self._process_until(
            lambda: workspace._comparison is not None
            and workspace._comparison.option.session_id == expected_comparison[0],
            timeout=5.0,
        ))
        self.assertNotEqual(workspace.compare_combo.currentData(), primary_data)

        self.assertGreater(workspace.comparison_table.rowCount(), 10)
        self.assertEqual(len(workspace.distribution_plot.plotItem.listDataItems()), 2)
        self.assertEqual(len(workspace.fps_plot.plotItem.listDataItems()), 2)
        self.assertTrue(workspace.fps_deviation_checkbox.isEnabled())
        self.assertTrue(workspace.interval_overlay_checkbox.isEnabled())
        workspace.interval_overlay_checkbox.setChecked(True)
        self.application.processEvents()
        interval_items = workspace.interval_plot.plotItem.listDataItems()
        self.assertEqual(len(interval_items), 2)
        self.assertTrue(all("Session " in item.name() for item in interval_items))
        workspace.interval_primary_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(len(workspace.interval_plot.plotItem.listDataItems()), 1)
        workspace.interval_primary_checkbox.setChecked(True)
        self.application.processEvents()
        self.assertIn("Separate recordings", workspace.comparison_notice.text())
        self.assertTrue(all(item.toolTip() for item in workspace.fps_plot.plotItem.listDataItems()))
        counts_before = [len(item.getData()[0]) for item in workspace.fps_plot.plotItem.listDataItems()]
        workspace.fps_deviation_checkbox.setChecked(True)
        self.application.processEvents()
        self.assertEqual(workspace.fps_plot.plotItem.getAxis("left").labelText, "Deviation from session mean (%)")
        axis = workspace.fps_plot.plotItem.getAxis("left")
        self.assertIn("%", axis.tickStrings([-12.5, 0.0, 4.25], 1.0, 1.0)[0])
        self.assertEqual(
            [len(item.getData()[0]) for item in workspace.fps_plot.plotItem.listDataItems()],
            counts_before,
        )
        self.assertIn("Capture FPS", workspace.metric_cards["capture_fps"].title.text())
        self.assertIn("Median Interval", workspace.metric_cards["median"].title.text())
        labels = [
            workspace.comparison_table.item(row, 0).text()
            for row in range(workspace.comparison_table.rowCount())
        ]
        self.assertIn("Capture FPS mean", labels)
        self.assertIn("Capture read failures", labels)
        self.assertEqual(
            [workspace.comparison_table.horizontalHeaderItem(i).text() for i in range(4)],
            ["Metric", "Session A", "Session B", "B − A"],
        )

        primary_identity = workspace.session_a_combo.currentData()
        comparison_identity = workspace.session_b_combo.currentData()
        workspace.swap_button.click()
        self.assertTrue(self._process_until(
            lambda: workspace._primary is not None
            and workspace._primary.option.session_id == comparison_identity[0]
            and workspace._comparison is not None
            and workspace._comparison.option.session_id == primary_identity[0]
        ))
        workspace.clear_comparison_button.click()
        self.assertTrue(self._process_until(lambda: workspace._comparison is None))
        self.assertIsNone(workspace.session_b_combo.currentData())

        for width, height in ((1366, 768), (1920, 1080)):
            workspace.resize(width, height)
            self.application.processEvents()
            scroll = workspace.findChild(QScrollArea)
            self.assertIsNotNone(scroll)
            self.assertGreater(scroll.viewport().width(), 0)
            self.assertGreater(scroll.viewport().height(), 0)
            self.assertEqual(scroll.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.assertEqual(workspace._plot_splitter.orientation(), Qt.Orientation.Horizontal)
        workspace.resize(1024, 768)
        self.application.processEvents()
        self.assertEqual(workspace._plot_splitter.orientation(), Qt.Orientation.Vertical)
        workspace.close()

    def test_deviation_math_ranges_and_series_visibility_survive_redraws(self) -> None:
        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        base = workspace._primary
        run_a = replace(
            base,
            option=replace(base.option, label="PS3 Eye 320x240 GRBG 187 FPS"),
            duration_seconds=30.0,
            fps_x_label="Recorder elapsed time (s)",
            fps_points=((0.5, 187.35), (15.0, 187.50), (29.5, 187.20)),
            capture_fps_stats=MetricStats(count=3, mean=187.35),
        )
        run_b = replace(
            base,
            option=replace(base.option, label="PS3 Eye 640x480 GRBG 60 FPS"),
            duration_seconds=30.0,
            fps_x_label="Recorder elapsed time (s)",
            fps_points=((0.5, 60.0), (15.0, 60.6), (29.5, 59.4)),
            capture_fps_stats=MetricStats(count=3, mean=60.0),
        )
        workspace._render(run_a, run_b)
        plot = workspace.fps_plot
        self.assertEqual(len(plot.plotItem.listDataItems()), 2)
        x_range = plot.getViewBox().viewRange()[0]
        self.assertAlmostEqual(x_range[0], 0.0)
        self.assertAlmostEqual(x_range[1], 30.0)
        self.assertIs(plot.plotItem.legend.parentItem(), plot.plotItem.vb)

        workspace.fps_deviation_checkbox.setChecked(True)
        self.application.processEvents()
        items = plot.plotItem.listDataItems()
        self.assertEqual(len(items), 2)
        by_name = {item.name(): tuple(item.getData()[1]) for item in items}
        values_a = next(values for name, values in by_name.items() if "Session A" in name)
        values_b = next(values for name, values in by_name.items() if "Session B" in name)
        self.assertAlmostEqual(values_a[0], 0.0)
        self.assertAlmostEqual(values_a[1], 100.0 * (187.50 - 187.35) / 187.35)
        self.assertAlmostEqual(values_a[2], 100.0 * (187.20 - 187.35) / 187.35)
        self.assertAlmostEqual(values_b[0], 0.0)
        self.assertAlmostEqual(values_b[1], 1.0)
        self.assertAlmostEqual(values_b[2], -1.0)
        y_range = plot.getViewBox().viewRange()[1]
        self.assertLess(y_range[0], 0.0)
        self.assertGreater(y_range[1], 0.0)
        self.assertEqual(plot.plotItem.getAxis("left").labelText, "Deviation from session mean (%)")
        self.assertTrue(all("%" in value for value in plot.plotItem.getAxis("left").tickStrings([-1.0, 0.0, 0.08, 1.0], 1.0, 0.1)))

        # Simulate stale absolute-FPS and time limits before changing display mode.
        plot.getViewBox().setRange(xRange=(50.0, 200.0), yRange=(180.0, 192.0), padding=0.0)
        workspace.fps_deviation_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(len(plot.plotItem.listDataItems()), 2)
        self.assertAlmostEqual(plot.getViewBox().viewRange()[0][0], 0.0)
        self.assertAlmostEqual(plot.getViewBox().viewRange()[0][1], 30.0)
        absolute_y = plot.getViewBox().viewRange()[1]
        self.assertLess(absolute_y[0], 60.0)
        self.assertGreater(absolute_y[1], 187.0)
        absolute_values = [tuple(item.getData()[1]) for item in plot.plotItem.listDataItems()]
        self.assertIn((187.35, 187.5, 187.2), absolute_values)
        self.assertIn((60.0, 60.6, 59.4), absolute_values)

        # A visibility change in one plot must not disturb manual navigation in the other.
        interval_view = workspace.interval_plot.getViewBox()
        interval_view.setRange(xRange=(1.0, 2.0), yRange=(4.0, 8.0), padding=0.0)
        workspace.fps_deviation_checkbox.setChecked(True)
        workspace.fps_primary_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(interval_view.viewRange()[0], [1.0, 2.0])
        fps_view = workspace.fps_plot.getViewBox()
        fps_view.setRange(xRange=(5.0, 10.0), yRange=(-2.0, 2.0), padding=0.0)
        workspace.interval_primary_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(fps_view.viewRange()[0], [5.0, 10.0])
        workspace.interval_primary_checkbox.setChecked(True)
        workspace.fps_primary_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(len(plot.plotItem.listDataItems()), 1)
        self.assertIn("Session B", plot.plotItem.listDataItems()[0].name())
        workspace.fps_comparison_checkbox.setChecked(False)
        self.application.processEvents()
        self.assertEqual(plot.plotItem.listDataItems(), [])
        self.assertIn("No capture-rate trace is enabled", workspace.fps_note.text())
        workspace.fps_comparison_checkbox.setChecked(True)
        workspace.fps_primary_checkbox.setChecked(True)
        self.application.processEvents()
        self.assertEqual(len(plot.plotItem.listDataItems()), 2)
        self.assertIs(plot.plotItem.legend.parentItem(), plot.plotItem.vb)

        summary_only = replace(
            run_a,
            option=replace(run_a.option, source="imported", status="interrupted"),
            duration_seconds=0.0,
            fps_points=(),
            capture_fps_stats=MetricStats(count=0, mean=None),
            interval_points=(),
            interval_data_available=False,
        )
        workspace._render(summary_only, None)
        self.assertEqual(plot.plotItem.listDataItems(), [])
        self.assertIn("No valid capture-FPS time series", workspace.fps_note.text())
        self.assertFalse(workspace.fps_controls.fit_button.isEnabled())
        workspace.close()

    def test_navigation_controls_fit_reset_zoom_expand_and_restore(self) -> None:
        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        initial_sizes = workspace._plot_splitter.sizes()
        workspace.fps_controls.expand_button.click()
        self.application.processEvents()
        self.assertEqual(workspace.fps_controls.expand_button.text(), "Restore Layout")
        self.assertEqual(workspace._plot_splitter.sizes()[1], 0)
        workspace.fps_controls.expand_button.click()
        self.application.processEvents()
        self.assertEqual(workspace.fps_controls.expand_button.text(), "Expand Plot")
        self.assertGreater(workspace._plot_splitter.sizes()[1], 0)

        view = workspace.fps_plot.getViewBox()
        view.setRange(xRange=(50.0, 200.0), yRange=(180.0, 192.0), padding=0.0)
        workspace.fps_controls.fit_button.click()
        self.application.processEvents()
        fitted = view.viewRange()
        self.assertLess(fitted[0][0], 0.0)
        self.assertGreater(fitted[0][1], 60.0)
        before_zoom = fitted[0][1] - fitted[0][0]
        workspace.fps_controls.zoom_in_button.click()
        zoomed = view.viewRange()
        self.assertLess(zoomed[0][1] - zoomed[0][0], before_zoom)
        workspace.fps_controls.zoom_out_button.click()
        zoomed_out = view.viewRange()
        self.assertGreater(zoomed_out[0][1] - zoomed_out[0][0], zoomed[0][1] - zoomed[0][0])
        workspace.fps_controls.reset_button.click()
        self.application.processEvents()
        reset = view.viewRange()
        self.assertAlmostEqual(reset[0][0], 0.0)
        self.assertAlmostEqual(reset[0][1], workspace._primary.duration_seconds)
        self.assertEqual(view.state["mouseMode"], workspace.fps_plot.plotItem.vb.PanMode)
        self.assertGreater(initial_sizes[0], 0)
        workspace.close()

    def test_metadata_notes_graph_labels_and_disabled_controls_have_theme_contrast(self) -> None:
        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#22262d"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#c4c8ce"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#c4c8ce"))
        light = QPalette()
        light.setColor(QPalette.ColorRole.Window, QColor("#f3f4f6"))
        light.setColor(QPalette.ColorRole.WindowText, QColor("#25282c"))
        light.setColor(QPalette.ColorRole.Text, QColor("#25282c"))
        for palette in (dark, light):
            workspace.setPalette(palette)
            workspace._update_theme_contrast()
            labels = [
                label for label in workspace.findChildren(QLabel)
                if label.property("celThemeText")
            ]
            self.assertGreaterEqual(len(labels), 15)
            for label in labels:
                foreground = QColor(label.property("celTextForeground"))
                background = QColor(label.property("celTextBackground"))
                self.assertGreaterEqual(contrast_ratio(foreground, background), 4.5, label.text())
            theme_foreground = QColor(labels[0].property("celTextForeground")).name()
            for checkbox in (
                workspace.fps_deviation_checkbox,
                workspace.fps_primary_checkbox,
                workspace.fps_comparison_checkbox,
                workspace.interval_primary_checkbox,
                workspace.interval_overlay_checkbox,
            ):
                foreground = QColor(checkbox.property("celDisabledForeground"))
                background = QColor(checkbox.property("celTextBackground"))
                self.assertGreaterEqual(contrast_ratio(foreground, background), 4.5)
            for plot in (workspace.fps_plot, workspace.interval_plot, workspace.distribution_plot):
                self.assertEqual(plot.plotItem.titleLabel.opts["color"], theme_foreground)
                for axis_name in ("left", "bottom"):
                    axis = plot.plotItem.getAxis(axis_name)
                    pen = axis.textPen()
                    surface = palette.color(QPalette.ColorRole.Window)
                    self.assertGreaterEqual(contrast_ratio(pen.color(), surface), 4.5)
                    self.assertIn(pen.color().name(), axis.label.toHtml().lower())
            legend = workspace.fps_plot.plotItem.legend
            if legend is not None:
                for _sample, label in legend.items:
                    html = label.item.toHtml().lower()
                    self.assertIn(f"color:{theme_foreground}", html)
        workspace.close()

    def test_filtering_and_empty_comparison_state(self) -> None:
        second_session = SessionRecord(camera_id=None, label="Second camera mode", source="live_experiment")
        self.database.create_session(second_session)
        second_run = BenchmarkRecord(
            session_id=second_session.id, label="640x480 MJPEG", status="completed",
            configuration={"requested": {"width": 640, "height": 480, "pixel_format": "MJPG", "fps": 60}},
        )
        self.database.create_benchmark(second_run)
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=second_run.id, metric="frame_interval_ms", value=40.0,
                unit="ms", stream_scope=StreamScope.CAPTURE, elapsed_seconds=0.04,
                sample_index=1, interval_ns=40_000_000, method="synthetic second mode",
            ),
        ])
        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        self.assertFalse(workspace.empty_state.isVisible())
        self.assertEqual(workspace.session_b_combo.count(), 2)
        workspace.camera_filter.setCurrentIndex(1)
        self.assertEqual(workspace.session_a_combo.count(), 2)
        mode_index = next(
            index for index in range(1, workspace.mode_filter.count())
            if "640×480" in workspace.mode_filter.itemText(index)
        )
        workspace.mode_filter.setCurrentIndex(mode_index)
        self.assertEqual(workspace.session_a_combo.count(), 1)
        fps_index = next(
            index for index in range(1, workspace.fps_filter.count())
            if workspace.fps_filter.itemData(index) == 60.0
        )
        workspace.fps_filter.setCurrentIndex(fps_index)
        self.application.processEvents()
        self.assertEqual(workspace.session_a_combo.count(), 1)
        self.assertIn("640×480", workspace.session_a_combo.currentText())
        workspace.search_edit.setText("no matching synthetic name")
        self.application.processEvents()
        self.assertEqual(workspace.session_a_combo.count(), 0)
        self.assertIn("No experiments match", workspace.empty_state.text())
        workspace.metric_cards["p99"].help_button.click()
        self.application.processEvents()
        guide = workspace._metrics_dialog
        self.assertIsNotNone(guide)
        self.assertEqual(guide.topic_list.currentItem().data(Qt.ItemDataRole.UserRole), "percentiles")
        guide.close()
        workspace.close()

    def test_camera_filter_groups_legacy_and_discovered_identity_without_numbered_units(self) -> None:
        identities = (
            CameraIdentity(
                "legacy:synthetic-eye", BackendKind.V4L2, manufacturer="Sony",
                model="PlayStation Eye", vendor_id="1415", product_id="2000",
                identity_source="inferred_from_legacy_path",
            ),
            CameraIdentity(
                "/dev/v4l/by-id/synthetic-eye", BackendKind.V4L2, manufacturer="Sony",
                model="PlayStation Eye", vendor_id="1415", product_id="2000",
                identity_source="v4l_by_id",
            ),
        )
        for index, camera in enumerate(identities):
            camera_id = self.database.upsert_camera(camera)
            session = SessionRecord(camera_id=camera_id, label=f"Synthetic Eye {index}", source="imported" if index == 0 else "live_experiment")
            self.database.create_session(session)
            benchmark = BenchmarkRecord(
                session_id=session.id, label=f"Mode {index}", status="completed",
                configuration={"requested": {"width": 320, "height": 240, "pixel_format": "GRBG", "fps": 60 + index}},
            )
            self.database.create_benchmark(benchmark)
        workspace = self._track(AnalysisWorkspace(self.database))
        workspace.show()
        self.assertTrue(self._process_until(lambda: len(workspace._options) == 3))
        eye_index = workspace.camera_filter.findData("usb-family:1415:2000")
        self.assertGreater(eye_index, 0)
        self.assertIn("2 recorded identity groups", workspace.camera_filter.itemText(eye_index))
        self.assertNotIn("unit 1", workspace.camera_filter.itemText(eye_index).lower())
        workspace.camera_filter.setCurrentIndex(eye_index)
        self.assertEqual(workspace.session_a_combo.count(), 2)
        selector_labels = " ".join(
            workspace.session_a_combo.itemText(index) for index in range(2)
        )
        self.assertIn("Legacy CSV inferred", selector_labels)
        self.assertIn("V4L2 by-id", selector_labels)

    def test_metric_cards_meet_contrast_target_in_dark_and_light_palettes(self) -> None:
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Button, QColor("#363a40"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#c4c8ce"))
        light = QPalette()
        light.setColor(QPalette.ColorRole.Button, QColor("#f1f2f4"))
        light.setColor(QPalette.ColorRole.Text, QColor("#25282c"))
        for palette in (dark, light):
            for title in (
                "Capture FPS", "Preview FPS", "Processing FPS", "Frame Count",
                "Median Interval", "p95 Interval", "p99 Interval", "Read Failures",
            ):
                card = _MetricCard(title, "Synthetic accessibility test", "frame_count")
                card.setPalette(palette)
                card._update_contrast()
                self.assertGreaterEqual(
                    contrast_ratio(card.foreground_color, card.surface_color), 4.5, title
                )
                if card.surface_color.lightness() < 100:
                    self.assertGreater(card.foreground_color.lightness(), 180, title)
                card.close()

    def test_deviation_axis_uses_readable_percent_units(self) -> None:
        axis = _RateAxisItem(orientation="left")
        axis.percent_mode = True
        labels = axis.tickStrings([0.0, -12.5, 0.5, 125.0], 1.0, 1.0)
        self.assertEqual(labels, ["0%", "-12.5%", "0.50%", "125%"])
        self.assertTrue(all("e" not in label.lower() for label in labels))
        self.assertFalse(axis.autoSIPrefix)


if __name__ == "__main__":
    unittest.main(verbosity=2)
