"""Bounded, streaming persistence for live camera experiments."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .capture import CaptureTimingEvent, NegotiatedSettings
from .capture_modes import CaptureMode
from .db import Database
from .models import (
    BackendKind,
    BenchmarkRecord,
    CameraIdentity,
    MeasurementSample,
    SessionRecord,
    SettingSnapshot,
    StreamScope,
    new_id,
    utc_now_iso,
)
from .recording_ownership import RecordingOwnership

logger = logging.getLogger(__name__)

MEASUREMENT_BATCH_SIZE = 128
TIMING_QUEUE_CAPACITY = 8192


class RecordingState(StrEnum):
    RECORDING = "recording"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RecordingSnapshot:
    state: RecordingState
    benchmark_id: str
    session_id: str
    started_at: str
    requested_duration_seconds: int | None
    elapsed_seconds: float = 0.0
    frame_count: int = 0
    read_failures: int = 0
    reason: str = ""
    summary: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _QueuedTiming:
    kind: str
    monotonic_ns: int
    observed_at: str
    value: float | None = None
    interval_ns: int | None = None
    capture_fps: float = 0.0


def _process_start_ticks() -> str | None:
    try:
        stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
        return stat[stat.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None


class ExperimentRecorder:
    """Persist timing events on a dedicated writer thread using bounded memory.

    The capture thread only submits timestamp metadata with ``put_nowait``. It
    never hands frame objects to this service and never waits for SQLite.
    """

    def __init__(
        self,
        database: Database,
        camera: CameraIdentity,
        mode: CaptureMode,
        negotiated: NegotiatedSettings,
        *,
        name: str,
        notes: str,
        duration_seconds: int | None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        now_iso: Callable[[], str] = utc_now_iso,
        queue_capacity: int = TIMING_QUEUE_CAPACITY,
        batch_size: int = MEASUREMENT_BATCH_SIZE,
    ) -> None:
        if duration_seconds not in (None, 10, 30, 60, 120):
            raise ValueError("recording duration must be manual or 10, 30, 60, or 120 seconds")
        if not name.strip():
            raise ValueError("experiment name is required")
        self.database = database
        self.camera = camera
        self.mode = mode
        self.negotiated = negotiated
        self.duration_seconds = duration_seconds
        self._clock_ns = clock_ns
        self._now_iso = now_iso
        self._batch_size = max(1, batch_size)
        self._queue: queue.Queue[_QueuedTiming] = queue.Queue(maxsize=max(1, queue_capacity))
        self._lock = threading.Lock()
        self._accepting = True
        self._stop_requested = threading.Event()
        self._started_ns = self._clock_ns()
        self._deadline_ns = (
            self._started_ns + duration_seconds * 1_000_000_000
            if duration_seconds is not None else None
        )
        self._started_at = self._now_iso()
        self._snapshot = RecordingSnapshot(
            state=RecordingState.RECORDING,
            benchmark_id="",
            session_id="",
            started_at=self._started_at,
            requested_duration_seconds=duration_seconds,
        )
        self._terminal_status = RecordingState.INTERRUPTED
        self._terminal_reason = "manual_stop"
        self._terminal_ns: int | None = None
        self._terminal_utc: str | None = None
        self._writer_error: str | None = None
        self._last_capture_fps_ns = self._started_ns
        self._last_preview_fps_ns = self._started_ns
        self._frame_count = 0
        self._read_failures = 0
        self._benchmark_id = new_id()
        self._session_id = new_id()
        self._ownership: RecordingOwnership | None = None

        requested = {
            "width": mode.width,
            "height": mode.height,
            "fps": mode.fps,
            "pixel_format": mode.pixel_format,
        }
        reported = {
            "width": negotiated.width,
            "height": negotiated.height,
            "fps": negotiated.fps,
            "pixel_format": negotiated.pixel_format,
        }
        owner_metadata: dict[str, object] = {
            "camera_identity": {
                "stable_identifier": camera.stable_identifier,
                "backend": camera.backend.value if isinstance(camera.backend, BackendKind) else str(camera.backend),
                "device_path": camera.device_path,
                "identity_source": camera.identity_source,
                "vendor_id": camera.vendor_id,
                "product_id": camera.product_id,
                "manufacturer": camera.manufacturer,
                "model": camera.model,
                "serial_number": camera.serial_number,
            },
            "requested_capture_settings": requested,
            "driver_reported_capture_settings": reported,
            "owner_pid": os.getpid(),
        }
        process_ticks = _process_start_ticks()
        if process_ticks is not None:
            owner_metadata["owner_process_start_ticks"] = process_ticks
        configuration = {
            "requested": requested,
            "driver_reported": reported,
            "duration_seconds": duration_seconds,
            "capture_timing_method": "host monotonic timestamp immediately after OpenCV VideoCapture.read returned",
            "capture_fps_scope": "capture",
            "preview_fps_scope": "preview",
            "processing_fps_scope": "processing",
        }
        session = SessionRecord(
            camera_id=camera.stable_identifier,
            label=name.strip(),
            started_at=self._started_at,
            source="live_experiment",
            notes=notes.strip() or None,
            metadata=owner_metadata,
            id=self._session_id,
            created_at=self._started_at,
        )
        benchmark = BenchmarkRecord(
            session_id=self._session_id,
            label=mode.label,
            started_at=self._started_at,
            status="running",
            configuration=configuration,
            id=self._benchmark_id,
        )
        settings = [
            SettingSnapshot(
                session_id=self._session_id,
                benchmark_id=self._benchmark_id,
                setting_key=key,
                requested_value=requested[key],
                reported_value=reported[key],
                verification_status="reported" if reported[key] is not None else "not_reported",
                requested_at=self._started_at,
                checked_at=self._started_at,
                verification_method="OpenCV VideoCapture.get driver readback",
            )
            for key in ("width", "height", "fps", "pixel_format")
        ]
        self._ownership = self.database.acquire_recording_ownership(owner_kind="single_camera")
        owner_metadata["recording_owner_token"] = self._ownership.owner_token
        session = replace(session, metadata=owner_metadata)
        try:
            self.database.begin_live_experiment(
                camera, session, benchmark, settings, seen_at=self._started_at,
                ownership=self._ownership,
            )
        except BaseException:
            self._release_ownership()
            raise
        self._snapshot = replace(
            self._snapshot, benchmark_id=self._benchmark_id, session_id=self._session_id
        )
        self._thread = threading.Thread(
            target=self._writer_loop,
            name=f"celab-recorder-{self._benchmark_id[:8]}",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            try:
                self.database.finish_live_experiment(
                    self._benchmark_id,
                    ended_at=self._now_iso(),
                    duration_seconds=0.0,
                    status="failed",
                    termination_reason="writer_start_failure",
                    ownership=self._ownership,
                )
            except Exception:
                logger.exception("Could not finalize writer-start failure for %s", self._benchmark_id)
            else:
                self._release_ownership()
            raise
        logger.info("Started experiment %s (%s)", name.strip(), self._benchmark_id)

    def _release_ownership(self) -> None:
        ownership = self._ownership
        if ownership is None:
            return
        try:
            ownership.release(ownership.owner_token)
        except Exception:
            logger.exception("Could not release recording ownership for %s", self._benchmark_id)
        else:
            self._ownership = None

    def snapshot(self) -> RecordingSnapshot:
        with self._lock:
            return self._snapshot

    def _offer(self, event: _QueuedTiming) -> None:
        with self._lock:
            if not self._accepting:
                return
            if self._deadline_ns is not None and event.monotonic_ns >= self._deadline_ns:
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._accepting = False
                self._terminal_status = RecordingState.INTERRUPTED
                self._terminal_reason = "timing_queue_overflow"
                self._terminal_ns = event.monotonic_ns
                self._terminal_utc = event.observed_at
                self._stop_requested.set()
                logger.error("Experiment %s timing queue is full; capture remains active", self._benchmark_id)

    def record_capture_event(self, event: CaptureTimingEvent) -> None:
        """Non-blocking callback suitable for the camera-owning thread."""
        self._offer(_QueuedTiming(
            kind=event.kind,
            monotonic_ns=event.monotonic_ns,
            observed_at=event.observed_at,
            interval_ns=event.interval_ns,
            capture_fps=event.capture_fps,
        ))

    def record_preview_fps(self, fps: float, *, monotonic_ns: int | None = None,
                           observed_at: str | None = None) -> None:
        timestamp = self._clock_ns() if monotonic_ns is None else monotonic_ns
        self._offer(_QueuedTiming(
            kind="preview_fps",
            monotonic_ns=timestamp,
            observed_at=observed_at or self._now_iso(),
            value=max(0.0, float(fps)),
        ))

    def stop(self, reason: str = "manual_stop", *, timeout: float = 5.0) -> RecordingSnapshot:
        """Stop early as interrupted, drain queued events, then close the record."""
        with self._lock:
            if self._snapshot.state is RecordingState.RECORDING:
                if self._accepting:
                    self._accepting = False
                    if self._deadline_ns is not None and self._clock_ns() >= self._deadline_ns:
                        self._terminal_status = RecordingState.COMPLETED
                        self._terminal_reason = "duration_elapsed"
                        self._terminal_ns = self._deadline_ns
                        self._terminal_utc = self._deadline_iso()
                    else:
                        self._terminal_status = RecordingState.INTERRUPTED
                        self._terminal_reason = reason
                        self._terminal_ns = self._clock_ns()
                        self._terminal_utc = self._now_iso()
                self._stop_requested.set()
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            logger.error("Experiment writer did not finish within %.1f seconds", timeout)
        return self.snapshot()

    def _set_snapshot(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def _measurement(self, event: _QueuedTiming, metric: str, value: float, unit: str,
                     *, scope: StreamScope, sample_index: int | None,
                     interval_ns: int | None = None, method: str) -> MeasurementSample:
        elapsed = max(0.0, (event.monotonic_ns - self._started_ns) / 1_000_000_000)
        return MeasurementSample(
            benchmark_id=self._benchmark_id,
            metric=metric,
            value=value,
            unit=unit,
            stream_scope=scope,
            observed_at=event.observed_at,
            elapsed_seconds=elapsed,
            sample_index=sample_index,
            interval_ns=interval_ns,
            method=method,
        )

    def _samples_for(self, event: _QueuedTiming) -> list[MeasurementSample]:
        elapsed = max(0.0, (event.monotonic_ns - self._started_ns) / 1_000_000_000)
        if event.kind == "frame_arrival":
            self._frame_count += 1
            # The capture worker may have been previewing before recording was
            # started. Its first delivered timing event can therefore include
            # an interval whose earlier endpoint is outside this experiment.
            interval_ns = event.interval_ns if self._frame_count > 1 else None
            samples = [self._measurement(
                event, "frame_arrival", elapsed, "seconds", scope=StreamScope.CAPTURE,
                sample_index=self._frame_count, interval_ns=interval_ns,
                method="host monotonic time immediately after OpenCV read returned",
            )]
            if interval_ns is not None:
                samples.append(self._measurement(
                    event, "frame_interval_ms", interval_ns / 1_000_000, "ms",
                    scope=StreamScope.CAPTURE, sample_index=self._frame_count,
                    interval_ns=interval_ns,
                    method="difference between successive host monotonic read-return timestamps",
                ))
            if event.monotonic_ns - self._last_capture_fps_ns >= 1_000_000_000:
                samples.append(self._measurement(
                    event, "capture_fps", event.capture_fps, "frames/s",
                    scope=StreamScope.CAPTURE, sample_index=self._frame_count,
                    method="bounded rolling window of host read-return timestamps",
                ))
                self._last_capture_fps_ns = event.monotonic_ns
            self._set_snapshot(frame_count=self._frame_count)
            return samples
        if event.kind == "read_failure":
            self._read_failures += 1
            self._set_snapshot(read_failures=self._read_failures)
            return [self._measurement(
                event, "read_failure", 1.0, "count", scope=StreamScope.CAPTURE,
                sample_index=self._frame_count,
                method="unsuccessful OpenCV VideoCapture.read call",
            )]
        if event.kind == "preview_fps" and event.value is not None:
            if event.monotonic_ns - self._last_preview_fps_ns >= 500_000_000:
                self._last_preview_fps_ns = event.monotonic_ns
                return [self._measurement(
                    event, "preview_fps", event.value, "frames/s",
                    scope=StreamScope.PREVIEW, sample_index=None,
                    method="Qt preview render cadence measured independently from camera capture",
                )]
        return []

    def _elapsed(self) -> float:
        elapsed = max(0.0, (self._clock_ns() - self._started_ns) / 1_000_000_000)
        if self.duration_seconds is not None:
            elapsed = min(elapsed, float(self.duration_seconds))
        return elapsed

    def _deadline_iso(self) -> str:
        if self.duration_seconds is None:
            return self._now_iso()
        try:
            started = datetime.fromisoformat(self._started_at)
            return (started + timedelta(seconds=self.duration_seconds)).isoformat(timespec="microseconds")
        except ValueError:
            return self._now_iso()

    def _writer_loop(self) -> None:
        batch: list[MeasurementSample] = []
        terminal_status = RecordingState.INTERRUPTED
        terminal_reason = "manual_stop"
        try:
            while True:
                if self._deadline_ns is not None and self._clock_ns() >= self._deadline_ns:
                    with self._lock:
                        if self._accepting:
                            self._accepting = False
                            self._terminal_status = RecordingState.COMPLETED
                            self._terminal_reason = "duration_elapsed"
                            self._terminal_ns = self._deadline_ns
                            self._terminal_utc = self._deadline_iso()
                            self._stop_requested.set()
                try:
                    event = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
                    self._set_snapshot(elapsed_seconds=self._elapsed())
                    continue
                try:
                    for sample in self._samples_for(event):
                        batch.append(sample)
                        if len(batch) == self._batch_size:
                            self.database.record_measurements_batch(batch, ownership=self._ownership)
                            batch.clear()
                finally:
                    self._queue.task_done()
                self._set_snapshot(elapsed_seconds=self._elapsed())
                if self._stop_requested.is_set() and self._queue.empty():
                    break

            if batch:
                self.database.record_measurements_batch(batch, ownership=self._ownership)
                batch.clear()
            with self._lock:
                terminal_status = self._terminal_status
                terminal_reason = self._terminal_reason
                terminal_ns = self._terminal_ns
                terminal_utc = self._terminal_utc
            if self._writer_error:
                terminal_status = RecordingState.FAILED
                terminal_reason = "database_write_failure"
            terminal_ns = terminal_ns if terminal_ns is not None else self._clock_ns()
            elapsed = max(0.0, (terminal_ns - self._started_ns) / 1_000_000_000)
            if self.duration_seconds is not None:
                elapsed = min(elapsed, float(self.duration_seconds))
            ended_at = terminal_utc or self._now_iso()
            summary = self.database.finish_live_experiment(
                self._benchmark_id,
                ended_at=ended_at,
                duration_seconds=elapsed,
                status=terminal_status.value,
                termination_reason=terminal_reason,
                ownership=self._ownership,
            )
            self._release_ownership()
            self._set_snapshot(
                state=terminal_status,
                elapsed_seconds=elapsed,
                read_failures=self._read_failures,
                reason=terminal_reason,
                summary=summary,
            )
            logger.info(
                "Experiment %s ended as %s after %.3f seconds (%s)",
                self._benchmark_id, terminal_status.value, elapsed, terminal_reason,
            )
        except Exception as exc:
            logger.exception("Experiment writer failed for %s", self._benchmark_id)
            self._writer_error = f"{type(exc).__name__}: {exc}"
            self._set_snapshot(
                state=RecordingState.FAILED,
                elapsed_seconds=self._elapsed(),
                reason=f"database_write_failure: {self._writer_error}",
            )
            try:
                self.database.finish_live_experiment(
                    self._benchmark_id,
                    ended_at=self._now_iso(),
                    duration_seconds=self._elapsed(),
                    status="failed",
                    termination_reason="database_write_failure",
                    ownership=self._ownership,
                )
            except Exception:
                # Keep the SQLite row as running; next launch's recovery marks it
                # interrupted. The failed writer never marks it completed.
                logger.exception("Could not close failed experiment %s", self._benchmark_id)
            else:
                self._release_ownership()
