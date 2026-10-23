from __future__ import annotations

import math
import time
import unittest

from camera_exploration_lab.capture import OpenCVCaptureWorker
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.performance import (
    Availability,
    BoundedTelemetryBuffer,
    ClockDomain,
    Metric,
    MeasurementKind,
    TelemetrySample,
    reading,
)


def sample(index: int, *, fps: float = 30.0) -> TelemetrySample:
    return TelemetrySample(
        timestamp_ns=index * 1_000_000,
        source="synthetic unit-test source",
        clock_domain=ClockDomain.HOST_MONOTONIC,
        sequence=index,
        readings=(
            reading(Metric.OPENCV_CAPTURE_FPS, fps, kind=MeasurementKind.DERIVED,
                    provenance="Synthetic host read-return events."),
            reading(Metric.GUI_PRESENTATION_FPS, None, availability=Availability.UNAVAILABLE,
                    provenance="No display presentation source in this test."),
        ),
    )


class MetricContractTests(unittest.TestCase):
    def test_unavailable_and_not_recorded_are_explicit_and_value_free(self) -> None:
        unavailable = reading(Metric.SENSOR_EXPOSURE_FPS, None,
                              availability=Availability.UNAVAILABLE,
                              provenance="Sensor timestamps are not supplied.")
        absent = reading(Metric.CHDKPTP_FETCH_FPS, None,
                         availability=Availability.NOT_RECORDED,
                         provenance="The source CSV did not record this field.")
        self.assertIsNone(unavailable.value)
        self.assertIsNone(absent.value)
        self.assertNotEqual(unavailable.availability, absent.availability)

    def test_metric_separation_uses_distinct_names_and_units(self) -> None:
        capture = reading(Metric.OPENCV_CAPTURE_FPS, 99.9,
                          provenance="Successful OpenCV read returns.")
        fetch = reading(Metric.CHDKPTP_FETCH_FPS, 31.64,
                        provenance="Completed CHDKPTP payload fetches.")
        changes = reading(Metric.CONTENT_CHANGE_RATE, 28.0,
                          kind=MeasurementKind.ESTIMATED,
                          provenance="Published threshold-classified count.")
        preview = reading(Metric.GUI_PREVIEW_UPDATE_FPS, 30.0,
                          provenance="CEL GUI preview updates.")
        self.assertEqual(len({capture.metric, fetch.metric, changes.metric, preview.metric}), 4)
        self.assertNotEqual(capture.unit, fetch.unit)
        self.assertEqual(changes.kind, MeasurementKind.ESTIMATED)

    def test_invalid_numbers_units_and_availability_combinations_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            reading(Metric.OPENCV_CAPTURE_FPS, math.nan,
                    provenance="invalid non-finite test value")
        with self.assertRaises(ValueError):
            reading(Metric.OPENCV_CAPTURE_FPS, -1.0,
                    provenance="invalid negative rate test value")
        with self.assertRaises(ValueError):
            reading(Metric.OPENCV_CAPTURE_FPS, None,
                    availability=Availability.AVAILABLE,
                    provenance="available without a value")
        with self.assertRaises(ValueError):
            from camera_exploration_lab.performance import Measurement
            Measurement(Metric.OPENCV_CAPTURE_FPS, 30.0, "USB latency", Availability.AVAILABLE,
                        MeasurementKind.OBSERVED, "wrong unit")


