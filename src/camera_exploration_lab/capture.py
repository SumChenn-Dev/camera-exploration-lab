"""Threaded V4L2 capture with bounded preview handoff and host diagnostics.

OpenCV is imported only when a capture is started. One camera-owning worker
reads continuously and overwrites a single latest-frame slot; the GUI polls
that slot at its own display rate. Frame reads therefore never wait for a Qt
paint, and a fast camera cannot build an unbounded frame queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol

from .capture_modes import CaptureMode
from .models import CameraCandidate
from .performance import (
    Availability,
    BoundedTelemetryBuffer,
    ClockDomain,
    Metric,
    MeasurementKind,
    TelemetrySample,
    reading,
)

logger = logging.getLogger(__name__)

# Stable OpenCV CAP_PROP identifiers (documented by OpenCV's Video I/O API).
_CAP_PROP_FRAME_WIDTH = 3
_CAP_PROP_FRAME_HEIGHT = 4
_CAP_PROP_FPS = 5
_CAP_PROP_FOURCC = 6
_CAP_V4L2 = 200


class CaptureState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class CaptureLike(Protocol):
    def isOpened(self) -> bool: ...
    def set(self, prop: int, value: float) -> bool: ...
    def get(self, prop: int) -> float: ...
    def read(self) -> tuple[bool, Any]: ...
    def release(self) -> None: ...


CaptureFactory = Callable[[str], CaptureLike]


@dataclass(frozen=True, slots=True)
class NegotiatedSettings:
    width: int | None
    height: int | None
    fps: float | None
    pixel_format: str | None

    @property
    def dimensions(self) -> tuple[int, int] | None:
        if self.width is None or self.height is None:
            return None
        return self.width, self.height


@dataclass(frozen=True, slots=True)
class CaptureSnapshot:
    state: CaptureState
    requested: CaptureMode
    negotiated: NegotiatedSettings | None = None
    observed_frame_size: tuple[int, int] | None = None
    capture_fps: float = 0.0
    frame_interval_ms: float | None = None
    frame_count: int = 0
    read_failures: int = 0
    message: str = ""
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class CaptureTimingEvent:
    """Small host-side timing event; camera frames are never passed to a recorder."""

    kind: str
    monotonic_ns: int
    observed_at: str
    interval_ns: int | None = None
    capture_fps: float = 0.0


class CaptureTimingSubscription:
    """Exclusive, removable subscription to one worker's scalar timing events.

    The worker retains one callback slot for backwards compatibility with the
    single-camera recorder. A subscription owns that same slot until closed,
    so a second recorder cannot silently replace or share the callback.
    """

    def __init__(self, worker: "OpenCVCaptureWorker", token: object) -> None:
        self._worker = worker
        self._token = token
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
        return self._worker._remove_timing_subscription(self._token)


class LatestFrameBuffer:
    """A one-frame overwrite slot; readers may skip frames without blocking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Any = None
        self._timestamp_ns: int | None = None
        self._version = 0

    def publish(self, frame: Any, timestamp_ns: int) -> int:
        with self._lock:
            self._frame = frame
            self._timestamp_ns = timestamp_ns
            self._version += 1
            return self._version

    def read_after(self, version: int) -> tuple[Any, int, int] | None:
        with self._lock:
            if self._version <= version or self._frame is None or self._timestamp_ns is None:
                return None
            return self._frame, self._version, self._timestamp_ns


class RollingFrameRate:
    """Bounded host timestamp window for one independently measured stream."""

    def __init__(self, window_ns: int = 1_000_000_000, max_samples: int = 2048) -> None:
        self.window_ns = window_ns
        self._timestamps: deque[int] = deque(maxlen=max_samples)

    def reset(self) -> None:
        self._timestamps.clear()

    def add(self, timestamp_ns: int) -> float:
        self._timestamps.append(timestamp_ns)
        return self.value_at(timestamp_ns)

    def value_at(self, timestamp_ns: int) -> float:
        cutoff_ns = timestamp_ns - self.window_ns
        while len(self._timestamps) > 1 and self._timestamps[0] < cutoff_ns:
            self._timestamps.popleft()
        if len(self._timestamps) < 2:
            return 0.0
        span_ns = self._timestamps[-1] - self._timestamps[0]
        if span_ns <= 0:
            return 0.0
        return (len(self._timestamps) - 1) * 1_000_000_000 / span_ns


