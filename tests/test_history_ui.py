from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from camera_exploration_lab.capture import CaptureState
    from camera_exploration_lab.db import Database
    from camera_exploration_lab.models import BackendKind, BenchmarkRecord, CameraCandidate, CameraIdentity, MeasurementSample, SessionRecord, StreamScope
    from camera_exploration_lab.ui.live_workbench import LiveWorkbenchWindow
    from camera_exploration_lab.ui.live_workbench import recording_readiness_message

    QT_AVAILABLE = True
except ImportError:
    QApplication = None  # type: ignore[assignment,misc]
    Database = None  # type: ignore[assignment,misc]
    LiveWorkbenchWindow = None  # type: ignore[assignment,misc]
    QT_AVAILABLE = False


class SyntheticFrame:
    shape = (2, 2, 3)
    strides = (6, 3, 1)
    flags = None
    data = bytes(12)


class FakeCapture:
    def __init__(self) -> None:
        self.properties = {3: 640.0, 4: 480.0, 5: 60.0, 6: float(int.from_bytes(b"GRBG", "little"))}
        self.released = False
        self.fail_reads = False
        self.opened = True

    def isOpened(self) -> bool:
        return self.opened

    def set(self, prop: int, value: float) -> bool:
        self.properties[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.properties.get(prop, 0.0)

    def read(self):
        time.sleep(0.003)
        if self.fail_reads:
            return False, None
        return True, SyntheticFrame()

    def release(self) -> None:
        self.released = True


class SyntheticDiscovery:
    def __init__(self) -> None:
        self.candidate = CameraCandidate(
            identity=CameraIdentity(
                stable_identifier="/dev/v4l/by-id/usb-Synthetic_PS3_Eye-video-index0",
                backend=BackendKind.V4L2,
                device_path="/dev/video9",
                manufacturer="Synthetic",
                model="Mock PS3 Eye",
                vendor_id="1415",
                product_id="2000",
                identity_source="v4l_by_id",
            ),
            display_name="Mock PS3 Eye",
        )

    def discover(self):
        return [self.candidate]


@unittest.skipUnless(QT_AVAILABLE, "PySide6 desktop extra is not installed")
class HistoryAndRecordingWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "ui-history.sqlite3")
        self.database.initialize()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    def _process_until(self, predicate, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.application.processEvents()
        return predicate()

    def test_recording_status_reports_missing_name_when_preview_is_ready(self) -> None:
        window = LiveWorkbenchWindow(
            SyntheticDiscovery(), self.database,
            capture_factory=lambda _device: FakeCapture(),
        )
        window.camera_combo.setCurrentIndex(0)
        window.start_capture()
        self.assertTrue(self._process_until(
            lambda: window._worker is not None
            and window._worker.snapshot().state == CaptureState.RUNNING
        ))
        self.assertEqual(window.recording_status.text(), "Enter a short experiment name first.")
        self.assertFalse(window.record_start_button.isEnabled())
        window.experiment_name.setText("Synthetic status check")
        self.assertEqual(
            window.recording_status.text(),
            "Ready to record; live preview will continue independently.",
        )
        self.assertTrue(window.record_start_button.isEnabled())
        window.close()

    def test_recording_status_helper_prioritizes_live_blocker(self) -> None:
        self.assertEqual(
            recording_readiness_message(
                has_camera=True, has_mode=True, capture_ready=True, has_name=False,
            ),
            "Enter a short experiment name first.",
        )
        self.assertEqual(
            recording_readiness_message(
                has_camera=True, has_mode=True, capture_ready=False, has_name=True,
            ),
            "Start preview and wait for negotiated camera settings before recording.",
        )
        self.assertIn(
            "initialization failed",
            recording_readiness_message(
                has_camera=True, has_mode=True, capture_ready=False, has_name=True,
                capture_state=CaptureState.FAILED,
            ).lower(),
        )

    def test_preview_continues_during_recording_and_history_includes_imported_data(self) -> None:
        imported = SessionRecord(camera_id=None, label="Imported: ps-eye.csv", source="imported")
        self.database.create_session(imported)
        old_run = BenchmarkRecord(session_id=imported.id, label="legacy 320x240", status="imported")
        self.database.create_benchmark(old_run)
        self.database.record_measurement(
            MeasurementSample(old_run.id, "capture_fps", 124.9, "frames/s", StreamScope.CAPTURE)
        )
        fake_capture = FakeCapture()
        window = LiveWorkbenchWindow(
            SyntheticDiscovery(),
            self.database,
            capture_factory=lambda _device: fake_capture,
        )
        window.show()
        window.refresh_history()
        self.assertEqual(window.history_table.rowCount(), 1)
        self.assertIn("Imported: ps-eye.csv", window.history_details.toPlainText())

        window.camera_combo.setCurrentIndex(0)
        window.experiment_name.setText("Synthetic preview independence")
        window.duration_combo.setCurrentIndex(window.duration_combo.count() - 1)  # manual stop
        window.start_capture()
        self.assertTrue(self._process_until(
            lambda: window._worker is not None
            and window._worker.snapshot().state == CaptureState.RUNNING
            and window._worker.snapshot().frame_count >= 10
        ))
        self.assertTrue(window.record_start_button.isEnabled())
        before = window._worker.snapshot().frame_count
        window.start_recording()
        self.assertTrue(self._process_until(
            lambda: window._recorder is not None and window._recorder.snapshot().frame_count >= 5
        ))
        time.sleep(0.05)
        self.application.processEvents()
        during = window._worker.snapshot().frame_count
        self.assertGreater(during, before)
        self.assertTrue(window._worker.is_alive())

        window.stop_recording()
        self.assertEqual(window.history_table.rowCount(), 2)
        labels = [window.history_table.item(row, 0).text() for row in range(window.history_table.rowCount())]
        self.assertTrue(any("Synthetic preview independence" in label for label in labels))
        with self.database.connect() as connection:
            live_run = connection.execute(
                """SELECT b.status, COUNT(m.id) AS sample_count FROM benchmark_runs b
                   LEFT JOIN measurements m ON m.benchmark_id=b.id
                   WHERE b.session_id IN (SELECT id FROM sessions WHERE label=?) GROUP BY b.id""",
                ("Synthetic preview independence",),
            ).fetchone()
        self.assertEqual(live_run["status"], "interrupted")
        self.assertGreater(live_run["sample_count"], 0)
        self.assertIn("Interrupted", window.recording_status.text())
        self.assertTrue(window._worker.is_alive(), "stopping experiment recording must leave live preview running")

        window.experiment_name.setText("Shutdown safety")
        window.start_recording()
        self.assertTrue(self._process_until(
            lambda: window._recorder is not None and window._recorder.snapshot().frame_count >= 3
        ))
        window.close()
        with self.database.connect() as connection:
            shutdown_status = connection.execute(
                """SELECT b.status, z.summary_json FROM benchmark_runs b
                   JOIN sessions s ON s.id=b.session_id
                   JOIN benchmark_summaries z ON z.benchmark_id=b.id
                   WHERE s.label='Shutdown safety'"""
            ).fetchone()
        self.assertEqual(shutdown_status["status"], "interrupted")
        self.assertIn("application_shutdown", shutdown_status["summary_json"])

    def test_repeated_mock_read_errors_interrupt_the_recording(self) -> None:
        fake_capture = FakeCapture()
        window = LiveWorkbenchWindow(
            SyntheticDiscovery(), self.database,
            capture_factory=lambda _device: fake_capture,
        )
        window.camera_combo.setCurrentIndex(0)
        window.experiment_name.setText("Mock disconnect")
        window.duration_combo.setCurrentIndex(window.duration_combo.count() - 1)
        window.start_capture()
        self.assertTrue(self._process_until(
            lambda: window._worker is not None
            and window._worker.snapshot().state == CaptureState.RUNNING
            and window._worker.snapshot().frame_count >= 5
        ))
        window.start_recording()
        self.assertTrue(self._process_until(
            lambda: window._recorder is not None and window._recorder.snapshot().frame_count >= 2
        ))
        fake_capture.fail_reads = True
        self.assertTrue(self._process_until(
            lambda: window._worker is not None
            and window._worker.snapshot().state == CaptureState.UNAVAILABLE
            and window._recorder is None
        ))
        self.assertIn("Interrupted", window.recording_status.text())
        with self.database.connect() as connection:
            run = connection.execute(
                """SELECT b.status, z.read_failures, z.summary_json FROM benchmark_runs b
                   JOIN sessions s ON s.id=b.session_id
                   JOIN benchmark_summaries z ON z.benchmark_id=b.id
                   WHERE s.label='Mock disconnect'"""
            ).fetchone()
        self.assertEqual(run["status"], "interrupted")
        self.assertGreaterEqual(run["read_failures"], 10)
        self.assertIn("camera_disconnected", run["summary_json"])
        window.close()

    def test_failed_capture_initialization_cannot_create_an_experiment(self) -> None:
        fake_capture = FakeCapture()
        fake_capture.opened = False
        window = LiveWorkbenchWindow(
            SyntheticDiscovery(), self.database,
            capture_factory=lambda _device: fake_capture,
        )
        window.camera_combo.setCurrentIndex(0)
        window.experiment_name.setText("Must not start")
        window.start_capture()
        self.assertTrue(self._process_until(
            lambda: window._worker is not None and window._worker.snapshot().state == CaptureState.FAILED
        ))
        window.start_recording()
        self.assertEqual(self.database.count("sessions"), 0)
        self.assertEqual(self.database.count("benchmark_runs"), 0)
        self.assertIn("initialization failed", window.recording_status.text().lower())
        window.close()

    def test_history_row_opens_the_same_run_in_analysis(self) -> None:
        session = SessionRecord(
            camera_id=None,
            label="Imported navigation fixture",
            source="imported",
            started_at="2026-10-13T12:00:00+00:00",
        )
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id,
            label="Legacy 320x240",
            status="imported",
            configuration={"legacy_mode": "320x240"},
        )
        self.database.create_benchmark(benchmark)
        self.database.record_measurements_batch([
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="capture_frame_interval_ms",
                value=8.0,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                sample_index=1,
                method="legacy_csv:interval_ms",
            ),
            MeasurementSample(
                benchmark_id=benchmark.id,
                metric="capture_frame_interval_ms",
                value=9.0,
                unit="ms",
                stream_scope=StreamScope.CAPTURE,
                sample_index=2,
                method="legacy_csv:interval_ms",
            ),
        ])
        window = LiveWorkbenchWindow(SyntheticDiscovery(), self.database)
        window.refresh_history()
        self.assertEqual(window.history_table.rowCount(), 1)
        window.history_table.selectRow(0)
        window.analyze_history_button.click()

        self.assertEqual(window.tabs.currentWidget(), window.analysis_workspace)
        self.assertTrue(self._process_until(
            lambda: window.analysis_workspace._primary is not None
            and window.analysis_workspace._primary.option.session_id == session.id
        ))
        self.assertEqual(
            window.analysis_workspace.experiment_combo.currentData(),
            (session.id, benchmark.id),
        )
        self.assertEqual(
            window.analysis_workspace._primary.interval_stats.count,
            2,
        )
        self.assertEqual(len(window.analysis_workspace.interval_plot.plotItem.listDataItems()), 1)
        window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
