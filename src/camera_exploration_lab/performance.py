"""Camera-neutral, bounded host-side performance telemetry contracts.

Telemetry is scalar metadata only. Capture workers publish to a bounded ring
that readers poll; this path does not use Qt signals and never waits for a
database writer or preview consumer.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from math import isfinite
import threading
from typing import TypeAlias


class Metric(StrEnum):
    REQUESTED_FPS = "requested_fps"
    OPENCV_CAPTURE_FPS = "opencv_capture_fps"
    CHDKPTP_FETCH_FPS = "chdkptp_payload_fetch_fps"
    CONTENT_CHANGE_RATE = "threshold_content_change_rate"
    CHDKPTP_GUI_REPORTED_FPS = "chdkptp_gui_reported_fps"
    GUI_PREVIEW_UPDATE_FPS = "gui_preview_update_fps"
    GUI_PRESENTATION_FPS = "gui_presentation_fps"
    SENSOR_EXPOSURE_FPS = "sensor_exposure_fps"
    CHDKPTP_FETCH_DURATION_MS = "chdkptp_fetch_call_duration_ms"
    OPENCV_READ_CALL_DURATION_MS = "opencv_read_call_duration_ms"
    FRAME_INTERVAL_MS = "host_read_return_interval_ms"
    SOFTWARE_SKIPS = "software_skips"
    CHDKPTP_FETCH_ATTEMPTS = "chdkptp_fetch_attempts"
    CHDKPTP_POLL_ATTEMPTS = "chdkptp_poll_attempts"
    DEVICE_POLL_CHECKS = "device_poll_checks"
    CHDKPTP_FETCH_COUNT = "chdkptp_completed_fetch_count"
    CHDKPTP_DROPPED_COUNTER = "chdkptp_dropped_counter"
    CHDKPTP_REPORTED_THROUGHPUT_KBPS = "chdkptp_reported_throughput_kbps"
    CHDKPTP_REPORTED_USB_SPEED_KBPS = "chdkptp_reported_usb_speed_kbps"
    CONTENT_SAMPLE_COUNT = "content_sample_count"
    EXACT_REPEAT_COUNT = "exact_repeat_count"
    SUCCESSFUL_FRAME_COUNT = "successful_opencv_frame_count"
    READ_FAILURES = "opencv_read_failures"
    CONNECTION_STATE = "connection_state"


class Availability(StrEnum):
    AVAILABLE = "available"
    NOT_RECORDED = "not_recorded"
    UNAVAILABLE = "unavailable"


class MeasurementKind(StrEnum):
    OBSERVED = "observed"
    REQUESTED = "requested"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    REPORTED = "reported"


class ClockDomain(StrEnum):
    HOST_MONOTONIC = "host_monotonic"
    SOURCE_ELAPSED = "source_elapsed"


MetricValue: TypeAlias = int | float | str | None

_UNITS: dict[Metric, str] = {
    Metric.REQUESTED_FPS: "frames/s",
    Metric.OPENCV_CAPTURE_FPS: "successful OpenCV reads/s",
    Metric.CHDKPTP_FETCH_FPS: "completed CHDKPTP payload fetches/s",
    Metric.CONTENT_CHANGE_RATE: "threshold-classified changes/s",
    Metric.CHDKPTP_GUI_REPORTED_FPS: "CHDKPTP-reported frames/s",
    Metric.GUI_PREVIEW_UPDATE_FPS: "GUI preview updates/s",
    Metric.GUI_PRESENTATION_FPS: "physical display presentations/s",
    Metric.SENSOR_EXPOSURE_FPS: "sensor exposures/s",
    Metric.CHDKPTP_FETCH_DURATION_MS: "ms/fetch call",
    Metric.OPENCV_READ_CALL_DURATION_MS: "ms/OpenCV read call",
    Metric.FRAME_INTERVAL_MS: "ms between successful host read returns",
    Metric.SOFTWARE_SKIPS: "count per recorded sample",
    Metric.CHDKPTP_FETCH_ATTEMPTS: "count per recorded sample",
    Metric.CHDKPTP_POLL_ATTEMPTS: "count per recorded sample",
    Metric.DEVICE_POLL_CHECKS: "count per recorded sample",
    Metric.CHDKPTP_FETCH_COUNT: "completed payload fetches per recorded sample",
    Metric.CHDKPTP_DROPPED_COUNTER: "CHDKPTP-reported dropped count (cumulative)",
    Metric.CHDKPTP_REPORTED_THROUGHPUT_KBPS: "CSV-reported kbps",
    Metric.CHDKPTP_REPORTED_USB_SPEED_KBPS: "CSV-reported kbps",
    Metric.CONTENT_SAMPLE_COUNT: "content samples per recorded sample",
    Metric.EXACT_REPEAT_COUNT: "exact repeats per recorded sample",
    Metric.SUCCESSFUL_FRAME_COUNT: "successful OpenCV frames (cumulative)",
    Metric.READ_FAILURES: "unsuccessful OpenCV reads (cumulative)",
    Metric.CONNECTION_STATE: "state",
}

_NONNEGATIVE = set(_UNITS) - {Metric.CONNECTION_STATE}
_COUNT_METRICS = {
    Metric.SOFTWARE_SKIPS,
    Metric.CHDKPTP_FETCH_ATTEMPTS,
    Metric.CHDKPTP_POLL_ATTEMPTS,
    Metric.DEVICE_POLL_CHECKS,
    Metric.CHDKPTP_FETCH_COUNT,
    Metric.CHDKPTP_DROPPED_COUNTER,
    Metric.CONTENT_SAMPLE_COUNT,
    Metric.EXACT_REPEAT_COUNT,
    Metric.SUCCESSFUL_FRAME_COUNT,
    Metric.READ_FAILURES,
}


@dataclass(frozen=True, slots=True)
class Measurement:
    """One metric value; timestamp and source are inherited from its sample."""

    metric: Metric
    value: MetricValue
    unit: str
    availability: Availability
    kind: MeasurementKind | None
    provenance: str

    def __post_init__(self) -> None:
        if not isinstance(self.metric, Metric):
            raise TypeError("measurement metric must use the Metric enum")
        if not isinstance(self.availability, Availability):
            raise TypeError("measurement availability must use the Availability enum")
        if self.kind is not None and not isinstance(self.kind, MeasurementKind):
            raise TypeError("measurement kind must use the MeasurementKind enum")
        if self.unit != _UNITS[self.metric]:
            raise ValueError(f"{self.metric.value} requires unit {_UNITS[self.metric]!r}")
        if not self.provenance.strip():
            raise ValueError("measurement provenance is required")
        if self.availability == Availability.AVAILABLE:
            if self.value is None or self.kind is None:
                raise ValueError("available measurements require a value and measurement kind")
        elif self.value is not None or self.kind is not None:
            raise ValueError("unavailable measurements must not contain a value or kind")
        if self.value is None:
            return
        if self.metric == Metric.CONNECTION_STATE:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("connection state must be a non-empty string")
            return
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError(f"{self.metric.value} must be numeric")
        if not isfinite(float(self.value)):
            raise ValueError(f"{self.metric.value} must be finite")
        if self.metric in _NONNEGATIVE and self.value < 0:
            raise ValueError(f"{self.metric.value} cannot be negative")
        if self.metric in _COUNT_METRICS and not isinstance(self.value, int):
            raise TypeError(f"{self.metric.value} must be an integer count")


def reading(
    metric: Metric,
    value: MetricValue,
    *,
    kind: MeasurementKind | None = MeasurementKind.OBSERVED,
    availability: Availability = Availability.AVAILABLE,
    provenance: str,
) -> Measurement:
    """Create a validated reading with the unit fixed by its metric contract."""
    if availability != Availability.AVAILABLE:
        value = None
        kind = None
    return Measurement(metric, value, _UNITS[metric], availability, kind, provenance)


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """Small timestamped group of related scalar measurements."""

    timestamp_ns: int
    source: str
    clock_domain: ClockDomain
    readings: tuple[Measurement, ...]
    sequence: int | None = None
    source_elapsed_s: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int) or self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be a non-negative integer")
        if not self.source.strip():
            raise ValueError("telemetry source is required")
        if not isinstance(self.clock_domain, ClockDomain):
            raise TypeError("clock_domain must use the ClockDomain enum")
        if not isinstance(self.readings, tuple) or any(not isinstance(item, Measurement) for item in self.readings):
            raise TypeError("readings must be a tuple of Measurement values")
        if not self.readings:
            raise ValueError("telemetry sample must contain at least one metric reading")
        metrics = [item.metric for item in self.readings]
        if len(metrics) != len(set(metrics)):
            raise ValueError("telemetry sample cannot repeat a metric")
        if self.sequence is not None and (
            isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        if self.source_elapsed_s is not None and (
            isinstance(self.source_elapsed_s, bool)
            or not isinstance(self.source_elapsed_s, (int, float))
            or not isfinite(self.source_elapsed_s)
            or self.source_elapsed_s < 0
        ):
            raise ValueError("source elapsed time must be finite and non-negative")

    def get(self, metric: Metric) -> Measurement | None:
        return next((item for item in self.readings if item.metric == metric), None)


@dataclass(frozen=True, slots=True)
class BufferRead:
    entries: tuple[tuple[int, TelemetrySample], ...]
    next_cursor: int
    missed_entries: int


class BoundedTelemetryBuffer:
    """Thread-safe overwrite-oldest ring for polling telemetry consumers."""

    def __init__(self, capacity: int = 4096) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("telemetry buffer capacity must be a positive integer")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._entries: deque[tuple[int, TelemetrySample]] = deque(maxlen=capacity)
        self._next_sequence = 0
        self._overwritten_count = 0

    @property
    def overwritten_count(self) -> int:
        with self._lock:
            return self._overwritten_count

    def append(self, sample: TelemetrySample) -> int:
        if not isinstance(sample, TelemetrySample):
            raise TypeError("telemetry buffer accepts TelemetrySample values")
        with self._lock:
            sequence = self._next_sequence
            if len(self._entries) == self.capacity:
                self._overwritten_count += 1
            self._entries.append((sequence, sample))
            self._next_sequence += 1
            return sequence

    def snapshot(self) -> tuple[TelemetrySample, ...]:
        with self._lock:
            return tuple(sample for _sequence, sample in self._entries)

    def read_since(self, cursor: int | None = None) -> BufferRead:
        """Read bounded entries newer than cursor and report overwritten gaps.

        ``None`` reads the currently retained ring. Repeated calls should pass
        ``next_cursor`` back; no consumer callback or growing queue is created.
        """
        with self._lock:
            if not self._entries:
                if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < -1):
                    raise ValueError("telemetry buffer cursor must be -1 or greater")
                if cursor is not None and cursor > self._next_sequence - 1:
                    raise ValueError("telemetry buffer cursor is newer than the latest entry")
                return BufferRead((), self._next_sequence - 1, 0)
            oldest = self._entries[0][0]
            if cursor is None:
                start = oldest
                missed = 0
            else:
                if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < -1:
                    raise ValueError("telemetry buffer cursor must be -1 or greater")
                if cursor > self._entries[-1][0]:
                    raise ValueError("telemetry buffer cursor is newer than the latest entry")
                missed = max(0, oldest - (cursor + 1))
                start = max(oldest, cursor + 1)
            entries = tuple(islice(self._entries, max(0, start - oldest), None))
            return BufferRead(entries, self._entries[-1][0], missed)
