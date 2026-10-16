from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import threading
import time
from contextlib import closing
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.capture import CaptureState
from camera_exploration_lab.db import Database
from camera_exploration_lab.capture_recording_adapter import (
    GroupedCaptureRecordingBridge,
    GroupedRecordingPersistenceError,
)
from camera_exploration_lab.grouped_recording_coordinator import GroupedRecordingCoordinator
from camera_exploration_lab.grouped_recording import GroupedExperimentRepository, RecordingLeaseBusyError
from camera_exploration_lab.schema_migrations import SYNTHETIC_DATABASE_APPLICATION_ID
from camera_exploration_lab.ui.synthetic_recording_harness import (
    SyntheticCaptureSource,
    SyntheticRecordingWindow,
    parse_demo_args,
)

APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout: float = 4.0) -> bool:
    """Pump the Qt event loop without blocking it on a worker or long sleep."""
    if predicate():
        return True
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(round(timeout * 1000))
    loop.exec()
    poll.stop()
    return bool(predicate())


def contrast_ratio_for_test(foreground: QColor, background: QColor) -> float:
    def luminance(color: QColor) -> float:
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class SyntheticRecordingUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources: dict[str, SyntheticCaptureSource] = {}
        self.window: SyntheticRecordingWindow | None = None

    def tearDown(self) -> None:
        if self.window is not None and not self.window._close_complete:
            self.window.close()
            wait_until(lambda: self.window is not None and self.window._close_complete, 4.0)
        APP.processEvents()
        for source in self.sources.values():
            self.assertFalse(source._thread and source._thread.is_alive(), "synthetic producer thread leaked")
        if self.window is not None:
            self.assertFalse(self.window._thread.isRunning(), "Qt orchestration thread leaked")

    def make_window(
        self, *, failed_slots: set[str] | None = None, auto_emit: bool = False,
        fail_after_frames: dict[str, int] | None = None,
    ):
        failed_slots = failed_slots or set()
        fail_after_frames = fail_after_frames or {}

        def source_factory(slot: str, fps: int, **kwargs):
            source = SyntheticCaptureSource(
                slot, fps, auto_emit=auto_emit,
                start_allowed=slot not in failed_slots, **kwargs,
                fail_after_frames=fail_after_frames.get(slot),
            )
            self.sources[slot] = source
            return source

        self.window = SyntheticRecordingWindow(source_factory=source_factory)
        self.window.resize(1024, 768)
        self.window.show()
        APP.processEvents()
        return self.window

    def acknowledge_and_start(self, window: SyntheticRecordingWindow) -> None:
        self.assertFalse(window.start_both_button.isEnabled())
        window.acknowledgement.setChecked(True)
        self.assertTrue(window.start_both_button.isEnabled())
        window.start_both_button.click()
        self.assertTrue(wait_until(lambda: all(
            window._latest_view is not None
            and stream.capture_state in {"running", "failed"}
            for stream in window._latest_view.streams
        )))

    @staticmethod
    def emit_samples(source: SyntheticCaptureSource, count: int) -> None:
        for _ in range(count):
            source.emit_frame(time.monotonic_ns())

    def test_inspect_request_disables_duplicate_load_actions(self) -> None:
        window = self.make_window()
        window.acknowledgement.setChecked(True)
        dispatched = []
        window.request_inspect_group.connect(lambda: dispatched.append("inspect"))

        window._request_inspect()
        self.assertFalse(window.inspect_button.isEnabled())
        self.assertIn("Loading committed samples", window.inspection_label.text())
        self.assertEqual(dispatched, ["inspect"])

        # A rapid repeated slot invocation is ignored while the first request
        # is pending, so it cannot enqueue a second long-running read.
        window._request_inspect()
        self.assertEqual(dispatched, ["inspect"])
        self.assertTrue(wait_until(lambda: window._pending_action is None))
        self.assertIn("Inspect failed", window.state_label.text())

    def test_cli_requires_synthetic_ack_and_rejects_database_path(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_demo_args([])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_demo_args(["--synthetic-only", "--acknowledge-synthetic-only", "--database", "local.sqlite3"])
        args = parse_demo_args(["--synthetic-only", "--acknowledge-synthetic-only"])
        self.assertTrue(args.synthetic_only)
        self.assertTrue(args.acknowledge_synthetic_only)
        self.assertNotIn("--database", parse_demo_args.__code__.co_consts)

    def test_record_both_is_async_persists_then_stops_without_stopping_fake_capture(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        source_a, source_b = self.sources["A"], self.sources["B"]
        window.record_both_button.click()
        self.assertIn("requested", window.state_label.text().lower())
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.session_id is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.assertIn("acknowledged", window.state_label.text().lower())

        self.emit_samples(source_a, 5)
        self.emit_samples(source_b, 3)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples >= 5
                                   and window._latest_view.streams[1].persisted_samples >= 3))
        persisted_before_stop = tuple(item.persisted_samples for item in window._latest_view.streams)
        frames_before_stop = source_a.snapshot().frame_count
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "partial"))
        self.assertIn("finalization committed", window.state_label.text().lower())
        session_id = window._latest_view.session_id
        completed_requests = QSignalSpy(window._worker.action_finished)
        window.request_stop_recording.emit()
        window.request_stop_recording.emit()
        self.assertTrue(wait_until(lambda: completed_requests.count() >= 2))
        self.assertEqual(window._latest_view.session_id, session_id)
        with closing(sqlite3.connect(f"{window._worker._database.path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM group_recordings").fetchone()[0], 1)
        self.assertEqual(source_a.snapshot().state, CaptureState.RUNNING)
        self.assertEqual(source_b.snapshot().state, CaptureState.RUNNING)
        self.assertIn("finalized as partial", window.capture_status_label.text().lower())
        self.assertIn("capture is still running", window.capture_status_label.text().lower())
        self.assertIn("stop both", window.capture_status_label.text().lower())
        self.assertIn("live capture events since start both", " ".join(
            label.text().lower() for label in window._stream_panels[0].metric_title_labels
        ))
        self.assertIn("timing samples committed to sqlite", " ".join(
            label.text().lower() for label in window._stream_panels[0].metric_title_labels
        ))
        database_path = window._worker._database.path
        with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], SYNTHETIC_DATABASE_APPLICATION_ID)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM benchmark_streams").fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(DISTINCT slot) FROM benchmark_streams"
            ).fetchone()[0], 2)
            rows = connection.execute(
                "SELECT benchmark_id, sample_index, elapsed_ns, source_timestamp_ns, source_clock "
                "FROM measurements WHERE metric='frame_arrival' ORDER BY benchmark_id, sample_index"
            ).fetchall()
        self.assertEqual(len(rows), sum(persisted_before_stop))
        self.assertTrue(all(row[2] is not None and row[3] is None and row[4] is None for row in rows))

        source_a.emit_frame(time.monotonic_ns())
        self.assertEqual(source_a.snapshot().frame_count, frames_before_stop + 1)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples == persisted_before_stop[0]))
        window.inspect_button.click()
        self.assertTrue(wait_until(lambda: "Saved synthetic group" in window.inspection_label.text()))
        self.assertIn("persisted", window.inspection_label.text())
        self.assertIn("Read-only recovery inspection", window.inspection_label.text())
        self.assertIn("partial", window.inspection_label.text())

        window.stop_both_button.click()
        self.assertTrue(wait_until(lambda: all(
            stream.capture_state == "stopped" for stream in window._latest_view.streams
        )))
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))
        self.assertFalse(window._thread.isRunning())

    def test_close_during_grouped_analysis_cancels_loader_without_stale_result(self) -> None:
        window = self.make_window(auto_emit=True)
        self.acknowledge_and_start(window)
        window.duration.setCurrentIndex(0)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "completed", 4.0))

        entered = threading.Event()
        from camera_exploration_lab.grouped_analysis import GroupedAnalysisCancelled

        def wait_for_shutdown(_database, _session_id, *, cancellation_check=None):
            entered.set()
            while cancellation_check is not None and not cancellation_check():
                time.sleep(0.001)
            raise GroupedAnalysisCancelled("synthetic viewer close cancellation")

        with patch(
            "camera_exploration_lab.ui.synthetic_recording_harness.load_grouped_analysis",
            side_effect=wait_for_shutdown,
        ):
            window._request_inspect()
            self.assertTrue(wait_until(entered.is_set))
            window.close()
            self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertIsNone(window._explorer_dialog)
        self.assertFalse(window._thread.isRunning())

    def test_inspection_counts_one_and_two_frame_rows_not_zero_based_indexes(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        self.assertEqual(self.sources["A"].mode.fps, 100)
        self.assertEqual(self.sources["B"].mode.fps, 24)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.emit_samples(self.sources["A"], 1)
        self.emit_samples(self.sources["B"], 2)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples == 1
                                   and window._latest_view.streams[1].persisted_samples == 2))
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "partial"))
        group_id = window._latest_view.session_id
        database_path = window._worker._database.path
        with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                """SELECT bs.slot, bs.last_persisted_sample_index, COUNT(m.id)
                   FROM benchmark_streams bs
                   LEFT JOIN measurements m ON m.benchmark_id=bs.benchmark_id
                       AND m.stream_scope='capture'
                       AND m.metric IN ('frame_arrival', 'read_failure')
                   WHERE bs.session_id=? GROUP BY bs.slot ORDER BY bs.slot""",
                (group_id,),
            ).fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [("A", 0, 1), ("B", 1, 2)])
        inspection_results = []
        window._worker.action_finished.connect(
            lambda action, result: inspection_results.append(result) if action == "inspect" else None
        )
        database = window._worker._database
        with patch.object(database, "inspect_recording_recovery", wraps=database.inspect_recording_recovery) as recovery_spy:
            window.inspect_button.click()
            self.assertTrue(wait_until(lambda: bool(inspection_results)))
        self.assertEqual(recovery_spy.call_count, 1, "reuse the loader's recovery snapshot instead of rescanning twice")
        inspected = {item["slot"]: item for item in inspection_results[-1]["streams"]}
        self.assertEqual(inspected["A"]["persisted_samples"], 1)
        self.assertEqual(inspected["B"]["persisted_samples"], 2)
        self.assertEqual(inspected["A"]["last_persisted_sample_index"], 0)
        self.assertEqual(inspected["B"]["last_persisted_sample_index"], 1)
        self.assertEqual(window._latest_view.streams[0].persisted_samples, 1)
        self.assertEqual(window._latest_view.streams[1].persisted_samples, 2)
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_inspection_reports_995_and_242_rows_not_last_zero_based_indexes(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.emit_samples(self.sources["A"], 995)
        self.emit_samples(self.sources["B"], 242)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples == 995
                                   and window._latest_view.streams[1].persisted_samples == 242, 8.0))
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "partial", 8.0))
        group_id = window._latest_view.session_id
        database_path = window._worker._database.path
        with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                """SELECT bs.slot, bs.last_persisted_sample_index, COUNT(m.id)
                   FROM benchmark_streams bs
                   LEFT JOIN measurements m ON m.benchmark_id=bs.benchmark_id
                       AND m.stream_scope='capture'
                       AND m.metric IN ('frame_arrival', 'read_failure')
                   WHERE bs.session_id=? GROUP BY bs.slot ORDER BY bs.slot""",
                (group_id,),
            ).fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [("A", 994, 995), ("B", 241, 242)])
        inspection_results = []
        window._worker.action_finished.connect(
            lambda action, result: inspection_results.append(result) if action == "inspect" else None
        )
        window.inspect_button.click()
        self.assertTrue(wait_until(lambda: bool(inspection_results)))
        inspected_counts = tuple(item["persisted_samples"] for item in inspection_results[-1]["streams"])
        self.assertEqual(inspected_counts, (995, 242))
        self.assertEqual(tuple(stream.persisted_samples for stream in window._latest_view.streams), (995, 242))
        self.assertIn("995 persisted", window.inspection_label.text())
        self.assertIn("242 persisted", window.inspection_label.text())
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_duration_target_auto_stops_sources_after_committed_finalization(self) -> None:
        window = self.make_window(auto_emit=True)
        self.assertFalse(window.keep_capture_running_checkbox.isChecked())
        self.acknowledge_and_start(window)
        window.duration.setCurrentIndex(0)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "completed"
                                   and all(item.capture_state == "stopped" for item in window._latest_view.streams), 4.0))
        self.assertIn("finalized as completed", window.state_label.text().lower())
        self.assertEqual(window._latest_view.writer_status, "Final transaction committed")
        self.assertIn(
            "Recording completed and saved. Both synthetic capture sources stopped automatically.",
            window.capture_status_label.text(),
        )
        session_id = window._latest_view.session_id
        self.assertIsNotNone(session_id)
        persisted_at_finalization = tuple(stream.persisted_samples for stream in window._latest_view.streams)
        frames_at_finalization = tuple(stream.frame_count for stream in window._latest_view.streams)
        self.assertGreater(persisted_at_finalization[0], 0)
        self.assertGreater(persisted_at_finalization[1], 0)
        self.assertEqual(tuple(stream.simulated_capture_fps for stream in window._latest_view.streams), (0.0, 0.0))
        self.assertTrue(all(source.snapshot().state is CaptureState.STOPPED for source in self.sources.values()))

        database = window._worker._database
        self.assertIsNotNone(database)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            persisted_rows = tuple(int(row[0]) for row in connection.execute(
                """SELECT COUNT(*) FROM measurements m
                   JOIN benchmark_streams bs ON bs.benchmark_id=m.benchmark_id
                   WHERE bs.session_id=? AND m.metric='frame_arrival' AND m.stream_scope='capture'
                   GROUP BY bs.slot ORDER BY bs.slot""",
                (session_id,),
            ).fetchall())
        self.assertEqual(persisted_rows, persisted_at_finalization)
        APP.processEvents()
        self.assertEqual(
            tuple(stream.frame_count for stream in window._latest_view.streams), frames_at_finalization,
            "live event totals must remain fixed after both synthetic sources stop",
        )
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM measurements WHERE metric='frame_arrival' AND stream_scope='capture'"
            ).fetchone()[0], sum(persisted_at_finalization))

        inspection_results = []
        window._worker.action_finished.connect(
            lambda action, result: inspection_results.append(result) if action == "inspect" else None
        )
        window.inspect_button.click()
        self.assertTrue(wait_until(lambda: bool(inspection_results)))
        inspected_counts = tuple(item["persisted_samples"] for item in inspection_results[-1]["streams"])
        self.assertEqual(inspected_counts, persisted_at_finalization)
        self.assertIn("Saved synthetic group", window.inspection_label.text())
        self.assertIsNotNone(window._explorer_dialog, "grouped History/Analysis stays available after auto-stop")
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_duration_target_keep_running_checkbox_preserves_capture_but_not_recording(self) -> None:
        window = self.make_window(auto_emit=True)
        window.keep_capture_running_checkbox.setChecked(True)
        self.acknowledge_and_start(window)
        window.duration.setCurrentIndex(0)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "completed", timeout=4.0))
        self.assertIn(
            "Recording completed and saved. Synthetic capture is still running; use Stop Both to stop the sources.",
            window.capture_status_label.text(),
        )
        persisted_at_finalization = tuple(stream.persisted_samples for stream in window._latest_view.streams)
        frames_at_finalization = tuple(stream.frame_count for stream in window._latest_view.streams)
        self.assertGreater(persisted_at_finalization[0], 0)
        self.assertGreater(persisted_at_finalization[1], 0)
        self.assertTrue(all(source.snapshot().state is CaptureState.RUNNING for source in self.sources.values()))
        self.assertTrue(wait_until(lambda: all(
            stream.frame_count > frames_at_finalization[index]
            for index, stream in enumerate(window._latest_view.streams)
        ), 2.0))
        self.assertEqual(
            tuple(stream.persisted_samples for stream in window._latest_view.streams),
            persisted_at_finalization,
            "finalized recording counters must stay fixed while live capture continues",
        )
        database = window._worker._database
        self.assertIsNotNone(database)
        with database.connect_readonly() as connection:
            row_count = int(connection.execute(
                "SELECT COUNT(*) FROM measurements WHERE metric='frame_arrival' AND stream_scope='capture'"
            ).fetchone()[0])
        self.assertEqual(row_count, sum(persisted_at_finalization))
        self.assertEqual(
            tuple(stream.persisted_samples for stream in window._latest_view.streams),
            persisted_at_finalization,
        )
        window.inspect_button.click()
        self.assertTrue(wait_until(lambda: "Saved synthetic group" in window.inspection_label.text()))
        window.stop_both_button.click()
        self.assertTrue(wait_until(lambda: all(
            stream.capture_state == "stopped" for stream in window._latest_view.streams
        )))
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_close_during_natural_finalization_waits_for_ack_then_shuts_sources_down(self) -> None:
        window = self.make_window(auto_emit=True)
        self.acknowledge_and_start(window)
        window.duration.setCurrentIndex(0)
        entered = threading.Event()
        release = threading.Event()
        original = GroupedCaptureRecordingBridge.stop_recording

        def gated_finalize(bridge, **kwargs):
            entered.set()
            if not release.wait(3.0):
                raise TimeoutError("synthetic natural-finalization gate timed out")
            return original(bridge, **kwargs)

        with patch.object(
            GroupedCaptureRecordingBridge, "stop_recording", autospec=True, side_effect=gated_finalize
        ):
            window.record_both_button.click()
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and window._latest_view.recording_state.startswith("Recording")))
            self.assertTrue(wait_until(entered.is_set, 3.0))
            window.close()
            self.assertFalse(window._close_complete)
            release.set()
            self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertFalse(window._thread.isRunning())
        self.assertIsNotNone(window._worker._final_record)
        self.assertEqual(str(window._worker._final_record.status), "completed")
        self.assertTrue(all(not (source._thread and source._thread.is_alive()) for source in self.sources.values()))
        self.assertNotEqual(window._worker._auto_stop_result, "stopped")

    def test_high_rate_synthetic_profiles_run_independently(self) -> None:
        window = self.make_window(auto_emit=True)
        window.acknowledgement.setChecked(True)
        window.rate_a.setCurrentIndex(1)
        window.rate_b.setCurrentIndex(1)
        window.start_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and all(item.capture_state == "running" for item in window._latest_view.streams)))
        self.assertTrue(wait_until(lambda: window._latest_view.streams[0].frame_count >= 50
                                   and window._latest_view.streams[1].frame_count >= 8, 2.0))
        stream_a, stream_b = window._latest_view.streams
        self.assertIn("187 fps", stream_a.capture_mode.lower())
        self.assertIn("30 fps", stream_b.capture_mode.lower())
        self.assertGreater(stream_a.frame_count, stream_b.frame_count * 2)
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_partial_startup_keeps_healthy_stream_and_group_is_explicitly_partial(self) -> None:
        window = self.make_window(failed_slots={"B"})
        self.acknowledge_and_start(window)
        self.assertEqual(window._latest_view.streams[0].capture_state, "running")
        self.assertEqual(window._latest_view.streams[1].capture_state, "failed")
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state == "Recording partially"))
        self.emit_samples(self.sources["A"], 2)
        self.assertTrue(wait_until(lambda: window._latest_view.streams[0].persisted_samples >= 2))
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view.recording_state == "partial"))
        self.assertEqual(window._latest_view.streams[1].read_failures, 0)
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_stopping_one_stream_does_not_stop_its_recording_sibling(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        source_a, source_b = self.sources["A"], self.sources["B"]
        self.emit_samples(source_a, 2)
        self.emit_samples(source_b, 2)
        window.stop_a_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].capture_state == "stopped"))
        self.assertEqual(source_b.snapshot().state, CaptureState.RUNNING)
        sibling_count = source_b.snapshot().frame_count
        source_b.emit_frame(time.monotonic_ns())
        self.assertEqual(source_b.snapshot().frame_count, sibling_count + 1)
        self.assertTrue(wait_until(lambda: window._latest_view.streams[1].persisted_samples >= 3))
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view.recording_state == "partial"))
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_unexpected_mid_recording_capture_failure_keeps_sibling_recording(self) -> None:
        window = self.make_window(auto_emit=True, fail_after_frames={"A": 12})
        window.acknowledgement.setChecked(True)
        window.start_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and all(item.capture_state == "running" for item in window._latest_view.streams)))
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].capture_state == "failed"))
        self.assertEqual(window._latest_view.streams[0].read_failures, 1)
        self.assertEqual(window._latest_view.streams[1].capture_state, "running")
        prior = self.sources["B"].snapshot().frame_count
        self.assertTrue(wait_until(lambda: self.sources["B"].snapshot().frame_count > prior))
        window.stop_recording_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view.recording_state == "partial"))
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_natural_completion_after_source_failure_stops_sibling_but_keeps_partial_status(self) -> None:
        window = self.make_window(auto_emit=True, fail_after_frames={"A": 12})
        self.acknowledge_and_start(window)
        window.duration.setCurrentIndex(0)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].capture_state == "failed"))
        self.assertEqual(window._latest_view.streams[1].capture_state, "running")
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and not window._latest_view.automatic_capture_stop_pending
                                   and window._latest_view.recording_state in {"partial", "failed"}
                                   and window._latest_view.streams[1].capture_state == "stopped", 4.0))
        self.assertNotEqual(window._latest_view.recording_state, "completed")
        self.assertIn("finalized as", window.capture_status_label.text().lower())
        self.assertIn("stopped automatically", window.capture_status_label.text().lower())
        self.assertEqual(self.sources["A"].snapshot().state, CaptureState.FAILED)
        self.assertEqual(self.sources["B"].snapshot().state, CaptureState.STOPPED)
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_close_during_startup_is_deferred_until_worker_teardown(self) -> None:
        window = self.make_window(auto_emit=True)
        window.acknowledgement.setChecked(True)
        window.start_both_button.click()
        window.close()
        self.assertFalse(window._close_complete)
        self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertFalse(window._thread.isRunning())

    def test_close_during_recording_initialization_cancels_before_group_creation(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        entered = threading.Event()
        release = threading.Event()
        from camera_exploration_lab.ui import synthetic_recording_harness as harness_module
        original_migration = harness_module.migrate_synthetic_database_to_v3

        def gated_migration(database):
            entered.set()
            if not release.wait(2.0):
                raise TimeoutError("test migration gate timed out")
            return original_migration(database)

        with patch.object(harness_module, "migrate_synthetic_database_to_v3", side_effect=gated_migration):
            window.record_both_button.click()
            self.assertTrue(entered.wait(2.0))
            window.close()
            release.set()
            self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertFalse(window._thread.isRunning())
        self.assertIsNone(window._latest_view.session_id)

    def test_stop_both_queued_during_recorder_startup_finalizes_before_stopping_sources(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        entered = threading.Event()
        release = threading.Event()
        original_factory = GroupedRecordingCoordinator.create_and_start

        def gated_factory(*args, **kwargs):
            entered.set()
            if not release.wait(2.0):
                raise TimeoutError("test coordinator gate timed out")
            return original_factory(*args, **kwargs)

        with patch.object(GroupedRecordingCoordinator, "create_and_start", side_effect=gated_factory):
            window.record_both_button.click()
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(window._latest_view.recording_state.startswith("Recording"))
            self.assertIn("requested", window.state_label.text().lower())
            window.request_stop_both.emit()
            release.set()
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and all(item.capture_state == "stopped" for item in window._latest_view.streams)))
            self.assertEqual(window._latest_view.recording_state, "failed")
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_responsive_layout_keeps_all_stream_metrics_reachable_at_desktop_sizes(self) -> None:
        window = self.make_window()
        for width in (1024, 1366, 1920):
            window.resize(width, 768)
            APP.processEvents()
            self.assertEqual(window.size().width(), width)
            self.assertEqual(window.size().height(), 768)
            self.assertTrue(window.keep_capture_running_checkbox.isVisible())
            self.assertGreater(window.keep_capture_running_checkbox.width(), 0)
            self.assertEqual(window.splitter.orientation(), Qt.Orientation.Horizontal)
            for panel in window._stream_panels:
                scroll = panel.metrics_scroll_area
                self.assertGreater(scroll.viewport().width(), 0)
                self.assertGreater(scroll.viewport().height(), 0)
                for label in (*panel.metric_title_labels, *panel.metric_labels.values()):
                    scroll.ensureWidgetVisible(label)
                    APP.processEvents()
                    top_left = label.mapTo(scroll.viewport(), QPoint(0, 0))
                    self.assertGreaterEqual(top_left.y(), 0)
                    self.assertLessEqual(top_left.y() + label.height(), scroll.viewport().height())
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_status_labels_follow_dark_and_light_qt_palettes(self) -> None:
        window = self.make_window()
        for dark in (True, False):
            palette = QPalette()
            background = QColor("#20242a" if dark else "#ffffff")
            foreground = QColor("#f3f5f7" if dark else "#17191c")
            for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
                palette.setColor(role, background)
            for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
                palette.setColor(role, foreground)
            window.setPalette(palette)
            APP.processEvents()
            for label in (
                window.state_label, window.capture_status_label, window.group_id_label,
                window._stream_panels[0].state_label,
                *window._stream_panels[0].metric_title_labels,
                *window._stream_panels[0].metric_labels.values(),
            ):
                self.assertIn("palette(text)", label.styleSheet())
            self.assertGreaterEqual(
                contrast_ratio_for_test(foreground, background), 4.5
            )
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_per_frame_events_do_not_emit_per_frame_qt_signals(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        snapshots = QSignalSpy(window._worker.snapshot_ready)
        before = snapshots.count()
        self.emit_samples(self.sources["A"], 200)
        self.emit_samples(self.sources["B"], 48)
        self.assertEqual(snapshots.count(), before)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples >= 200
                                   and window._latest_view.streams[1].persisted_samples >= 48))
        self.assertLess(snapshots.count() - before, 10)
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_duplicate_record_request_cannot_create_a_second_group(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.request_record_both.emit(5.0, False)
        window.request_record_both.emit(5.0, False)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.session_id is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        session_id = window._latest_view.session_id
        self.assertEqual(window._latest_view.session_id, session_id)
        self.assertTrue(wait_until(lambda: "already active" in window.state_label.text().lower()))
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))

    def test_close_during_recording_cancels_as_interrupted_and_preserves_inspectable_counts(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.emit_samples(self.sources["A"], 1)
        self.emit_samples(self.sources["B"], 2)
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.streams[0].persisted_samples == 1
                                   and window._latest_view.streams[1].persisted_samples == 2))
        database_path = window._worker._database.path
        with patch.object(window._worker, "_remove_synthetic_database", lambda: None):
            window.close()
            self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertFalse(window._thread.isRunning())
        self.assertTrue(all(not (source._thread and source._thread.is_alive()) for source in self.sources.values()))
        self.assertIsNotNone(window._worker._final_record)
        self.assertEqual(str(window._worker._final_record.status), "interrupted")
        self.assertEqual(window._worker._final_record.streams[0].capture_frame_count, 1)
        self.assertEqual(window._worker._final_record.streams[1].capture_frame_count, 2)
        try:
            read_only_database = Database(database_path)
            with read_only_database.connect_readonly() as connection:
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                counts = connection.execute(
                    """SELECT bs.slot, COUNT(m.id)
                       FROM benchmark_streams bs
                       LEFT JOIN measurements m ON m.benchmark_id=bs.benchmark_id
                           AND m.stream_scope='capture'
                           AND m.metric IN ('frame_arrival', 'read_failure')
                       WHERE bs.session_id=? GROUP BY bs.slot ORDER BY bs.slot""",
                    (window._worker._session_id,),
                ).fetchall()
                self.assertEqual([(row[0], row[1]) for row in counts], [("A", 1), ("B", 2)])
                self.assertEqual(connection.execute(
                    "SELECT status FROM group_recordings WHERE session_id=?",
                    (window._worker._session_id,),
                ).fetchone()[0], "interrupted")
        finally:
            shutil.rmtree(database_path.parent, ignore_errors=True)

    def test_writer_finalization_error_never_displays_completed(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        self.emit_samples(self.sources["A"], 1)
        self.emit_samples(self.sources["B"], 1)
        with patch("camera_exploration_lab.ui.synthetic_recording_harness.GroupedCaptureRecordingBridge.stop_recording",
                   side_effect=GroupedRecordingPersistenceError("synthetic writer failure")):
            window.stop_recording_button.click()
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and "unconfirmed" in window._latest_view.operation.lower()))
            self.assertNotEqual(window._latest_view.recording_state, "completed")
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete, 4.0))

    def test_slow_writer_keeps_callbacks_nonblocking_and_reports_bounded_queue_loss(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))

        entered = threading.Event()
        release = threading.Event()
        original = GroupedExperimentRepository.record_stream_measurements

        def blocked_write(repository, session_id, slot, samples, *, owner_token):
            entered.set()
            if not release.wait(5.0):
                raise TimeoutError("synthetic writer gate timed out")
            return original(repository, session_id, slot, samples, owner_token=owner_token)

        with patch.object(
            GroupedExperimentRepository, "record_stream_measurements", autospec=True, side_effect=blocked_write
        ):
            try:
                self.sources["A"].emit_frame(time.monotonic_ns())
                self.assertTrue(entered.wait(2.0), "writer did not enter its controlled slow-write gate")
                self.emit_samples(self.sources["A"], 2200)
                self.emit_samples(self.sources["B"], 2200)
                self.assertTrue(wait_until(lambda: window._latest_view is not None and any(
                    stream.queue_rejections > 0 for stream in window._latest_view.streams
                )))
                view = window._latest_view
                self.assertEqual(view.recording_state, "Recording — timing data incomplete")
                self.assertTrue(all(stream.queue_depth <= stream.queue_capacity for stream in view.streams))
                self.assertTrue(all(stream.queue_capacity == 2048 for stream in view.streams))
                self.assertGreater(sum(stream.queue_rejections for stream in view.streams), 0)
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: window._latest_view is not None and any(
                stream.queue_depth < stream.queue_capacity for stream in window._latest_view.streams
            )))
            self.sources["A"].emit_frame(time.monotonic_ns())
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and window._latest_view.streams[0].accepted_samples > 2048))
            window.stop_recording_button.click()
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and window._latest_view.recording_state == "partial", 8.0))
            self.assertTrue(any(stream.queue_rejections > 0 for stream in window._latest_view.streams))
            self.assertNotEqual(window._latest_view.recording_state, "completed")
            database = window._worker._database
            session_id = window._latest_view.session_id
            self.assertIsNotNone(database)
            with database.connect_readonly() as connection:
                rows = connection.execute(
                    """SELECT bs.slot, COUNT(m.id)
                       FROM benchmark_streams bs
                       LEFT JOIN measurements m ON m.benchmark_id=bs.benchmark_id
                           AND m.stream_scope='capture'
                           AND m.metric IN ('frame_arrival', 'read_failure')
                       WHERE bs.session_id=? GROUP BY bs.slot ORDER BY bs.slot""",
                    (session_id,),
                ).fetchall()
            self.assertEqual(
                tuple(int(row[1]) for row in rows),
                tuple(stream.persisted_samples for stream in window._latest_view.streams),
            )
            inspection_results = []
            window._worker.action_finished.connect(
                lambda action, result: inspection_results.append(result) if action == "inspect" else None
            )
            window.inspect_button.click()
            self.assertTrue(wait_until(lambda: bool(inspection_results)))
            self.assertEqual(
                tuple(item["persisted_samples"] for item in inspection_results[-1]["streams"]),
                tuple(int(row[1]) for row in rows),
            )
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete, 4.0))

    def test_sqlite_measurement_failure_is_reported_without_false_completion(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))

        def failed_write(repository, session_id, slot, samples, *, owner_token):
            raise sqlite3.OperationalError("synthetic disk-full failure")

        with patch.object(
            GroupedExperimentRepository, "record_stream_measurements", autospec=True, side_effect=failed_write
        ):
            self.emit_samples(self.sources["A"], 2)
            self.assertTrue(wait_until(lambda: window._latest_view is not None
                                       and "unconfirmed" in window._latest_view.recording_state.lower()))
            self.assertNotEqual(window._latest_view.recording_state, "completed")
            self.assertTrue(all(source.snapshot().state is CaptureState.RUNNING for source in self.sources.values()))
            self.assertIn("not reported as saved", window.capture_status_label.text().lower())
            with patch("camera_exploration_lab.ui.synthetic_recording_harness.QMessageBox.warning"):
                window.close()
                self.assertTrue(wait_until(lambda: window._close_complete, 4.0))
        self.assertFalse(window._thread.isRunning())

    def test_close_during_slow_writer_reports_unresolved_then_finishes_after_drain(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        window.record_both_button.click()
        self.assertTrue(wait_until(lambda: window._latest_view is not None
                                   and window._latest_view.recording_state.startswith("Recording")))
        entered = threading.Event()
        release = threading.Event()
        original = GroupedExperimentRepository.record_stream_measurements

        def blocked_write(repository, session_id, slot, samples, *, owner_token):
            entered.set()
            if not release.wait(5.0):
                raise TimeoutError("synthetic shutdown writer gate timed out")
            return original(repository, session_id, slot, samples, owner_token=owner_token)

        with patch.object(
            GroupedExperimentRepository, "record_stream_measurements", autospec=True, side_effect=blocked_write
        ):
            with patch("camera_exploration_lab.ui.synthetic_recording_harness.SHUTDOWN_UNRESOLVED_WARNING_SECONDS", 0.05):
                try:
                    self.sources["A"].emit_frame(time.monotonic_ns())
                    self.assertTrue(entered.wait(2.0), "writer did not acknowledge the blocked transaction")
                    window.close()
                    self.assertTrue(wait_until(
                        lambda: "deadline exceeded" in window.state_label.text().lower(),
                        2.5,
                    ))
                    self.assertFalse(window._close_complete)
                    self.assertTrue(self.sources["A"]._stop.is_set() or self.sources["B"]._stop.is_set())
                finally:
                    # Always release the injected writer, including on an
                    # assertion failure, so unittest teardown cannot strand
                    # its non-daemon worker behind the test fixture's own gate.
                    release.set()

                self.assertTrue(wait_until(lambda: window._close_complete, 5.0))
                self.assertIsNotNone(window._worker._final_record)
        self.assertFalse(window._thread.isRunning())

    def test_lease_acquisition_failure_does_not_create_or_claim_a_recording(self) -> None:
        window = self.make_window()
        self.acknowledge_and_start(window)
        with patch.object(
            GroupedRecordingCoordinator,
            "create_and_start",
            side_effect=RecordingLeaseBusyError("synthetic owner contention"),
        ):
            window.record_both_button.click()
            self.assertTrue(wait_until(lambda: "startup failed" in window._latest_view.operation.lower()
                                       or "failed" in window.state_label.text().lower()))
            self.assertFalse(window._latest_view.session_id)
            self.assertNotEqual(window._latest_view.recording_state, "completed")
        window.close()
        self.assertTrue(wait_until(lambda: window._close_complete))


if __name__ == "__main__":
    unittest.main(verbosity=2)