class BoundedTelemetryBufferTests(unittest.TestCase):
    def test_capacity_overwrites_oldest_and_reports_cursor_gap(self) -> None:
        buffer = BoundedTelemetryBuffer(capacity=2)
        for index in range(3):
            buffer.append(sample(index))
        self.assertEqual(buffer.overwritten_count, 1)
        self.assertEqual([item.sequence for item in buffer.snapshot()], [1, 2])
        read = buffer.read_since(-1)
        self.assertEqual(read.missed_entries, 1)
        self.assertEqual([entry[0] for entry in read.entries], [1, 2])
        self.assertEqual(read.next_cursor, 2)
        self.assertEqual(buffer.read_since(read.next_cursor).entries, ())

    def test_reader_catches_up_without_an_unbounded_pending_queue(self) -> None:
        buffer = BoundedTelemetryBuffer(capacity=4)
        cursor = -1
        for index in range(20):
            buffer.append(sample(index))
            result = buffer.read_since(cursor)
            cursor = result.next_cursor
            self.assertLessEqual(len(result.entries), 1)
            self.assertLessEqual(len(buffer.snapshot()), 4)
        self.assertEqual(buffer.overwritten_count, 16)


class CaptureTelemetryCoexistenceTests(unittest.TestCase):
    def test_capture_telemetry_does_not_displace_exclusive_recorder_listener(self) -> None:
        class FakeCapture:
            def isOpened(self):
                return True

            def set(self, *_args):
                return True

            def get(self, _prop):
                return 30.0

            def read(self):
                time.sleep(0.001)
                return True, object()

            def release(self):
                pass

        events = []
        worker = OpenCVCaptureWorker(
            "synthetic-only",
            CaptureMode("synthetic", 320, 240, 30),
            capture_factory=lambda _device: FakeCapture(),
            telemetry_buffer=BoundedTelemetryBuffer(capacity=64),
        )
        subscription = worker.subscribe_timing_listener(events.append)
        worker.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(worker.telemetry_buffer.snapshot()) < 5:
            time.sleep(0.002)
        worker.request_stop()
        worker.join(timeout=1.0)
        subscription.close()

        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(len(worker.telemetry_buffer.snapshot()), 5)
        arrivals = [event for event in events if event.kind == "frame_arrival"]
        self.assertEqual(len(arrivals), worker.snapshot().frame_count)
        self.assertEqual(len(arrivals), len(worker.telemetry_buffer.snapshot()))
        self.assertEqual(worker.telemetry_buffer.snapshot()[-1].clock_domain, ClockDomain.HOST_MONOTONIC)
        self.assertIsNone(worker.telemetry_buffer.snapshot()[-1].get(Metric.CHDKPTP_FETCH_FPS))
        self.assertGreaterEqual(
            worker.telemetry_buffer.snapshot()[-1].get(Metric.OPENCV_READ_CALL_DURATION_MS).value,
            0,
        )


    def test_failed_reads_are_published_with_independent_failure_and_connection_state(self) -> None:
        from camera_exploration_lab.capture import CaptureState

        class FailingCapture:
            def __init__(self) -> None:
                self.read_count = 0

            def isOpened(self):
                return True

            def set(self, *_args):
                return True

            def get(self, prop):
                return {3: 320.0, 4: 240.0, 5: 30.0, 6: 0.0}.get(prop, 0.0)

            def read(self):
                self.read_count += 1
                return False, None

            def release(self):
                pass

        worker = OpenCVCaptureWorker(
            "synthetic-failing-only", CaptureMode("synthetic-failure", 320, 240, 30),
            capture_factory=lambda _device: FailingCapture(), read_failure_limit=3,
            telemetry_buffer=BoundedTelemetryBuffer(capacity=8),
        )
        worker.start()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.snapshot().state, CaptureState.UNAVAILABLE)
        samples = worker.telemetry_buffer.snapshot()
        self.assertEqual(len(samples), 3)
        self.assertEqual(
            [item.get(Metric.READ_FAILURES).value for item in samples], [1, 2, 3]
        )
        self.assertEqual(
            [item.get(Metric.CONNECTION_STATE).value for item in samples],
            [CaptureState.RUNNING.value, CaptureState.RUNNING.value, CaptureState.UNAVAILABLE.value],
        )
        self.assertTrue(all(item.get(Metric.SUCCESSFUL_FRAME_COUNT).value == 0 for item in samples))


if __name__ == "__main__":
    unittest.main(verbosity=2)
