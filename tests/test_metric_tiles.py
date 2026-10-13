from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from camera_exploration_lab.capture import CaptureSnapshot, CaptureState, LatestFrameBuffer, NegotiatedSettings
    from camera_exploration_lab.capture_modes import CaptureMode
    from camera_exploration_lab.db import Database
    from camera_exploration_lab.ui.live_workbench import LiveWorkbenchWindow

    QT_AVAILABLE = True
except ImportError:
    QApplication = None  # type: ignore[assignment,misc]
    CaptureSnapshot = None  # type: ignore[assignment,misc]
    CaptureState = None  # type: ignore[assignment,misc]
    LatestFrameBuffer = None  # type: ignore[assignment,misc]
    NegotiatedSettings = None  # type: ignore[assignment,misc]
    CaptureMode = None  # type: ignore[assignment,misc]
    Database = None  # type: ignore[assignment,misc]
    LiveWorkbenchWindow = None  # type: ignore[assignment,misc]
    QT_AVAILABLE = False


class EmptyDiscovery:
    def discover(self):
        return []


@unittest.skipUnless(QT_AVAILABLE, "PySide6 desktop extra is not installed")
class MetricTileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _window(self, discovery):
        database = Database(":memory:")
        self.addCleanup(database.close)
        return LiveWorkbenchWindow(discovery, database=database)

    def test_each_metric_has_a_persistent_visible_label_and_explanatory_tooltip(self) -> None:
        window = self._window(EmptyDiscovery())
        window.show()
        self.application.processEvents()
        cards = (
            (window.capture_fps_card, "Capture FPS", "Capture can continue faster than the preview updates."),
            (window.preview_fps_card, "Preview FPS", "measured separately from capture FPS."),
            (window.interval_card, "Frame Interval (ms)", "not sensor-to-display latency."),
            (window.failures_card, "Read Failures", "unsuccessful frame reads"),
        )
        for card, expected_title, tooltip_excerpt in cards:
            with self.subTest(metric=expected_title):
                self.assertEqual(card.title_label.text(), expected_title)
                self.assertTrue(card.title_label.isVisible())
                self.assertIn(tooltip_excerpt, card.toolTip())
                self.assertEqual(card.title_label.toolTip(), card.toolTip())
                self.assertEqual(card.value.toolTip(), card.toolTip())
        window.close()

    def test_frame_interval_value_uses_the_persistently_labeled_milliseconds_unit(self) -> None:
        window = self._window(EmptyDiscovery())
        snapshot = CaptureSnapshot(
            state=CaptureState.RUNNING,
            requested=CaptureMode("synthetic", 640, 480, 30),
            negotiated=NegotiatedSettings(640, 480, 30.0, "MJPG"),
            capture_fps=23.9,
            frame_interval_ms=5.384,
            read_failures=0,
            message="Synthetic capture",
        )

        class SnapshotWorker:
            frame_buffer = LatestFrameBuffer()

            def snapshot(self):
                return snapshot

            def is_alive(self):
                return False

        window._worker = SnapshotWorker()
        window._poll_capture()
        self.assertEqual(window.interval_card.title_label.text(), "Frame Interval (ms)")
        self.assertEqual(window.interval_card.value.text(), "5.38")
        self.assertEqual(window.capture_fps_card.value.text(), "23.9")
        window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
