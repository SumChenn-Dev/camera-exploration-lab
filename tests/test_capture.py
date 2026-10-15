from __future__ import annotations

import time
import unittest

from camera_exploration_lab.capture import (
    CaptureState,
    CaptureTimingEvent,
    LatestFrameBuffer,
    OpenCVCaptureWorker,
    RollingFrameRate,
    preferred_device_path,
)
from camera_exploration_lab.capture_modes import (
    AVERMEDIA_CAM_313_MODES,
    PS3_EYE_MODES,
    CaptureMode,
    modes_for_camera,
)
from camera_exploration_lab.models import BackendKind, CameraCandidate, CameraIdentity


class SyntheticFrame:
    shape = (240, 320, 3)


class FakeCapture:
    def __init__(self, *, opened: bool = True, negotiated: dict[int, float] | None = None,
                 reads: list[tuple[bool, object]] | None = None, delay: float = 0.001) -> None:
        self.opened = opened
        self.properties = dict(negotiated or {})
        self.reads = list(reads) if reads is not None else [(True, SyntheticFrame())]
        self.delay = delay
        self.released = False
        self.set_calls: list[tuple[int, float]] = []
        self._read_index = 0

    def isOpened(self) -> bool:
        return self.opened

    def set(self, prop: int, value: float) -> bool:
        self.set_calls.append((prop, value))
        return True

    def get(self, prop: int) -> float:
        return self.properties.get(prop, 0.0)

    def read(self) -> tuple[bool, object]:
        if self.delay:
            time.sleep(self.delay)
        index = min(self._read_index, len(self.reads) - 1)
        self._read_index += 1
        return self.reads[index]

    def release(self) -> None:
        self.released = True


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def candidate(vendor: str, product: str, stable: str = "/dev/video7",
              source: str = "sysfs_usb_identity") -> CameraCandidate:
    return CameraCandidate(
        identity=CameraIdentity(
            stable_identifier=stable,
            backend=BackendKind.V4L2,
            device_path="/dev/video7",
            vendor_id=vendor,
            product_id=product,
            identity_source=source,
        ),
        display_name="Synthetic camera",
    )


class CaptureModeTests(unittest.TestCase):
    def test_recognized_camera_profiles_match_the_existing_benchmarks(self) -> None:
        self.assertEqual([(mode.width, mode.height, mode.fps) for mode in modes_for_camera(candidate("1415", "2000"))],
                         [(640, 480, 60), (320, 240, 125), (320, 240, 187)])
        self.assertEqual(modes_for_camera(candidate("1415", "2000")), PS3_EYE_MODES)
        self.assertEqual(len(modes_for_camera(candidate("07ca", "313a"))), len(AVERMEDIA_CAM_313_MODES))
        self.assertTrue(all(mode.pixel_format == "MJPG" for mode in AVERMEDIA_CAM_313_MODES[:4]))

    def test_unknown_camera_gets_a_conservative_mode(self) -> None:
        mode = modes_for_camera(candidate("ffff", "0001"))[0]
        self.assertEqual((mode.width, mode.height, mode.fps), (640, 480, 30))
        self.assertIsNone(mode.pixel_format)

    def test_by_id_alias_is_preferred_for_opening(self) -> None:
        camera = candidate("1415", "2000", "/dev/v4l/by-id/usb-eye-video-index0", "v4l_by_id")
        self.assertEqual(preferred_device_path(camera), camera.identity.stable_identifier)
        self.assertEqual(preferred_device_path(candidate("1415", "2000")), "/dev/video7")


class LatestFrameBufferTests(unittest.TestCase):
    def test_new_frame_overwrites_old_without_creating_a_queue(self) -> None:
        frames = LatestFrameBuffer()
        first = object()
        newest = object()
        frames.publish(first, 100)
        frames.publish(newest, 200)
        latest = frames.read_after(0)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest[0], newest)
        self.assertEqual(latest[1:], (2, 200))
        self.assertIsNone(frames.read_after(2))

    def test_capture_and_preview_rates_use_independent_timestamps(self) -> None:
        capture_rate = RollingFrameRate()
        preview_rate = RollingFrameRate()
        capture_fps = 0.0
        preview_fps = 0.0
        for index in range(201):
            capture_fps = capture_rate.add(index * 5_000_000)  # 200 FPS
        for index in range(31):
            preview_fps = preview_rate.add(index * 33_333_333)  # about 30 FPS
        self.assertAlmostEqual(capture_fps, 200.0, delta=0.1)
        self.assertAlmostEqual(preview_fps, 30.0, delta=0.1)
        self.assertGreater(capture_fps, preview_fps * 6)
        self.assertEqual(preview_rate.value_at(2_200_000_000), 0.0)


