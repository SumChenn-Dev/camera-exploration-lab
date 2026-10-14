from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.analysis import load_analysis as real_load_analysis
from camera_exploration_lab.db import Database
from camera_exploration_lab.models import BenchmarkRecord, MeasurementSample, SessionRecord, StreamScope
from camera_exploration_lab.ui.analysis_workspace import AnalysisWorkspace


class AnalysisWorkspaceResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
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
        self.database.close()
        self.tempdir.cleanup()

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
            workspace = AnalysisWorkspace(self.database)
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
                sample_index=index + 1,
                interval_ns=(40 + index) * 1_000_000,
                method="synthetic MJPEG intervals",
            )
            for index in range(4)
        ])

        workspace = AnalysisWorkspace(self.database)
        workspace.show()
        self.assertTrue(self._process_until(lambda: workspace._primary is not None, timeout=5.0))
        primary_data = workspace.experiment_combo.currentData()
        other_index = next(
            index for index in range(1, workspace.compare_combo.count())
            if workspace.compare_combo.itemData(index) != primary_data
        )
        workspace.compare_combo.setCurrentIndex(other_index)
        self.assertTrue(self._process_until(lambda: workspace._comparison is not None, timeout=5.0))
        self.assertNotEqual(workspace.compare_combo.currentData(), primary_data)

        self.assertGreater(workspace.comparison_table.rowCount(), 10)
        self.assertEqual(len(workspace.distribution_plot.plotItem.listDataItems()), 2)
        labels = [
            workspace.comparison_table.item(row, 0).text()
            for row in range(workspace.comparison_table.rowCount())
        ]
        self.assertIn("Capture FPS mean", labels)
        self.assertIn("Capture read failures", labels)
        workspace.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
