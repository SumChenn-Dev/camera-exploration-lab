from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.db import Database
from camera_exploration_lab.history import filter_history_rows, make_history_rows
from camera_exploration_lab.models import BackendKind, BenchmarkRecord, CameraIdentity, SessionRecord
from camera_exploration_lab.identity import identity_fingerprint
from camera_exploration_lab.ui.history_workspace import HistoryWorkspace


class HistorySearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        legacy_camera_id = self.database.upsert_camera(CameraIdentity(
            "legacy:sony-eye:1415:2000", BackendKind.V4L2,
            manufacturer="Sony", model="PlayStation Eye", vendor_id="1415",
            product_id="2000", identity_source="inferred_from_legacy_path",
        ))
        native_camera_id = self.database.upsert_camera(CameraIdentity(
            "/dev/v4l/by-id/synthetic-eye", BackendKind.V4L2,
            manufacturer="Sony", model="PlayStation Eye", vendor_id="1415",
            product_id="2000", identity_source="v4l_by_id",
        ))
        other_camera_id = self.database.upsert_camera(CameraIdentity(
            "/dev/v4l/by-id/synthetic-cam313", BackendKind.V4L2,
            manufacturer="AVerMedia", model="CAM 313", vendor_id="07ca",
            product_id="313a", identity_source="v4l_by_id",
        ))
        self.sessions: dict[str, tuple[str, str]] = {}
        self._add_run(legacy_camera_id, "Legacy baseline", "imported", "imported", "2026-10-10T12:00:00+00:00",
                      "window light baseline", "640x480 GRBG", 60, "imported")
        self._add_run(native_camera_id, "High rate follow-up", "live_experiment", "completed", "2026-10-11T12:00:00+00:00",
                      "desk lamp setup", "320x240 GRBG", 187, "completed")
        self._add_run(other_camera_id, "CAM 313 exposure check", "live_experiment", "interrupted", "2026-10-12T12:00:00+00:00",
                      "manual exposure attempt", "1920x1080 MJPEG", 24, "interrupted")
        self.workspace: HistoryWorkspace | None = None

    def _add_run(self, camera_id: str, label: str, source: str, status: str, started: str,
                 notes: str, mode: str, fps: int, benchmark_status: str) -> None:
        session = SessionRecord(camera_id=camera_id, label=label, source=source, started_at=started, notes=notes)
        self.database.create_session(session)
        benchmark = BenchmarkRecord(
            session_id=session.id,
            label=mode,
            started_at=started,
            status=benchmark_status,
            configuration={
                "requested": {"width": int(mode.split("x")[0]), "height": int(mode.split("x")[1].split()[0]),
                              "pixel_format": mode.split()[-1], "fps": fps},
                "capture_timing_method": "synthetic host-arrival fixture",
            },
        )
        self.database.create_benchmark(benchmark)
        self.sessions[label] = (session.id, benchmark.id)

    def tearDown(self) -> None:
        if self.workspace is not None:
            self.workspace.stop_background_work()
            self.workspace.close()
            self.application.processEvents()
        self.database.close()
        self.tempdir.cleanup()

    def _process_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        self.application.processEvents()
        return predicate()

    def _open_workspace(self) -> HistoryWorkspace:
        self.workspace = HistoryWorkspace(self.database)
        self.workspace.show()
        self.assertTrue(self._process_until(lambda: self.workspace.model.rowCount() == 3))
        return self.workspace

    def test_search_covers_name_camera_source_notes_mode_fps_date_and_status(self) -> None:
        workspace = self._open_workspace()
        for query, expected_count in (
            ("follow-up", 1), ("PlayStation Eye", 2), ("live_experiment", 2),
            ("desk lamp", 1), ("320x240", 1), ("187", 1),
            ("2026-10-11", 1), ("completed", 1), ("Imported CSV", 1),
        ):
            workspace.search_edit.setText(query)
            self.assertTrue(self._process_until(lambda: workspace.proxy.rowCount() == expected_count), query)
            workspace.search_edit.clear()
            self.assertTrue(self._process_until(lambda: workspace.proxy.rowCount() == 3))

    def test_camera_and_source_filters_group_models_without_merging_identities(self) -> None:
        workspace = self._open_workspace()
        eye_index = workspace.camera_filter.findData("usb-family:1415:2000")
        self.assertGreater(eye_index, 0)
        workspace.camera_filter.setCurrentIndex(eye_index)
        self.assertEqual(workspace.proxy.rowCount(), 2)
        identity_sources = {row.cells[1].split(" · ")[1] for row in workspace.model.rows
                            if row.camera_group_key == "usb-family:1415:2000"}
        self.assertIn("Legacy CSV inferred", identity_sources)
        self.assertIn("V4L2 by-id", identity_sources)
        source_index = workspace.source_filter.findData("imported")
        workspace.source_filter.setCurrentIndex(source_index)
        self.assertEqual(workspace.proxy.rowCount(), 1)
        self.assertIn("1 of 3", workspace.result_count.text())
        workspace.clear_button.click()
        self.assertEqual(workspace.proxy.rowCount(), 3)
        self.assertEqual(workspace.search_edit.text(), "")

    def test_metrics_guide_is_available_from_history_toolbar(self) -> None:
        workspace = self._open_workspace()
        workspace.metrics_guide_button.click()
        dialog = workspace._metrics_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertGreaterEqual(dialog.topic_list.count(), 12)
        dialog.close()

    def test_history_detail_keeps_method_fields_but_masks_raw_identity_and_paths(self) -> None:
        detail = {
            "entry": {
                "label": "Synthetic detail record", "source": "imported", "status": "interrupted",
                "model": "Synthetic Camera", "stable_identifier": "synthetic-identity-key",
                "identity_source": "inferred_from_legacy_path", "started_at": "2026-10-14",
                "source_basename": "synthetic.csv", "imported_row_count": 8,
                "imported_measurement_count": 6, "raw_record_count": 8,
                "configuration_json": json.dumps({"requested": {"fps": 60}, "device_path": "mock/device-path"}),
                "summary_json": json.dumps({"termination_reason": "synthetic_timeout"}),
                "duration_seconds": 3.0, "capture_fps": 58.7, "preview_fps": 31.0,
                "frame_count": 176, "read_failures": 0, "interval_count": 175,
                "median_interval_ms": 16.8, "p95_interval_ms": 17.2, "p99_interval_ms": 17.5,
            },
            "settings": [{
                "setting_key": "fps", "requested_value_json": "60", "reported_value_json": "59.9",
                "verification_status": "reported", "verification_method": "synthetic readback",
            }],
            "metrics": [],
        }
        rendered = HistoryWorkspace._format_detail(detail)
        self.assertIn("Original rows preserved: 8", rendered)
        self.assertIn("reported; synthetic readback", rendered)
        self.assertIn("Termination reason: synthetic_timeout", rendered)
        self.assertIn(identity_fingerprint("synthetic-identity-key"), rendered)
        self.assertNotIn("synthetic-identity-key", rendered)
        self.assertNotIn("mock/device-path", rendered)

    def test_filtering_and_sorting_preserve_exact_analyze_selection(self) -> None:
        workspace = self._open_workspace()
        workspace.history_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        target = self.sessions["High rate follow-up"]
        target_row = next(
            row for row in range(workspace.proxy.rowCount())
            if workspace.proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) == target
        )
        workspace.search_edit.setText("high rate")
        self.assertTrue(self._process_until(lambda: workspace.proxy.rowCount() == 1))
        index = workspace.proxy.index(0, 0)
        workspace.history_table.setCurrentIndex(index)
        selected: list[tuple[str, object]] = []
        workspace.analyze_requested.connect(lambda session_id, benchmark_id: selected.append((session_id, benchmark_id)))
        workspace.analyze_button.click()
        self.assertEqual(selected, [(target[0], target[1])])
        self.assertGreaterEqual(target_row, 0)

    def test_text_filter_helpers_and_history_reads_are_read_only(self) -> None:
        rows = make_history_rows(self.database.history_entries())
        self.assertEqual(len(filter_history_rows(rows, query="window light")), 1)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM sessions")
        self.database.history_entries()
        self.database.history_detail(*self.sessions["Legacy baseline"][::-1])
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_large_history_index_is_built_off_the_gui_thread(self) -> None:
        base = self.database.history_entries()[0]
        entries = []
        for index in range(2_000):
            entry = dict(base)
            entry["session_id"] = f"synthetic-session-{index}"
            entry["benchmark_id"] = f"synthetic-benchmark-{index}"
            entry["session_label"] = f"Synthetic history row {index}"
            entries.append(entry)
        worker_threads: list[int] = []

        def slow_index():
            worker_threads.append(threading.get_ident())
            time.sleep(0.12)
            return entries

        with patch.object(self.database, "history_entries", side_effect=slow_index), patch.object(
            self.database, "history_detail", return_value={"entry": {"label": "synthetic"}, "settings": [], "metrics": []}
        ):
            self.workspace = HistoryWorkspace(self.database)
            self.workspace.show()
            timer_fired: list[bool] = []
            from PySide6.QtCore import QTimer
            QTimer.singleShot(10, lambda: timer_fired.append(True))
            self.assertTrue(self._process_until(lambda: bool(timer_fired), timeout=0.08))
            self.assertEqual(self.workspace.model.rowCount(), 0)
            self.assertTrue(self._process_until(lambda: self.workspace.proxy.rowCount() == 2_000, timeout=5.0))
            self.assertTrue(self._process_until(
                lambda: "synthetic" in self.workspace.history_details.toPlainText(), timeout=5.0
            ))

        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], threading.get_ident())


if __name__ == "__main__":
    unittest.main(verbosity=2)