class OpenCVCaptureWorkerTests(unittest.TestCase):
    def test_timing_listener_receives_only_bounded_timestamp_metadata(self) -> None:
        fake = FakeCapture(delay=0.001)
        events: list[CaptureTimingEvent] = []
        worker = OpenCVCaptureWorker(
            "/dev/video7", CaptureMode("test", 640, 480, 60),
            capture_factory=lambda _device: fake,
        )
        worker.set_timing_listener(events.append)
        worker.start()
        self.assertTrue(wait_until(lambda: len(events) >= 4))
        worker.set_timing_listener(None)
        worker.request_stop()
        worker.join(timeout=1)

        self.assertTrue(all(isinstance(event, CaptureTimingEvent) for event in events))
        self.assertTrue(all(not hasattr(event, "frame") for event in events))
        arrivals = [event for event in events if event.kind == "frame_arrival"]
        self.assertGreaterEqual(len(arrivals), 2)
        self.assertIsNotNone(arrivals[1].interval_ns)
        self.assertGreater(arrivals[1].capture_fps, 0)

    def test_capture_runs_ahead_of_the_single_preview_slot_and_reports_readback(self) -> None:
        # Driver readback deliberately differs from the request.
        properties = {
            3: 640.0,
            4: 480.0,
            5: 30.0,
            6: float(int.from_bytes(b"MJPG", "little")),
        }
        fake = FakeCapture(negotiated=properties, delay=0.001)
        worker = OpenCVCaptureWorker(
            "/dev/v4l/by-id/mock-camera",
            CaptureMode("requested-fast", 320, 240, 187, "GRBG"),
            capture_factory=lambda _device: fake,
        )
        worker.start()
        self.assertTrue(wait_until(
            lambda: worker.snapshot().frame_count >= 8 and worker.snapshot().capture_fps > 0
        ))
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.state, CaptureState.RUNNING)
        self.assertGreater(snapshot.capture_fps, 0)
        self.assertEqual(snapshot.requested.width, 320)
        self.assertEqual(snapshot.negotiated.width, 640)
        self.assertEqual(snapshot.negotiated.pixel_format, "MJPG")
        self.assertEqual(snapshot.observed_frame_size, (320, 240))
        # Simulate a slow UI: it reads only after many capture frames have arrived.
        latest = worker.frame_buffer.read_after(0)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertGreater(latest[1], 1)
        worker.request_stop()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(fake.released)
        self.assertEqual(worker.snapshot().state, CaptureState.STOPPED)
        self.assertEqual(worker.snapshot().capture_fps, 0.0)
        self.assertIsNone(worker.snapshot().frame_interval_ms)

    def test_driver_default_mode_does_not_claim_or_set_a_requested_rate(self) -> None:
        fake = FakeCapture(delay=0.001)
        worker = OpenCVCaptureWorker(
            "/dev/video7", CaptureMode("default", 640, 480, None, "MJPG"),
            capture_factory=lambda _device: fake,
        )
        worker.start()
        self.assertTrue(wait_until(lambda: worker.snapshot().state == CaptureState.RUNNING))
        worker.request_stop()
        worker.join(timeout=1)
        self.assertTrue(worker.is_alive() is False)
        # OpenCV property 5 is FPS; other mode requests remain independent.
        self.assertNotIn(5, [prop for prop, _value in fake.set_calls])

    def test_stop_requested_during_slow_initialization_finishes_as_stopped(self) -> None:
        import threading

        entered = threading.Event()
        release_startup = threading.Event()
        fake = FakeCapture(delay=0.001)

        def slow_factory(_device: str) -> FakeCapture:
            entered.set()
            release_startup.wait(timeout=1)
            return fake

        worker = OpenCVCaptureWorker(
            "/dev/video7", CaptureMode("slow-open", 320, 240, 60),
            capture_factory=slow_factory,
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        worker.request_stop()
        release_startup.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.snapshot().state, CaptureState.STOPPED)
        self.assertTrue(fake.released)

    def test_failed_initialization_is_reported_and_released(self) -> None:
        fake = FakeCapture(opened=False)
        worker = OpenCVCaptureWorker("/dev/video7", CaptureMode("test", 640, 480, 30),
                                     capture_factory=lambda _device: fake)
        worker.start()
        worker.join(timeout=1)
        self.assertEqual(worker.snapshot().state, CaptureState.FAILED)
        self.assertIn("could not be opened", worker.snapshot().message)
        self.assertTrue(fake.released)

    def test_repeated_read_failure_marks_camera_unavailable(self) -> None:
        fake = FakeCapture(reads=[(False, None)], delay=0)
        worker = OpenCVCaptureWorker("/dev/video7", CaptureMode("test", 640, 480, 30),
                                     capture_factory=lambda _device: fake, read_failure_limit=3)
        worker.start()
        worker.join(timeout=1)
        self.assertEqual(worker.snapshot().state, CaptureState.UNAVAILABLE)
        self.assertEqual(worker.snapshot().read_failures, 3)
        self.assertEqual(worker.snapshot().capture_fps, 0.0)
        self.assertIsNone(worker.snapshot().frame_interval_ms)
        self.assertTrue(fake.released)


if __name__ == "__main__":
    unittest.main(verbosity=2)
