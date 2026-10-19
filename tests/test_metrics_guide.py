from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.ui.metrics_guide import METRIC_GUIDE, MetricsGuideDialog


class MetricsGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_guide_covers_requested_topics_with_consistent_explanation_fields(self) -> None:
        entries = {entry.key: entry for entry in METRIC_GUIDE}
        self.assertTrue({
            "capture_fps", "preview_fps", "processing_fps", "frame_count", "frame_interval",
            "mean_median", "percentiles", "jitter", "read_failures", "host_timestamps",
            "latency", "methodology",
        }.issubset(entries))
        for entry in entries.values():
            self.assertTrue(entry.meaning.strip(), entry.key)
            self.assertTrue(entry.units.strip(), entry.key)
            self.assertTrue(entry.example.startswith("SYNTHETIC:"), entry.key)
            self.assertTrue(entry.interpretation.strip(), entry.key)
            self.assertTrue(entry.misunderstanding.strip(), entry.key)
            self.assertTrue(entry.method.strip(), entry.key)

    def test_context_topic_opens_selected_explanation(self) -> None:
        dialog = MetricsGuideDialog("latency")
        dialog.show()
        self.application.processEvents()
        current = dialog.topic_list.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.data(Qt.ItemDataRole.UserRole), "latency")
        self.assertIn("does not directly measure", dialog.explanation.toPlainText())
        dialog.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
