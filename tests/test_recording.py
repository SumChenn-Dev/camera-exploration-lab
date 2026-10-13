from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from camera_exploration_lab.capture import CaptureTimingEvent, NegotiatedSettings
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.db import Database
from camera_exploration_lab.models import BackendKind, CameraIdentity
from camera_exploration_lab.recording import ExperimentRecorder, RecordingState


class FakeClock:
    def __init__(self) -> None:
        self.start_ns = 50_000_000_000
        self.monotonic_ns = self.start_ns
        self.start_utc = datetime(2026, 10, 13, 15, 30, tzinfo=timezone.utc)
        self.utc = self.start_utc

    def monotonic(self) -> int:
        return self.monotonic_ns

    def now_iso(self) -> str:
        return self.utc.isoformat(timespec="microseconds")

    def elapsed(self, seconds: float) -> int:
        return self.start_ns + int(seconds * 1_000_000_000)

    def observed_at(self, seconds: float) -> str:
        return (self.start_utc + timedelta(seconds=seconds)).isoformat(timespec="microseconds")

    def advance(self, seconds: float) -> None:
        self.monotonic_ns = self.elapsed(seconds)
        self.utc = self.start_utc + timedelta(seconds=seconds)


class SlowDatabase(Database):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.write_entered = threading.Event()
        self.release_write = threading.Event()

    def record_measurements_batch(self, samples):
        self.write_entered.set()
        self.release_write.wait(timeout=2)
        return super().record_measurements_batch(samples)


def camera_identity() -> CameraIdentity:
    return CameraIdentity(
        stable_identifier="/dev/v4l/by-id/usb-Synthetic_Eye-video-index0",
        backend=BackendKind.V4L2,
        device_path="/dev/video9",
        manufacturer="Synthetic",
        model="Mock PS3 Eye",
        vendor_id="1415",
        product_id="2000",
        identity_source="v4l_by_id",
    )


def event(clock: FakeClock, kind: str, seconds: float, *, interval_ns=None, fps=50.0):
    return CaptureTimingEvent(
        kind=kind,
        monotonic_ns=clock.elapsed(seconds),
        observed_at=clock.observed_at(seconds),
        interval_ns=interval_ns,
        capture_fps=fps,
    )


class ExperimentRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "recording.sqlite3")
        self.database.initialize()
        self.clock = FakeClock()
        self.mode = CaptureMode("240p_187", 320, 240, 187, "GRBG")
        self.negotiated = NegotiatedSettings(320, 240, 187.0, "GRBG")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def recorder(self, **changes) -> ExperimentRecorder:
        arguments = {
            "database": self.database,
            "camera": camera_identity(),
            "mode": self.mode,
            "negotiated": self.negotiated,
            "name": "Desk lamp PS Eye",
            "notes": "Fixed manual lens position; two desk lamps.",
            "duration_seconds": None,
            "clock_ns": self.clock.monotonic,
            "now_iso": self.clock.now_iso,
            "batch_size": 16,
        }
        arguments.update(changes)
        return ExperimentRecorder(**arguments)

    def test_manual_recording_streams_raw_timing_and_saves_summary_separately_by_scope(self) -> None:
        recorder = self.recorder()
        benchmark_id = recorder.snapshot().benchmark_id
        for index in range(101):
            seconds = index * 0.02
            # The first event can carry a pre-recording interval because the
            # camera may already be running for preview when recording starts.
            interval = 20_000_000
            recorder.record_capture_event(event(self.clock, "frame_arrival", seconds, interval_ns=interval))
            if index == 50:
                recorder.record_capture_event(event(self.clock, "read_failure", 1.005))
        recorder.record_preview_fps(20.0, monotonic_ns=self.clock.elapsed(1), observed_at=self.clock.observed_at(1))
        recorder.record_preview_fps(30.0, monotonic_ns=self.clock.elapsed(2), observed_at=self.clock.observed_at(2))
        self.clock.advance(2.1)

        snapshot = recorder.stop("manual_stop")

        self.assertEqual(snapshot.state, RecordingState.INTERRUPTED)
        self.assertEqual(snapshot.reason, "manual_stop")
        self.assertEqual(snapshot.frame_count, 101)
        self.assertEqual(snapshot.read_failures, 1)
        self.assertAlmostEqual(snapshot.elapsed_seconds, 2.1)
        self.assertEqual(snapshot.summary["frame_count"], 101)
        self.assertAlmostEqual(snapshot.summary["capture_fps"], 50.0)
        self.assertAlmostEqual(snapshot.summary["preview_fps"], 25.0)
        self.assertEqual(snapshot.summary["read_failures"], 1)
        self.assertEqual(snapshot.summary["interval_count"], 100)
        self.assertAlmostEqual(snapshot.summary["median_interval_ms"], 20.0)
        self.assertAlmostEqual(snapshot.summary["p95_interval_ms"], 20.0)
        self.assertAlmostEqual(snapshot.summary["p99_interval_ms"], 20.0)

        with self.database.connect() as connection:
            run = connection.execute(
                """SELECT b.status, s.label, s.notes, s.started_at, s.ended_at,
                          b.configuration_json
                   FROM benchmark_runs b JOIN sessions s ON s.id=b.session_id WHERE b.id=?""",
                (benchmark_id,),
            ).fetchone()
            settings = connection.execute(
                """SELECT setting_key, requested_value_json, reported_value_json, verified_value_json
                   FROM setting_snapshots WHERE benchmark_id=? ORDER BY setting_key""",
                (benchmark_id,),
            ).fetchall()
            streams = connection.execute(
                "SELECT metric, stream_scope, COUNT(*) AS n FROM measurements WHERE benchmark_id=? "
                "GROUP BY metric, stream_scope ORDER BY metric, stream_scope",
                (benchmark_id,),
            ).fetchall()
            timing = connection.execute(
                "SELECT observed_at, elapsed_seconds, interval_ns FROM measurements "
                "WHERE benchmark_id=? AND metric='frame_arrival' ORDER BY sample_index LIMIT 2",
                (benchmark_id,),
            ).fetchall()

        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["label"], "Desk lamp PS Eye")
        self.assertEqual(run["notes"], "Fixed manual lens position; two desk lamps.")
        self.assertEqual(run["started_at"], self.clock.start_utc.isoformat(timespec="microseconds"))
        self.assertEqual(run["ended_at"], self.clock.now_iso())
        self.assertIn('"duration_seconds":null', run["configuration_json"])
        settings_by_key = {row["setting_key"]: row for row in settings}
        self.assertEqual(settings_by_key["fps"]["requested_value_json"], "187")
        self.assertEqual(settings_by_key["fps"]["reported_value_json"], "187.0")
        self.assertIsNone(settings_by_key["fps"]["verified_value_json"])
        self.assertIsNone(timing[0]["interval_ns"])
        self.assertEqual(timing[1]["interval_ns"], 20_000_000)
        self.assertEqual(timing[1]["elapsed_seconds"], 0.02)
        self.assertEqual(
            {(row["metric"], row["stream_scope"]) for row in streams},
            {
                ("capture_fps", "capture"),
                ("frame_arrival", "capture"),
                ("frame_interval_ms", "capture"),
                ("preview_fps", "preview"),
                ("read_failure", "capture"),
            },
        )

    def test_fixed_duration_completes_automatically_and_ignores_events_after_deadline(self) -> None:
        recorder = self.recorder(duration_seconds=10)
        recorder.record_capture_event(event(self.clock, "frame_arrival", 0.1))
        self.clock.advance(10)
        recorder.record_capture_event(event(self.clock, "frame_arrival", 10.0))

        deadline = time.monotonic() + 1
        while recorder.snapshot().state == RecordingState.RECORDING and time.monotonic() < deadline:
            time.sleep(0.01)

        snapshot = recorder.snapshot()
        self.assertEqual(snapshot.state, RecordingState.COMPLETED)
        self.assertEqual(snapshot.reason, "duration_elapsed")
        self.assertEqual(snapshot.frame_count, 1)
        self.assertEqual(snapshot.elapsed_seconds, 10.0)
        self.assertEqual(snapshot.summary["duration_seconds"], 10.0)

    def test_camera_disconnect_stops_as_interrupted_while_capture_callback_remains_nonblocking(self) -> None:
        recorder = self.recorder(queue_capacity=4)
        recorder.record_capture_event(event(self.clock, "frame_arrival", 0.01))
        start = time.monotonic()
        recorder.record_capture_event(event(self.clock, "read_failure", 0.02))
        self.assertLess(time.monotonic() - start, 0.1)

        snapshot = recorder.stop("camera_disconnected")

        self.assertEqual(snapshot.state, RecordingState.INTERRUPTED)
        self.assertEqual(snapshot.reason, "camera_disconnected")
        self.assertEqual(snapshot.read_failures, 1)
        history = self.database.history_entries()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["session_label"], "Desk lamp PS Eye")
        self.assertEqual(history[0]["status"], "interrupted")

    def test_full_timing_queue_interrupts_recording_without_blocking_capture(self) -> None:
        slow_db = SlowDatabase(Path(self.tempdir.name) / "slow.sqlite3")
        slow_db.initialize()
        recorder = ExperimentRecorder(
            slow_db, camera_identity(), self.mode, self.negotiated,
            name="Queue limit", notes="", duration_seconds=None,
            clock_ns=self.clock.monotonic, now_iso=self.clock.now_iso,
            queue_capacity=1, batch_size=1,
        )
        recorder.record_capture_event(event(self.clock, "frame_arrival", 0.01))
        self.assertTrue(slow_db.write_entered.wait(1))
        recorder.record_capture_event(event(self.clock, "frame_arrival", 0.02))
        start = time.monotonic()
        recorder.record_capture_event(event(self.clock, "frame_arrival", 0.03))
        self.assertLess(time.monotonic() - start, 0.1)
        slow_db.release_write.set()
        snapshot = recorder.stop("manual_stop")
        self.assertEqual(snapshot.state, RecordingState.INTERRUPTED)
        self.assertEqual(snapshot.reason, "timing_queue_overflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