def preferred_device_path(candidate: CameraCandidate) -> str | None:
    """Open by-id where available; otherwise use the current V4L2 node."""
    identity = candidate.identity
    stable = identity.stable_identifier
    if identity.identity_source == "v4l_by_id" and stable.startswith("/dev/v4l/by-id/"):
        return stable
    return identity.device_path


def _open_opencv_capture(device: str) -> CaptureLike:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is not installed in this Python environment. "
            "Install the optional desktop dependencies in the per-user venv."
        ) from exc
    return cv2.VideoCapture(device, cv2.CAP_V4L2)


def _fourcc_code(value: str | None) -> int | None:
    if not value:
        return None
    encoded = value.encode("ascii", errors="replace")[:4].ljust(4, b"\0")
    return int.from_bytes(encoded, byteorder="little", signed=False)


def _fourcc_text(value: float) -> str | None:
    if not value:
        return None
    try:
        code = int(value)
        decoded = code.to_bytes(4, byteorder="little", signed=False).decode("ascii")
    except (OverflowError, UnicodeDecodeError, ValueError):
        return None
    cleaned = decoded.rstrip("\0 ")
    return cleaned or None


def _reported_int(value: float) -> int | None:
    if not value or value < 1:
        return None
    return int(round(value))


def _reported_fps(value: float) -> float | None:
    if value <= 0:
        return None
    return float(value)


def _frame_size(frame: Any) -> tuple[int, int] | None:
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        return None
    try:
        return int(shape[1]), int(shape[0])
    except (TypeError, ValueError, IndexError):
        return None


