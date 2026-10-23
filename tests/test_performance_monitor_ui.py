from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.capture import OpenCVCaptureWorker
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.db import Database
from camera_exploration_lab.performance import ClockDomain, Metric, TelemetrySample, reading
from camera_exploration_lab.ui.live_workbench import LiveWorkbenchWindow
from camera_exploration_lab.ui.performance_monitor import LiveCaptureSource, PerformanceMonitorWorkspace

APP = QApplication.instance() or QApplication([])


class SyntheticPerformanceMonitorUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace: PerformanceMonitorWorkspace | None = None

    def tearDown(self) -> None:
        if self.workspace is not None:
            self.workspace.close()
            APP.processEvents()

    def test_default_view_is_historical_replay_with_six_datasets_and_no_camera_connection(self) -> None:
        self.workspace = PerformanceMonitorWorkspace()
        self.workspace.resize(1024, 768)
        self.workspace.show()
        APP.processEvents()

        self.assertEqual(self.workspace.mode_combo.currentData(), "replay")
        self.assertEqual(self.workspace.dataset_combo.count(), 6)
        self.assertIn("HISTORICAL REPLAY", self.workspace.banner.text())
        self.assertIn("no camera connection", self.workspace.banner.text().lower())
        self.assertGreaterEqual(self.workspace.rate_plot.plotItem.listDataItems().__len__(), 2)
        self.assertIn("benchmark row", self.workspace.metric_values["state"].text())

    def test_dataset_selection_pause_resume_and_reset_controls(self) -> None:
        self.workspace = PerformanceMonitorWorkspace()
        self.workspace.dataset_combo.setCurrentIndex(5)
        APP.processEvents()
        self.assertEqual(len(self.workspace.rate_plot.plotItem.listDataItems()), 3)
        self.workspace.play_button.click()
        self.assertTrue(self.workspace._controller.playing)
        self.workspace.pause_button.click()
        self.assertFalse(self.workspace._controller.playing)
        self.workspace.reset_button.click()
        self.assertEqual(self.workspace._controller.index, 0)
        self.assertEqual(self.workspace.play_button.text(), "Play")

    def test_live_mode_reads_existing_worker_ring_without_starting_a_capture(self) -> None:
        calls: list[str] = []

        def forbidden_capture(device: str):
            calls.append(device)
            raise AssertionError("Performance Monitor must not open a camera")

        worker = OpenCVCaptureWorker(
            "synthetic-camera-only",
            CaptureMode("synthetic", 320, 240, 100),
            capture_factory=forbidden_capture,
        )
        tick = 10_000_000_000
        worker.telemetry_buffer.append(TelemetrySample(
            timestamp_ns=tick,
            source="fake OpenCV capture callback",
            clock_domain=ClockDomain.HOST_MONOTONIC,
            sequence=0,
            readings=(
                reading(Metric.OPENCV_CAPTURE_FPS, 99.0,
                        provenance="Synthetic successful host read-return events."),
                reading(Metric.OPENCV_READ_CALL_DURATION_MS, 4.0,
                        provenance="Synthetic OpenCV read() call duration."),
                reading(Metric.FRAME_INTERVAL_MS, 10.1,
                        provenance="Synthetic read-return interval."),
            ),
        ))
        sources = [LiveCaptureSource("fake-a", "Synthetic A", worker, 30.0)]
        self.workspace = PerformanceMonitorWorkspace(source_provider=lambda: tuple(sources))
        self.workspace.mode_combo.setCurrentIndex(1)
        APP.processEvents()
        self.assertTrue(self.workspace.replay_position.isHidden())
        self.workspace._poll_live()
        APP.processEvents()

        self.assertEqual(self.workspace.mode_combo.currentData(), "live")
        self.assertIn("no competing camera connection", self.workspace.banner.text().lower())
        self.assertIn("GUI updates/s", self.workspace.metric_values["preview"].text())
        self.assertEqual(len(self.workspace._live_times), 1)
        self.assertEqual(self.workspace._live_read_durations[-1], 4.0)
        self.assertEqual(calls, [])
        self.workspace.mode_combo.setCurrentIndex(0)
        APP.processEvents()
        self.assertFalse(self.workspace.replay_position.isHidden())

    def test_live_plot_histories_remain_bounded_during_long_sessions(self) -> None:
        self.workspace = PerformanceMonitorWorkspace(source_provider=lambda: ())
        capture_series = (
            self.workspace._live_times,
            self.workspace._live_capture_rates,
            self.workspace._live_intervals,
            self.workspace._live_read_durations,
        )
        for series in capture_series:
            self.assertEqual(series.maxlen, 4096)
            for value in range(series.maxlen + 100):
                series.append(float(value))
            self.assertEqual(len(series), 4096)
            self.assertEqual(series[0], 100.0)
            self.assertEqual(series[-1], 4195.0)

        for series in (self.workspace._preview_times, self.workspace._preview_rates):
            self.assertEqual(series.maxlen, 600)
            for value in range(700):
                series.append(float(value))
            self.assertEqual(len(series), 600)
            self.assertEqual(series[0], 100.0)
            self.assertEqual(series[-1], 699.0)

    def test_monitor_layout_can_resize_across_desktop_viewports(self) -> None:
        self.workspace = PerformanceMonitorWorkspace()
        self.workspace.show()
        for width, height in ((1024, 768), (1366, 768), (1920, 1080)):
            self.workspace.resize(width, height)
            APP.processEvents()
            self.assertEqual(self.workspace.width(), width)
            self.assertTrue(self.workspace.banner.isVisible())
            if width == 1024:
                scrollbar = self.workspace.chart_scroll.verticalScrollBar()
                self.assertGreater(scrollbar.maximum(), 0)
                scrollbar.setValue(scrollbar.maximum())
                APP.processEvents()
                self.assertEqual(scrollbar.value(), scrollbar.maximum())


    def test_all_historical_datasets_render_available_activity_and_content_metrics(self) -> None:
        self.workspace = PerformanceMonitorWorkspace()
        self.workspace.show()
        legends = []
        for index in range(6):
            self.workspace.dataset_combo.setCurrentIndex(index)
            APP.processEvents()
            self.assertGreater(len(self.workspace.activity_plot.plotItem.listDataItems()), 0)
            legends.append([label.text for _, label in self.workspace.activity_plot.plotItem.legend.items])
        self.assertIn("CHDKPTP cumulative counter (not sensor drops)", legends[0])
        self.assertIn("CHDKPTP poll attempts", legends[1])
        self.assertIn("Payload fetch attempts", legends[2])
        self.assertIn("Device-list checks", legends[3])
        self.assertIn("Software skips", legends[5])
        self.workspace.dataset_combo.setCurrentIndex(5)
        APP.processEvents()
        self.assertNotEqual(self.workspace.metric_values["content"].text(), "Not recorded")
        self.assertNotEqual(self.workspace.metric_values["content_samples"].text(), "Not recorded")
        self.assertNotEqual(self.workspace.metric_values["exact_repeats"].text(), "Not recorded")

    def test_metric_units_and_explanatory_text_follow_light_and_dark_palettes(self) -> None:
        self.workspace = PerformanceMonitorWorkspace()
        self.assertIn("(ms)", self.workspace.metric_labels["duration"].text())
        self.assertIn("(kbps)", self.workspace.metric_labels["throughput"].text())
        self.assertIn("(kbps)", self.workspace.metric_labels["usb_speed"].text())
        self.assertIn("palette(text)", self.workspace.status_detail.styleSheet())
        self.assertIn("palette(text)", self.workspace.replay_position.styleSheet())
        for background, foreground in (("#ffffff", "#161616"), ("#202124", "#f1f3f4")):
            palette = QPalette(self.workspace.palette())
            palette.setColor(QPalette.ColorRole.Window, QColor(background))
            palette.setColor(QPalette.ColorRole.Text, QColor(foreground))
            self.workspace.setPalette(palette)
            APP.processEvents()
            self.workspace._apply_plot_palette()
            self.assertEqual(
                self.workspace.rate_plot.getAxis("left").textPen().color().name(), foreground
            )

    def test_live_multi_camera_sources_keep_independent_rings_and_stop_state(self) -> None:
        from camera_exploration_lab.capture import CaptureState

        first = OpenCVCaptureWorker(
            "synthetic-camera-a", CaptureMode("a", 320, 240, 187),
            capture_factory=lambda _device: self.fail("monitor must not start a source"),
        )
        second = OpenCVCaptureWorker(
            "synthetic-camera-b", CaptureMode("b", 320, 240, 30),
            capture_factory=lambda _device: self.fail("monitor must not start a source"),
        )
        def append(worker, timestamp, sequence, fps):
            worker.telemetry_buffer.append(TelemetrySample(
                timestamp_ns=timestamp, source="synthetic worker",
                clock_domain=ClockDomain.HOST_MONOTONIC, sequence=sequence,
                readings=(reading(Metric.OPENCV_CAPTURE_FPS, fps,
                                  provenance="Synthetic worker-local rate."),),
            ))
        append(first, 10_000_000_000, 0, 187.0)
        append(second, 30_000_000_000, 0, 24.0)
        sources = [
            LiveCaptureSource("camera-a", "Camera A", first, 31.0),
            LiveCaptureSource("camera-b", "Camera B", second, 23.0),
        ]
        self.workspace = PerformanceMonitorWorkspace(source_provider=lambda: tuple(sources))
        self.workspace.mode_combo.setCurrentIndex(1)
        APP.processEvents()
        self.workspace._poll_live()
        self.assertEqual(self.workspace._live_capture_rates[-1], 187.0)
        self.workspace.source_combo.setCurrentIndex(1)
        APP.processEvents()
        self.workspace._poll_live()
        self.assertEqual(tuple(self.workspace._live_capture_rates), (24.0,))
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        second._update(state=CaptureState.STOPPED, message="Synthetic capture stopped.")
        self.workspace._poll_live()
        self.assertEqual(self.workspace.metric_values["state"].text(), "Stopped")
        self.assertEqual(tuple(self.workspace._live_capture_rates), (24.0,))

        replacement = OpenCVCaptureWorker(
            "synthetic-camera-b-reconnected", CaptureMode("b-reconnected", 320, 240, 30),
            capture_factory=lambda _device: self.fail("monitor must not start a source"),
        )
        replacement._update(state=CaptureState.RUNNING, message="Synthetic reconnection.")
        append(replacement, 40_000_000_000, 0, 27.5)
        sources[1] = LiveCaptureSource("camera-b", "Camera B reconnected", replacement, 26.0)
        self.workspace._poll_live()
        self.assertEqual(self.workspace.source_combo.currentData(), "camera-b")
        self.assertEqual(self.workspace.source_combo.currentText(), "Camera B reconnected")
        self.assertEqual(tuple(self.workspace._live_capture_rates), (27.5,))
        self.assertEqual(self.workspace.metric_values["state"].text(), "Running")

    def test_normal_workbench_exposes_monitor_without_v3_migration_or_camera_open(self) -> None:
        class EmptyDiscovery:
            def discover(self):
                return []

        opened: list[str] = []

        def forbidden_capture(device: str):
            opened.append(device)
            raise AssertionError("workbench construction must not open a camera")

        with tempfile.TemporaryDirectory(prefix="cel-monitor-v2-test-") as temporary:
            database = Database(Path(temporary) / "synthetic-v2.sqlite3")
            window = LiveWorkbenchWindow(
                discovery=EmptyDiscovery(),
                database=database,
                capture_factory=forbidden_capture,
            )
            try:
                labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]
                self.assertIn("Performance Monitor", labels)
                self.assertIn("History", labels)
                self.assertIn("Analysis", labels)
                with database.connect() as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(window._performance_sources(), ())
                self.assertEqual(opened, [])
            finally:
                window.close()
                APP.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