class OpenCVCaptureWorker(threading.Thread):
    """Camera-owning capture thread with non-blocking snapshots for the UI."""

    def __init__(
        self,
        device: str,
        mode: CaptureMode,
        *,
        capture_factory: CaptureFactory | None = None,
        frame_buffer: LatestFrameBuffer | None = None,
        read_failure_limit: int = 10,
        worker_label: str | None = None,
        telemetry_buffer: BoundedTelemetryBuffer | None = None,
    ) -> None:
        super().__init__(name=f"celab-capture-{worker_label or mode.name}", daemon=True)
        self.device = device
        self.mode = mode
        self.frame_buffer = frame_buffer or LatestFrameBuffer()
        # Pollable scalar telemetry has separate ownership from the exclusive
        # recorder listener. It never carries frame arrays or Qt signals.
        self.telemetry_buffer = telemetry_buffer or BoundedTelemetryBuffer()
        self._telemetry_sequence = 0
        self._capture_factory = capture_factory or _open_opencv_capture
        self._read_failure_limit = max(1, read_failure_limit)
        self._stop_event = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._listener_lock = threading.Lock()
        self._timing_listener: Callable[[CaptureTimingEvent], None] | None = None
        self._timing_subscription_token: object | None = None
        self._snapshot = CaptureSnapshot(
            state=CaptureState.STARTING,
            requested=mode,
            message="Opening camera…",
        )

    def snapshot(self) -> CaptureSnapshot:
        with self._snapshot_lock:
            return self._snapshot

    def request_stop(self) -> None:
        self._stop_event.set()
        current = self.snapshot()
        if current.state in (CaptureState.STARTING, CaptureState.RUNNING):
            self._update(state=CaptureState.STOPPING, message="Stopping after the current camera read…")

    def set_timing_listener(
        self,
        listener: Callable[[CaptureTimingEvent], None] | None,
    ) -> None:
        """Set the legacy single-camera timing sink.

        New integrations should use ``subscribe_timing_listener`` so their
        callback can only clear the listener slot it owns.
        """
        with self._listener_lock:
            if self._timing_subscription_token is not None:
                raise RuntimeError("the capture timing listener is owned by an active subscription")
            self._timing_listener = listener

    def subscribe_timing_listener(
        self,
        listener: Callable[[CaptureTimingEvent], None],
    ) -> CaptureTimingSubscription:
        """Claim the worker's single timing callback slot with an owned token."""
        if not callable(listener):
            raise TypeError("timing listener must be callable")
        token = object()
        with self._listener_lock:
            if self._timing_listener is not None:
                raise RuntimeError("this capture worker already has a timing listener")
            self._timing_listener = listener
            self._timing_subscription_token = token
        return CaptureTimingSubscription(self, token)

    def _remove_timing_subscription(self, token: object) -> bool:
        with self._listener_lock:
            if token is not self._timing_subscription_token:
                return False
            self._timing_listener = None
            self._timing_subscription_token = None
            return True

    def _emit_timing(self, kind: str, monotonic_ns: int, *, interval_ns: int | None = None,
                     capture_fps: float = 0.0) -> None:
        with self._listener_lock:
            listener = self._timing_listener
        if listener is None:
            return
        event = CaptureTimingEvent(
            kind=kind,
            monotonic_ns=monotonic_ns,
            observed_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            interval_ns=interval_ns,
            capture_fps=capture_fps,
        )
        try:
            listener(event)
        except Exception:
            # Persistence or GUI issues must not stall the independent camera loop.
            logger.exception("Camera timing listener failed")

    def _publish_capture_telemetry(
        self,
        *,
        timestamp_ns: int,
        read_duration_ns: int,
        capture_fps: float,
        frame_interval_ns: int | None,
        frame_count: int,
        read_failures: int,
        connection_state: str,
    ) -> None:
        """Append one bounded scalar event without touching the recorder slot."""
        readings = [
            reading(
                Metric.REQUESTED_FPS,
                self.mode.fps,
                kind=MeasurementKind.REQUESTED if self.mode.fps is not None else None,
                availability=(Availability.AVAILABLE if self.mode.fps is not None else Availability.UNAVAILABLE),
                provenance="Capture mode request; this is not driver readback.",
            ),
            reading(
                Metric.OPENCV_CAPTURE_FPS,
                max(0.0, capture_fps),
                kind=MeasurementKind.DERIVED,
                provenance="Successful OpenCV reads counted over the worker's host-monotonic rolling window.",
            ),
            reading(
                Metric.FRAME_INTERVAL_MS,
                frame_interval_ns / 1_000_000 if frame_interval_ns is not None else None,
                kind=MeasurementKind.OBSERVED if frame_interval_ns is not None else None,
                availability=(Availability.AVAILABLE if frame_interval_ns is not None else Availability.UNAVAILABLE),
                provenance="Difference between successive successful OpenCV read-return timestamps.",
            ),
            reading(
                Metric.OPENCV_READ_CALL_DURATION_MS,
                read_duration_ns / 1_000_000,
                provenance="Host-monotonic duration around cv2.VideoCapture.read(); not USB-only latency.",
            ),
            reading(
                Metric.SUCCESSFUL_FRAME_COUNT,
                frame_count,
                provenance="Cumulative successful reads in this capture-worker run.",
            ),
            reading(
                Metric.READ_FAILURES,
                read_failures,
                provenance="Cumulative unsuccessful OpenCV reads in this capture-worker run.",
            ),
            reading(
                Metric.CONNECTION_STATE,
                connection_state,
                provenance="Capture worker state at the host read-return observation.",
            ),
        ]
        sample = TelemetrySample(
            timestamp_ns=timestamp_ns,
            source="OpenCV/V4L2 capture worker",
            clock_domain=ClockDomain.HOST_MONOTONIC,
            readings=tuple(readings),
            sequence=self._telemetry_sequence,
        )
        self.telemetry_buffer.append(sample)
        self._telemetry_sequence += 1

    def _update(self, **changes: Any) -> None:
        with self._snapshot_lock:
            self._snapshot = replace(self._snapshot, **changes)

    def run(self) -> None:
        capture: CaptureLike | None = None
        capture_started_ns: int | None = None
        try:
            capture = self._capture_factory(self.device)
            if not capture.isOpened():
                self._update(state=CaptureState.FAILED, message="Camera could not be opened.")
                return

            # These are requests only. The fields below are read back separately.
            fourcc = _fourcc_code(self.mode.pixel_format)
            if fourcc is not None:
                capture.set(_CAP_PROP_FOURCC, float(fourcc))
            capture.set(_CAP_PROP_FRAME_WIDTH, float(self.mode.width))
            capture.set(_CAP_PROP_FRAME_HEIGHT, float(self.mode.height))
            if self.mode.fps is not None:
                capture.set(_CAP_PROP_FPS, float(self.mode.fps))

            negotiated = NegotiatedSettings(
                width=_reported_int(capture.get(_CAP_PROP_FRAME_WIDTH)),
                height=_reported_int(capture.get(_CAP_PROP_FRAME_HEIGHT)),
                fps=_reported_fps(capture.get(_CAP_PROP_FPS)),
                pixel_format=_fourcc_text(capture.get(_CAP_PROP_FOURCC)),
            )
            if self._stop_event.is_set():
                self._update(state=CaptureState.STOPPING, message="Stopped during camera initialization.")
                return
            capture_started_ns = time.monotonic_ns()
            self._update(
                state=CaptureState.RUNNING,
                negotiated=negotiated,
                message="Capturing; driver readback is shown separately from the request.",
                elapsed_seconds=0.0,
            )

            frame_rate = RollingFrameRate()
            previous_frame_ns: int | None = None
            consecutive_failures = 0
            last_snapshot_ns = 0

            while not self._stop_event.is_set():
                read_started_ns = time.monotonic_ns()
                ok, frame = capture.read()
                captured_ns = time.monotonic_ns()
                read_duration_ns = max(0, captured_ns - read_started_ns)
                if not ok or frame is None:
                    consecutive_failures += 1
                    current = self.snapshot()
                    total_failures = current.read_failures + 1
                    self._update(
                        read_failures=total_failures,
                        elapsed_seconds=(captured_ns - capture_started_ns) / 1_000_000_000,
                    )
                    if consecutive_failures >= self._read_failure_limit:
                        self._update(
                            state=CaptureState.UNAVAILABLE,
                            message=(
                                "Camera stopped returning frames. It may have been unplugged "
                                "or its stream ended."
                            ),
                        )
                    self._publish_capture_telemetry(
                        timestamp_ns=captured_ns,
                        read_duration_ns=read_duration_ns,
                        capture_fps=current.capture_fps,
                        frame_interval_ns=None,
                        frame_count=current.frame_count,
                        read_failures=total_failures,
                        connection_state=(
                            CaptureState.UNAVAILABLE.value
                            if consecutive_failures >= self._read_failure_limit
                            else CaptureState.RUNNING.value
                        ),
                    )
                    self._emit_timing(
                        "read_failure", captured_ns, capture_fps=current.capture_fps
                    )
                    if consecutive_failures >= self._read_failure_limit:
                        break
                    self._stop_event.wait(0.005)
                    continue

                consecutive_failures = 0
                frame_interval_ms = None
                frame_interval_ns = None
                if previous_frame_ns is not None:
                    frame_interval_ns = captured_ns - previous_frame_ns
                    frame_interval_ms = frame_interval_ns / 1_000_000
                previous_frame_ns = captured_ns
                capture_fps = frame_rate.add(captured_ns)
                current = self.snapshot()
                frame_count = current.frame_count + 1

                self.frame_buffer.publish(frame, captured_ns)
                self._publish_capture_telemetry(
                    timestamp_ns=captured_ns,
                    read_duration_ns=read_duration_ns,
                    capture_fps=capture_fps,
                    frame_interval_ns=frame_interval_ns,
                    frame_count=frame_count,
                    read_failures=current.read_failures,
                    connection_state=CaptureState.RUNNING.value,
                )
                self._emit_timing(
                    "frame_arrival", captured_ns,
                    interval_ns=frame_interval_ns,
                    capture_fps=capture_fps,
                )
                now_ns = captured_ns
                if now_ns - last_snapshot_ns >= 100_000_000 or frame_count == 1:
                    self._update(
                        observed_frame_size=_frame_size(frame),
                        capture_fps=capture_fps,
                        frame_interval_ms=frame_interval_ms,
                        frame_count=frame_count,
                        elapsed_seconds=(now_ns - capture_started_ns) / 1_000_000_000,
                    )
                    last_snapshot_ns = now_ns
                else:
                    self._update(
                        frame_count=frame_count, frame_interval_ms=frame_interval_ms,
                        elapsed_seconds=(now_ns - capture_started_ns) / 1_000_000_000,
                    )

        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("OpenCV is not installed"):
                logger.error("%s", exc)
            else:
                logger.exception("Camera capture initialization or read failed for %s", self.device)
            current = self.snapshot()
            if current.state not in (CaptureState.STOPPING, CaptureState.STOPPED):
                self._update(state=CaptureState.FAILED, message=f"Capture failed: {exc}")
        finally:
            if capture_started_ns is not None:
                self._update(elapsed_seconds=max(0.0, (time.monotonic_ns() - capture_started_ns) / 1_000_000_000))
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    logger.exception("Failed to release camera capture for %s", self.device)
            current = self.snapshot()
            if current.state == CaptureState.STOPPING:
                self._update(state=CaptureState.STOPPED, message="Capture stopped.")
            if current.state in (CaptureState.STOPPING, CaptureState.STOPPED,
                                 CaptureState.FAILED, CaptureState.UNAVAILABLE):
                # These cards show current performance, so do not leave a stale
                # capture rate visible after the stream has ended.
                self._update(capture_fps=0.0, frame_interval_ms=None)
