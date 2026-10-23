"""Isolated synthetic-only Qt harness for grouped timing recording.

This module is not imported by the ordinary desktop application. Its fake
sources emit scalar host-timing events and never discover/open cameras or
construct video frames. All SQLite and coordinator lifecycle work runs on an
owned Qt worker thread.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..capture import (
    CaptureSnapshot,
    CaptureState,
    CaptureTimingEvent,
    NegotiatedSettings,
    RollingFrameRate,
)
from ..capture_modes import CaptureMode
from ..capture_recording_adapter import (
    BridgeState,
    GroupedCaptureRecordingBridge,
)
from ..db import Database
from ..grouped_recording import (
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    GroupedStreamSpec,
)
from ..grouped_analysis import (
    GroupedAnalysisCancelled,
    GroupedExperimentAnalysis,
    load_grouped_analysis,
)
from ..grouped_recording_coordinator import (
    DEFAULT_STREAM_QUEUE_CAPACITY,
    CoordinatorState,
    GroupedRecordingCoordinator,
)
from ..models import BackendKind, CameraIdentity, utc_now_iso
from ..schema_migrations import (
    SYNTHETIC_DATABASE_APPLICATION_ID,
    migrate_synthetic_database_to_v3,
)
from .synthetic_grouped_explorer import GroupedExperimentExplorerDialog

SHUTDOWN_UNRESOLVED_WARNING_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SyntheticStreamView:
    slot: str
    capture_state: str
    capture_mode: str
    simulated_capture_fps: float
    frame_count: int
    read_failures: int
    recording_state: str
    accepted_samples: int
    persisted_samples: int
    queue_rejections: int
    persistence_losses: int
    queue_depth: int
    queue_capacity: int


@dataclass(frozen=True, slots=True)
class SyntheticHarnessView:
    operation: str
    capture_status_message: str
    recording_state: str
    session_id: str | None
    elapsed_seconds: float
    writer_status: str
    streams: tuple[SyntheticStreamView, SyntheticStreamView]
    last_error: str | None
    automatic_capture_stop_pending: bool


class _SyntheticSubscription:
    def __init__(self, source: SyntheticCaptureSource, callback: Callable[[CaptureTimingEvent], None]) -> None:
        self._source = source
        self._callback = callback
        self._closed = False

    def close(self) -> bool:
        if self._closed:
            return False
        self._closed = True
        with self._source._lock:
            if self._source._listener is self._callback:
                self._source._listener = None
        return True


class SyntheticCaptureSource:
    """Timing-only source that emulates the capture-worker adapter surface."""

    def __init__(
        self,
        slot: str,
        fps: int,
        *,
        width: int,
        height: int,
        pixel_format: str,
        auto_emit: bool = True,
        start_allowed: bool = True,
        fail_after_frames: int | None = None,
    ) -> None:
        self.slot = slot
        self.mode = CaptureMode(f"synthetic_{slot.lower()}", width, height, fps, pixel_format)
        self.auto_emit = auto_emit
        self.start_allowed = start_allowed
        self.fail_after_frames = fail_after_frames
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: Callable[[CaptureTimingEvent], None] | None = None
        self._state = CaptureState.STOPPED
        self._message = "Synthetic source is stopped."
        self._frames = 0
        self._read_failures = 0
        self._previous_ns: int | None = None
        self._rates = RollingFrameRate(max_samples=512)
        self._started_ns: int | None = None
        self._negotiated = NegotiatedSettings(width, height, float(fps), pixel_format)

    def start(self) -> bool:
        with self._lock:
            if self._state in {CaptureState.STARTING, CaptureState.RUNNING}:
                return False
            if not self.start_allowed:
                self._state = CaptureState.FAILED
                self._message = "Synthetic source startup was configured to fail."
                return False
            self._stop.clear()
            self._frames = 0
            self._read_failures = 0
            self._previous_ns = None
            self._rates.reset()
            self._started_ns = time.monotonic_ns()
            self._state = CaptureState.RUNNING
            self._message = "Fake timing source is running; no camera is open."
            if self.auto_emit:
                self._thread = threading.Thread(
                    target=self._produce,
                    name=f"cel-synthetic-capture-{self.slot.lower()}",
                    daemon=False,
                )
                self._thread.start()
        return True

    def _produce(self) -> None:
        period = 1.0 / max(1, self.mode.fps or 1)
        while not self._stop.wait(period):
            if not self.emit_frame():
                return

    def emit_frame(self, host_monotonic_ns: int | None = None) -> bool:
        timestamp = time.monotonic_ns() if host_monotonic_ns is None else host_monotonic_ns
        with self._lock:
            if self._state is not CaptureState.RUNNING:
                return False
            interval = None if self._previous_ns is None else max(0, timestamp - self._previous_ns)
            self._previous_ns = timestamp
            self._frames += 1
            self._rates.add(timestamp)
            callback = self._listener
            reached_failure = self.fail_after_frames is not None and self._frames >= self.fail_after_frames
        if callback is not None:
            callback(CaptureTimingEvent(
                "frame_arrival", timestamp, utc_now_iso(), interval_ns=interval,
                capture_fps=float(self.mode.fps or 0),
            ))
        if reached_failure:
            self.emit_read_failure(timestamp + 1)
            with self._lock:
                self._state = CaptureState.FAILED
                self._message = "Synthetic source injected a read failure."
            self._stop.set()
            return False
        return True

    def emit_read_failure(self, host_monotonic_ns: int | None = None) -> None:
        timestamp = time.monotonic_ns() if host_monotonic_ns is None else host_monotonic_ns
        with self._lock:
            self._read_failures += 1
            callback = self._listener
        if callback is not None:
            callback(CaptureTimingEvent("read_failure", timestamp, utc_now_iso()))

    def snapshot(self) -> CaptureSnapshot:
        now = time.monotonic_ns()
        with self._lock:
            elapsed = 0.0 if self._started_ns is None else max(0, now - self._started_ns) / 1_000_000_000
            rate = self._rates.value_at(now) if self._state is CaptureState.RUNNING else 0.0
            return CaptureSnapshot(
                state=self._state,
                requested=self.mode,
                negotiated=self._negotiated,
                capture_fps=rate,
                frame_count=self._frames,
                read_failures=self._read_failures,
                message=self._message,
                elapsed_seconds=elapsed,
            )

    def subscribe_timing_listener(self, listener: Callable[[CaptureTimingEvent], None]) -> _SyntheticSubscription:
        with self._lock:
            if self._listener is not None:
                raise RuntimeError("synthetic timing listener is already occupied")
            self._listener = listener
        return _SyntheticSubscription(self, listener)

    def stop(self, timeout: float = 0.75) -> bool:
        self.request_stop()
        return self.wait_stopped(timeout)

    def request_stop(self) -> None:
        """Ask the producer to stop without waiting for its thread."""
        self._stop.set()
        with self._lock:
            thread = self._thread
            if self._state is not CaptureState.FAILED:
                self._state = CaptureState.STOPPING
                self._message = "Synthetic source stop requested."

    def wait_stopped(self, timeout: float = 0.75) -> bool:
        """Wait at most ``timeout`` for a previously requested stop."""
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, timeout))
        alive = bool(thread and thread.is_alive())
        with self._lock:
            if not alive and self._state is not CaptureState.FAILED:
                self._state = CaptureState.STOPPED
                self._message = "Synthetic source stopped."
        return not alive


class _SyntheticRecordingWorker(QObject):
    """Owns all synthetic sources and schema-v3/coordinator operations."""

    snapshot_ready = Signal(object)
    action_finished = Signal(str, object)
    action_failed = Signal(str, str)
    shutdown_finished = Signal(bool, str)

    def __init__(
        self,
        shutdown_requested: threading.Event,
        *,
        source_factory: Callable[..., SyntheticCaptureSource] = SyntheticCaptureSource,
    ) -> None:
        super().__init__()
        self._shutdown_requested = shutdown_requested
        self._source_factory = source_factory
        self._timer: QTimer | None = None
        self._sources: dict[str, SyntheticCaptureSource] = {}
        self._ended_slots: set[str] = set()
        self._database: Database | None = None
        self._repository: GroupedExperimentRepository | None = None
        self._temporary_root: Path | None = None
        self._coordinator: GroupedRecordingCoordinator | None = None
        self._bridge: GroupedCaptureRecordingBridge | None = None
        self._session_id: str | None = None
        self._record_started_ns: int | None = None
        self._record_target_seconds: float | None = None
        self._keep_capture_running_after_recording = False
        self._auto_stop_pending = False
        self._auto_stop_result: str | None = None
        self._auto_stop_requested_at: float | None = None
        self._auto_stop_warning_emitted = False
        self._final_elapsed_seconds = 0.0
        self._final_record: Any = None
        self._finalization_unconfirmed = False
        self._operation = "Idle"
        self._last_error: str | None = None
        self._shutdown_running = False
        self._shutdown_complete = False
        self._shutdown_started_at: float | None = None
        self._shutdown_warning_emitted = False
        self._last_shutdown_notice: str | None = None

    @Slot()
    def initialize(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._publish_snapshot()

    @Slot(int, int)
    def start_both(self, rate_a: int, rate_b: int) -> None:
        if self._shutdown_requested.is_set():
            self.action_failed.emit("start_both", "Window shutdown has already been requested.")
            return
        if any(source.snapshot().state in {CaptureState.STARTING, CaptureState.RUNNING, CaptureState.STOPPING}
               for source in self._sources.values()):
            self.action_failed.emit("start_both", "Synthetic streams are already active or stopping.")
            return
        self._operation = "Starting both synthetic streams"
        self._last_error = None
        self._auto_stop_pending = False
        self._auto_stop_result = None
        self._auto_stop_requested_at = None
        self._auto_stop_warning_emitted = False
        self._ended_slots.clear()
        try:
            self._sources = {
                "A": self._source_factory(
                    "A", rate_a, width=320, height=240, pixel_format="GRBG"
                ),
                "B": self._source_factory(
                    "B", rate_b, width=1280, height=720, pixel_format="MJPG"
                ),
            }
            started = {slot: source.start() for slot, source in self._sources.items()}
            results = {slot: source.snapshot().state.value for slot, source in self._sources.items()}
            self._operation = "Synthetic streams running" if all(started.values()) else "Synthetic streams partially available"
            self.action_finished.emit("start_both", {"started": started, "states": results})
        except Exception as exc:
            self._last_error = self._safe_error(exc)
            self._operation = "Synthetic stream startup failed"
            self.action_failed.emit("start_both", self._last_error)
        self._publish_snapshot()

    @Slot(float, bool)
    def start_recording(self, target_seconds: float, keep_capture_running: bool = False) -> None:
        if self._shutdown_requested.is_set():
            self.action_failed.emit("record_both", "Window shutdown has already been requested.")
            return
        if self._bridge is not None:
            state, _adapters = self._bridge.snapshot()
            if state in {BridgeState.STARTING, BridgeState.RECORDING}:
                self.action_failed.emit("record_both", "A grouped recording is already active.")
                return
        if not any(source.snapshot().state is CaptureState.RUNNING for source in self._sources.values()):
            self.action_failed.emit("record_both", "Start at least one synthetic stream before recording.")
            return
        self._operation = "Record Both requested; preparing isolated synthetic database"
        self._last_error = None
        self._session_id = None
        self._record_started_ns = None
        self._record_target_seconds = None
        self._keep_capture_running_after_recording = bool(keep_capture_running)
        self._auto_stop_pending = False
        self._auto_stop_result = None
        self._auto_stop_requested_at = None
        self._auto_stop_warning_emitted = False
        self._final_elapsed_seconds = 0.0
        self._final_record = None
        self._finalization_unconfirmed = False
        self._coordinator = None
        self._bridge = None
        try:
            self._ensure_synthetic_database()
            if self._shutdown_requested.is_set():
                raise RuntimeError("shutdown was requested before grouped recording startup")
            assert self._repository is not None
            spec = self._group_spec(target_seconds)
            self._coordinator = GroupedRecordingCoordinator.create_and_start(
                self._repository, spec,
            )
            self._session_id = spec.session_id
            self._bridge = GroupedCaptureRecordingBridge(self._coordinator)
            workers = {
                slot: source if source.snapshot().state is CaptureState.RUNNING else None
                for slot, source in self._sources.items()
            }
            report = self._bridge.start_recording(workers)
            self._record_started_ns = time.monotonic_ns()
            self._record_target_seconds = target_seconds
            self._final_elapsed_seconds = 0.0
            self._final_record = report.finalized_record
            self._finalization_unconfirmed = False
            state = self._bridge.snapshot()[0]
            if self._shutdown_requested.is_set() and state is BridgeState.RECORDING:
                self._bridge.shutdown(timeout=2.0)
                self._operation = "Startup cancelled by window close"
                self.action_finished.emit("record_both", {"started_slots": report.started_slots, "cancelled": True})
            else:
                self._operation = (
                    "Recording acknowledged by the grouped writer"
                    if state is BridgeState.RECORDING else "Grouped recording did not start on an available stream"
                )
                self.action_finished.emit("record_both", {
                    "started_slots": report.started_slots,
                    "failed_slots": report.failed_slots,
                    "fully_started": report.fully_started,
                    "final_status": str(report.finalized_record.status) if report.finalized_record else None,
                    "session_id": spec.session_id,
                })
        except Exception as exc:
            self._last_error = self._safe_error(exc)
            self._operation = "Recording startup failed"
            if self._bridge is not None:
                try:
                    self._bridge.shutdown(timeout=2.0)
                except Exception:
                    pass
            self.action_failed.emit("record_both", self._last_error)
        self._publish_snapshot()

    @Slot()
    def stop_recording(self) -> None:
        if self._bridge is None:
            self.action_failed.emit("stop_recording", "No grouped recording has been started.")
            return
        state, _adapters = self._bridge.snapshot()
        if state is BridgeState.FINALIZED and self._final_record is not None:
            self.action_finished.emit("stop_recording", {"status": str(self._final_record.status), "idempotent": True})
            return
        if state is not BridgeState.RECORDING:
            self.action_failed.emit("stop_recording", "Recording is not active or finalization is unresolved.")
            return
        self._finish_recording("manual_stop", "Finalizing recording; synthetic streams continue")

    @Slot(str)
    def stop_stream(self, slot: str) -> None:
        if slot not in {"A", "B"}:
            self.action_failed.emit("stop_stream", "Unknown synthetic stream slot.")
            return
        source = self._sources.get(slot)
        if source is None:
            self.action_failed.emit("stop_stream", f"Synthetic stream {slot} has not been created.")
            return
        self._operation = f"Stop {slot} requested"
        if self._bridge is not None and self._bridge.snapshot()[0] is BridgeState.RECORDING and slot not in self._ended_slots:
            source.stop()
            failed = source.snapshot().state is CaptureState.FAILED
            try:
                self._bridge.capture_ended(
                    slot, failed=failed,
                    reason_code="synthetic_read_failure" if failed else "user_stopped_stream",
                )
                self._ended_slots.add(slot)
            except Exception as exc:
                self._last_error = self._safe_error(exc)
                self.action_failed.emit("stop_stream", self._last_error)
                self._publish_snapshot()
                return
        else:
            source.stop()
        self._operation = f"Synthetic stream {slot} stopped"
        self.action_finished.emit("stop_stream", {"slot": slot, "state": source.snapshot().state.value})
        self._publish_snapshot()

    @Slot()
    def stop_both(self) -> None:
        self._operation = "Stop Both requested"
        # A user Stop Both request takes ownership of shutdown if natural
        # completion has already requested automatic source termination.
        self._auto_stop_pending = False
        self._auto_stop_result = None
        self._auto_stop_requested_at = None
        state = self._bridge.snapshot()[0] if self._bridge is not None else None
        finalized = True
        if state is BridgeState.RECORDING:
            finalized = self._finish_recording(
                "manual_stop", "Finalizing before stopping both synthetic streams", emit_action=False
            )
        elif state is BridgeState.FAILED:
            finalized = False
        if not finalized and self._bridge is not None:
            try:
                self._bridge.shutdown(timeout=2.0)
            except Exception:
                pass
        stop_results = {slot: source.stop() for slot, source in self._sources.items()}
        if not finalized:
            self._last_error = self._last_error or "Recording finalization was not durably acknowledged."
            self.action_failed.emit("stop_both", self._last_error)
        elif not all(stop_results.values()):
            self._last_error = "One or more synthetic producer threads did not stop within the bounded wait."
            self.action_failed.emit("stop_both", self._last_error)
        else:
            self._operation = "Both synthetic streams stopped"
            self.action_finished.emit("stop_both", {"stopped": stop_results})
        self._publish_snapshot()

    @Slot()
    def inspect_group(self) -> None:
        if self._repository is None or self._session_id is None or self._database is None:
            self.action_failed.emit("inspect", "No synthetic grouped experiment exists yet.")
            return
        if self._shutdown_requested.is_set():
            return
        try:
            analysis = load_grouped_analysis(
                self._database,
                self._session_id,
                cancellation_check=self._shutdown_requested.is_set,
            )
            if self._shutdown_requested.is_set():
                return
            group = analysis.group
            detail = {
                "session_id": group.session_id,
                "label": group.label,
                "status": str(group.status),
                "data_complete": group.data_complete,
                "integrity_flags": group.integrity_flags,
                "streams": tuple({
                    "slot": item.stream.slot,
                    "benchmark_id": item.stream.benchmark_id,
                    "camera_model": (item.stream.identity_snapshot or {}).get("model"),
                    "status": item.stream.stream_status,
                    "frames": item.stream.capture_frame_count,
                    "failures": item.stream.read_failure_count,
                    "persisted_samples": item.persisted_timing_sample_count,
                    "persisted_frame_samples": item.persisted_frame_samples,
                    "persisted_read_failure_samples": item.persisted_read_failure_samples,
                    "last_persisted_sample_index": item.last_sequence_index,
                    "interval_count": item.interval_stats.count,
                    "data_complete": item.effective_data_complete,
                } for item in analysis.streams),
                "grouped_analysis": analysis,
            }
            if analysis.recovery_schema_version is not None:
                finding = analysis.recovery_finding
                findings = () if finding is None else ({
                    "session_id": finding.session_id,
                    "status": finding.status,
                    "classification": finding.classification.value,
                    "lease_present": finding.lease_present,
                },)
                detail["read_only_recovery"] = {
                    "schema_version": analysis.recovery_schema_version,
                    "lock_state": analysis.recovery_lock_state,
                    "findings": findings,
                }
            if not self._shutdown_requested.is_set():
                self.action_finished.emit("inspect", detail)
        except GroupedAnalysisCancelled:
            # Window shutdown owns the cancellation; do not publish a stale
            # partial result or present normal cancellation as an error.
            return
        except Exception as exc:
            if not self._shutdown_requested.is_set():
                self.action_failed.emit("inspect", self._safe_error(exc))

    @Slot()
    def request_shutdown(self) -> None:
        if self._shutdown_running or self._shutdown_complete:
            return
        self._shutdown_running = True
        self._shutdown_started_at = time.monotonic()
        self._shutdown_warning_emitted = False
        self._shutdown_requested.set()
        self._operation = "Shutdown requested; finalizing synthetic work"
        self._try_shutdown()

    def _try_shutdown(self) -> None:
        self._auto_stop_pending = False
        self._auto_stop_result = None
        self._auto_stop_requested_at = None
        if self._bridge is not None:
            try:
                state, _adapters = self._bridge.snapshot()
                if state in {BridgeState.STARTING, BridgeState.RECORDING}:
                    self._bridge.shutdown(timeout=0.75)
                if (
                    self._coordinator is not None
                    and self._coordinator.snapshot().state is CoordinatorState.FINALIZED
                    and self._repository is not None
                    and self._session_id is not None
                ):
                    self._final_record = self._repository.get_group(self._session_id)
            except Exception as exc:
                self._last_error = self._safe_error(exc)
                # Capture sources still need to stop even if writer cancellation
                # failed. The detached adapters reject subsequent callbacks.
        unfinished = [slot for slot, source in self._sources.items() if not source.stop(timeout=0.5)]
        if unfinished:
            self._report_shutdown_wait(f"Synthetic producers still active: {', '.join(unfinished)}.")
            return
        if self._coordinator is not None and not self._coordinator.wait_closed(timeout=0.0):
            self._report_shutdown_wait("Coordinator writer is still draining; finalization is not acknowledged.")
            return

        details = "Synthetic sources and writer closed."
        unresolved = bool(
            self._coordinator is not None
            and (self._coordinator.snapshot().state is CoordinatorState.FAILED or self._final_record is None)
        )
        if self._temporary_root is not None:
            if unresolved:
                details += " Finalization is unresolved; the disposable synthetic database was retained for inspection."
            else:
                try:
                    self._remove_synthetic_database()
                    details += " Disposable synthetic database removed."
                except OSError as exc:
                    self._last_error = self._safe_error(exc)
                    details += " Finalization is committed, but temporary synthetic data cleanup failed and was retained."
        self._operation = "Shutdown complete" if not unresolved else "Shutdown complete with unresolved synthetic recording"
        self._shutdown_complete = True
        self._shutdown_running = False
        if self._timer is not None:
            self._timer.stop()
        self.shutdown_finished.emit(True, details)
        self._publish_snapshot()

    def _report_shutdown_wait(self, detail: str) -> None:
        if (
            self._shutdown_started_at is not None
            and not self._shutdown_warning_emitted
            and time.monotonic() - self._shutdown_started_at >= SHUTDOWN_UNRESOLVED_WARNING_SECONDS
        ):
            self._shutdown_warning_emitted = True
        if self._shutdown_warning_emitted:
            # Keep the unresolved warning visible on later polls. Replacing it
            # with the shorter progress text made it disappear after one tick.
            detail = (
                "Shutdown deadline exceeded; finalization remains unresolved. "
                "The harness keeps the writer and database alive and continues checking; "
                "no completed status is asserted. " + detail
            )
        if detail != self._last_shutdown_notice:
            self._last_shutdown_notice = detail
            self.shutdown_finished.emit(False, detail)
        if self._timer is not None and not self._timer.isActive():
            self._timer.start()

    def _ensure_synthetic_database(self) -> None:
        if self._database is not None:
            return
        root = Path(tempfile.mkdtemp(prefix="cel-synthetic-grouped-ui-"))
        self._temporary_root = root
        path = root / "synthetic-grouped.sqlite3"
        if path.exists():
            raise RuntimeError("new synthetic temporary directory was not empty")
        database = Database(path)
        database.initialize()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA application_id={SYNTHETIC_DATABASE_APPLICATION_ID}")
            connection.commit()
        migrate_synthetic_database_to_v3(database)
        with database.connect_readonly() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            marker = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if version != 3 or marker != SYNTHETIC_DATABASE_APPLICATION_ID:
            raise RuntimeError("isolated synthetic database did not pass its schema marker check")
        self._temporary_root = root
        self._database = database
        self._repository = GroupedExperimentRepository(database)

    def _group_spec(self, target_seconds: float) -> GroupedExperimentSpec:
        streams: list[GroupedStreamSpec] = []
        for slot in ("A", "B"):
            source = self._sources.get(slot)
            if source is None:
                raise RuntimeError("both synthetic stream participants must be configured")
            mode = source.mode
            model = "Synthetic PS3 Eye-style timing source" if slot == "A" else "Synthetic CAM 313-style timing source"
            camera = CameraIdentity(
                stable_identifier=f"synthetic:cel013:{slot.lower()}",
                backend=BackendKind.V4L2,
                device_path=f"synthetic://stream-{slot.lower()}",
                manufacturer="Synthetic CEL fixture",
                model=model,
                vendor_id="ffff",
                product_id="0001" if slot == "A" else "0002",
                identity_source="synthetic_fixture",
                metadata={"synthetic_only": True, "no_hardware": True},
            )
            streams.append(GroupedStreamSpec(
                slot=slot,
                camera=camera,
                endpoint_key=f"synthetic-endpoint-{slot.lower()}",
                benchmark_label=f"{mode.width}x{mode.height} {mode.pixel_format} synthetic events",
                configuration={
                    "width": mode.width,
                    "height": mode.height,
                    "pixel_format": mode.pixel_format,
                    "requested_fps": mode.fps,
                    "synthetic_target_fps": mode.fps,
                    "capture_source": "synthetic_timing_only",
                },
                timing_method="synthetic_monotonic_event_v1",
                timestamp_source="host_monotonic_synthetic_event",
                clock_type="monotonic_ns",
                collection_point="synthetic_event_producer",
                timing_method_version="1",
            ))
        return GroupedExperimentSpec(
            label="Synthetic dual-camera timing demonstration",
            notes="Fake timing events only; no physical camera or media was captured.",
            streams=(streams[0], streams[1]),
            duration_target_seconds=target_seconds,
            timing_method_version="synthetic-v1",
        )

    def _finish_recording(
        self, reason_code: str, message: str, *, emit_action: bool = True
    ) -> bool:
        if self._bridge is None:
            return False
        self._operation = message
        self._publish_snapshot()
        try:
            self._final_record = self._bridge.stop_recording(
                reason_code=reason_code, timeout=5.0,
            )
            if self._record_started_ns is not None:
                self._final_elapsed_seconds = max(
                    0, time.monotonic_ns() - self._record_started_ns
                ) / 1_000_000_000
            self._operation = f"Finalization committed: {self._final_record.status}"
            if emit_action:
                self.action_finished.emit("stop_recording", {
                    "status": str(self._final_record.status),
                    "data_complete": self._final_record.data_complete,
                    "session_id": self._final_record.session_id,
                })
            self._publish_snapshot()
            return True
        except Exception as exc:
            self._last_error = self._safe_error(exc)
            self._operation = "Finalization unconfirmed; recording is not reported complete"
            self._finalization_unconfirmed = True
            if emit_action:
                self.action_failed.emit("stop_recording", self._last_error)
            self._publish_snapshot()
            return False

    def _tick(self) -> None:
        for slot, source in tuple(self._sources.items()):
            state = source.snapshot().state
            if state is CaptureState.FAILED and slot not in self._ended_slots:
                if self._bridge is not None and self._bridge.snapshot()[0] is BridgeState.RECORDING:
                    try:
                        self._bridge.capture_ended(slot, failed=True, reason_code="synthetic_read_failure")
                    except Exception as exc:
                        self._last_error = self._safe_error(exc)
                    self._ended_slots.add(slot)
        if self._shutdown_requested.is_set():
            # closeEvent sets the shared flag before its queued request slot runs.
            # If this timer poll wins that race, initialize the deadline through
            # the same idempotent path so shutdown is not attempted twice first.
            if not self._shutdown_running:
                self.request_shutdown()
            else:
                self._try_shutdown()
        else:
            if self._auto_stop_pending:
                self._poll_automatic_source_stop()
        if not self._shutdown_requested.is_set() and self._bridge is not None and self._bridge.snapshot()[0] is BridgeState.RECORDING:
            target = self._record_target_seconds
            if target is not None and self._record_started_ns is not None:
                elapsed = (time.monotonic_ns() - self._record_started_ns) / 1_000_000_000
                if elapsed >= target:
                    finalized = self._finish_recording(
                        "requested_duration_elapsed", "Requested synthetic duration reached"
                    )
                    if (
                        finalized
                        and not self._keep_capture_running_after_recording
                        and not self._shutdown_requested.is_set()
                    ):
                        self._request_automatic_source_stop()
        self._publish_snapshot()

    def _request_automatic_source_stop(self) -> None:
        """Stop fake producers only after the coordinator acknowledges finalization."""
        if self._bridge is None or self._final_record is None:
            return
        if self._bridge.snapshot()[0] is not BridgeState.FINALIZED:
            return
        self._auto_stop_pending = True
        self._auto_stop_requested_at = time.monotonic()
        self._auto_stop_warning_emitted = False
        self._auto_stop_result = "pending"
        self._operation = "Recording finalized; waiting for synthetic capture shutdown"
        try:
            # Request both stops before polling either producer, so their
            # termination proceeds concurrently and no sample can follow the
            # finalized experiment through an attached adapter.
            for source in self._sources.values():
                source.request_stop()
        except Exception as exc:
            self._auto_stop_pending = False
            self._auto_stop_result = "failed"
            self._last_error = self._safe_error(exc)
            self._operation = "Recording finalized, but automatic capture stop failed"
            self.action_failed.emit("auto_stop", self._last_error)
            self._publish_snapshot()

    def _poll_automatic_source_stop(self) -> None:
        if not self._auto_stop_pending:
            return
        try:
            stopped = {
                slot: source.wait_stopped(timeout=0.0)
                for slot, source in self._sources.items()
            }
        except Exception as exc:
            self._auto_stop_pending = False
            self._auto_stop_result = "failed"
            self._last_error = self._safe_error(exc)
            self._operation = "Recording finalized, but automatic capture stop failed"
            self.action_failed.emit("auto_stop", self._last_error)
            return
        if all(stopped.values()):
            self._auto_stop_pending = False
            self._auto_stop_result = "stopped"
            status = str(self._final_record.status) if self._final_record is not None else "unconfirmed"
            self._operation = (
                f"Recording finalized as {status}; both synthetic capture sources stopped automatically"
            )
            self.action_finished.emit("auto_stop", {
                "stopped": stopped,
                "recording_status": str(self._final_record.status),
                "data_complete": self._final_record.data_complete,
            })
            return
        if (
            self._auto_stop_requested_at is not None
            and time.monotonic() - self._auto_stop_requested_at >= SHUTDOWN_UNRESOLVED_WARNING_SECONDS
            and not self._auto_stop_warning_emitted
        ):
            self._auto_stop_warning_emitted = True
            self.action_failed.emit(
                "auto_stop",
                "Recording is finalized, but one or more synthetic source stops are not yet acknowledged; "
                "use Stop Both to retry shutdown.",
            )

    def _publish_snapshot(self) -> None:
        coordinator_snapshot = self._coordinator.snapshot() if self._coordinator is not None else None
        stream_views = []
        for slot in ("A", "B"):
            source = self._sources.get(slot)
            capture = source.snapshot() if source is not None else None
            coordinator_stream = next(
                (stream for stream in coordinator_snapshot.streams if stream.slot == slot), None
            ) if coordinator_snapshot is not None else None
            stream_views.append(SyntheticStreamView(
                slot=slot,
                capture_state=capture.state.value if capture else "stopped",
                capture_mode=source.mode.label if source else "not configured",
                simulated_capture_fps=capture.capture_fps if capture else 0.0,
                frame_count=capture.frame_count if capture else 0,
                read_failures=capture.read_failures if capture else 0,
                recording_state=coordinator_stream.state.value if coordinator_stream else "not recording",
                accepted_samples=coordinator_stream.accepted_samples if coordinator_stream else 0,
                persisted_samples=coordinator_stream.committed_samples if coordinator_stream else 0,
                queue_rejections=coordinator_stream.queue_rejections if coordinator_stream else 0,
                persistence_losses=coordinator_stream.persistence_losses if coordinator_stream else 0,
                queue_depth=coordinator_stream.queue_depth if coordinator_stream else 0,
                queue_capacity=(
                    coordinator_stream.queue_capacity
                    if coordinator_stream else DEFAULT_STREAM_QUEUE_CAPACITY
                ),
            ))
        recording_state = "Not recording"
        writer_status = "No database writer"
        elapsed_seconds = 0.0
        if self._bridge is not None:
            bridge_state = self._bridge.snapshot()[0]
            if self._finalization_unconfirmed:
                recording_state = "Failed; finalization unconfirmed"
                writer_status = self._last_error or "Final transaction was not acknowledged"
            elif coordinator_snapshot is not None and coordinator_snapshot.state is CoordinatorState.FAILED:
                recording_state = "Failed; finalization unconfirmed"
                writer_status = coordinator_snapshot.error or "Writer failed; durable completion not acknowledged"
            elif bridge_state is BridgeState.RECORDING and coordinator_snapshot is not None:
                statuses = {stream.state.value for stream in coordinator_snapshot.streams}
                incomplete = any(
                    stream.queue_rejections or stream.persistence_losses
                    for stream in coordinator_snapshot.streams
                )
                if incomplete:
                    recording_state = "Recording — timing data incomplete"
                else:
                    recording_state = "Recording" if statuses == {"recording"} else "Recording partially"
                writer_status = "Healthy" if coordinator_snapshot.writer_healthy else "Failed"
                if self._record_started_ns is not None:
                    elapsed_seconds = max(0, time.monotonic_ns() - self._record_started_ns) / 1_000_000_000
            elif bridge_state is BridgeState.FAILED:
                recording_state = "Failed; finalization unconfirmed"
                writer_status = coordinator_snapshot.error or "Writer failed; durable completion not acknowledged"
            elif bridge_state is BridgeState.FINALIZED:
                record = self._final_record or getattr(self._bridge, "_final_record", None)
                recording_state = str(record.status) if record is not None else "Finalization unconfirmed"
                writer_status = "Final transaction committed" if record is not None else "Finalization unconfirmed"
                elapsed_seconds = self._final_elapsed_seconds
            elif bridge_state is BridgeState.STARTING:
                recording_state = "Starting; awaiting stream acknowledgements"
                writer_status = "Starting"
        active_capture_slots = [
            stream.slot for stream in stream_views
            if stream.capture_state in {"starting", "running", "stopping"}
        ]
        if self._finalization_unconfirmed or recording_state.startswith("Failed"):
            capture_status_message = (
                "Recording finalization is unconfirmed; the experiment is not reported as saved. "
                "Synthetic capture is stopped or remains under user control; use Stop Both if sources are active."
            )
        elif self._auto_stop_pending:
            status = str(self._final_record.status) if self._final_record is not None else "finalized"
            capture_status_message = (
                f"Recording finalized as {status}. Both synthetic capture sources were asked to stop; "
                "waiting for shutdown acknowledgement."
            )
        elif self._auto_stop_result == "stopped":
            record = self._final_record
            failed_slots = [stream.slot for stream in stream_views if stream.capture_state == CaptureState.FAILED.value]
            if record is not None and str(record.status) == "completed" and record.data_complete and not failed_slots:
                capture_status_message = (
                    "Recording completed and saved. Both synthetic capture sources stopped automatically."
                )
            elif failed_slots:
                failed_text = ", ".join(f"Camera {slot}" for slot in failed_slots)
                capture_status_message = (
                    f"Recording finalized as {record.status if record is not None else 'unconfirmed'}; "
                    f"{failed_text} failed before shutdown and the remaining synthetic source stopped automatically. "
                    "Review the experiment status and completeness before interpreting its measurements."
                )
            else:
                status = str(record.status) if record is not None else "unconfirmed"
                capture_status_message = (
                    f"Recording finalized as {status}; both synthetic capture sources stopped automatically. "
                    "Review the experiment status and completeness before interpreting its measurements."
                )
        elif self._auto_stop_result == "failed":
            capture_status_message = (
                "Recording is finalized, but automatic source shutdown failed. "
                "Capture may still be running; use Stop Both to retry."
            )
        elif not active_capture_slots:
            capture_status_message = "Synthetic capture is stopped."
        elif self._bridge is not None and self._bridge.snapshot()[0] is BridgeState.FINALIZED:
            record = self._final_record or getattr(self._bridge, "_final_record", None)
            if record is not None and str(record.status) == "completed" and record.data_complete:
                capture_status_message = (
                    "Recording completed and saved. Synthetic capture is still running; "
                    "use Stop Both to stop the sources."
                )
            else:
                status = str(record.status) if record is not None else "unconfirmed"
                capture_status_message = (
                    f"Recording finalized as {status}. Synthetic capture is still running; "
                    "use Stop Both to stop the sources."
                )
        elif recording_state.startswith("Recording"):
            capture_status_message = (
                "Live capture continues independently; only timing samples accepted during this recording are saved."
            )
        else:
            capture_status_message = (
                "Synthetic capture is running. Events are counted live; they are not saved unless a recording is active."
            )
        view = SyntheticHarnessView(
            operation=self._operation,
            capture_status_message=capture_status_message,
            recording_state=recording_state,
            session_id=self._session_id,
            elapsed_seconds=elapsed_seconds,
            writer_status=writer_status,
            streams=(stream_views[0], stream_views[1]),
            last_error=self._last_error,
            automatic_capture_stop_pending=self._auto_stop_pending,
        )
        self.snapshot_ready.emit(view)

    def _remove_synthetic_database(self) -> None:
        root = self._temporary_root
        if root is None:
            return
        for child in root.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                import shutil
                shutil.rmtree(child)
        root.rmdir()
        self._temporary_root = None
        self._database = None
        self._repository = None

    def _safe_error(self, exc: BaseException) -> str:
        text = str(exc)
        if self._temporary_root is not None:
            text = text.replace(str(self._temporary_root), "<synthetic-temp>")
        return f"{type(exc).__name__}: {text[:240]}"


class _SyntheticStreamPanel(QGroupBox):
    def __init__(self, slot: str) -> None:
        super().__init__(f"Camera {slot} · synthetic")
        self.slot = slot
        layout = QVBoxLayout(self)
        self.preview = QLabel(
            f"SYNTHETIC STREAM {slot}\nTiming events only\nNo camera opened · no images created"
        )
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(96)
        self.preview.setStyleSheet(
            "QLabel { background: palette(base); color: palette(text); border: 1px solid palette(mid); }"
        )
        self.state_label = QLabel("Capture: stopped")
        self.state_label.setStyleSheet("color: palette(text); font-weight: 600;")
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.state_label)
        self.metric_labels: dict[str, QLabel] = {}
        self.metric_title_labels: list[QLabel] = []
        metric_content = QWidget(self)
        grid = QGridLayout(metric_content)
        grid.setContentsMargins(4, 2, 4, 2)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        for row, (key, title) in enumerate((
            ("mode", "Synthetic capture mode"),
            ("fps", "Simulated capture events/s"),
            ("frames", "Live capture events since Start Both"),
            ("failures", "Live read failures since Start Both"),
            ("recording", "Recording stream state"),
            ("accepted", "Timing samples accepted during recording"),
            ("persisted", "Timing samples committed to SQLite"),
            ("rejected", "Timing samples rejected by bounded queue"),
            ("persistence_losses", "Timing samples lost after writer failure"),
            ("queue", "Recording queue depth / capacity"),
        )):
            label = QLabel(title)
            label.setStyleSheet("color: palette(text);")
            label.setWordWrap(True)
            value = QLabel("—")
            value.setStyleSheet("color: palette(text); font-weight: 600;")
            value.setWordWrap(True)
            grid.addWidget(label, row, 0)
            grid.addWidget(value, row, 1)
            self.metric_title_labels.append(label)
            self.metric_labels[key] = value
        self.metrics_scroll_area = QScrollArea(self)
        self.metrics_scroll_area.setWidgetResizable(True)
        self.metrics_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.metrics_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.metrics_scroll_area.setMinimumHeight(72)
        self.metrics_scroll_area.setWidget(metric_content)
        layout.addWidget(self.metrics_scroll_area, 2)

    def update_view(self, view: SyntheticStreamView) -> None:
        self.state_label.setText(f"Capture: {view.capture_state}")
        self.metric_labels["mode"].setText(view.capture_mode)
        self.metric_labels["fps"].setText(f"{view.simulated_capture_fps:.1f}")
        self.metric_labels["frames"].setText(str(view.frame_count))
        self.metric_labels["failures"].setText(str(view.read_failures))
        self.metric_labels["recording"].setText(view.recording_state)
        self.metric_labels["accepted"].setText(str(view.accepted_samples))
        self.metric_labels["persisted"].setText(str(view.persisted_samples))
        self.metric_labels["rejected"].setText(str(view.queue_rejections))
        self.metric_labels["persistence_losses"].setText(str(view.persistence_losses))
        self.metric_labels["queue"].setText(f"{view.queue_depth} / {view.queue_capacity}")


class SyntheticRecordingWindow(QWidget):
    """Standalone fake-camera UI. Not part of LiveWorkbenchWindow or `celab gui`."""

    request_start_both = Signal(int, int)
    request_record_both = Signal(float, bool)
    request_stop_recording = Signal()
    request_stop_stream = Signal(str)
    request_stop_both = Signal()
    request_inspect_group = Signal()
    request_shutdown = Signal()

    def __init__(
        self,
        *,
        source_factory: Callable[..., SyntheticCaptureSource] = SyntheticCaptureSource,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Camera Exploration Lab — Synthetic Recording Harness")
        self.resize(1100, 760)
        self._shutdown_requested = threading.Event()
        self._close_complete = False
        self._close_pending = False
        self._pending_action: str | None = None
        self._latest_view: SyntheticHarnessView | None = None
        self._explorer_dialog: GroupedExperimentExplorerDialog | None = None
        self._stream_panels = (_SyntheticStreamPanel("A"), _SyntheticStreamPanel("B"))

        self._thread = QThread(self)
        self._thread.setObjectName("cel-synthetic-recording-orchestration")
        self._worker = _SyntheticRecordingWorker(
            self._shutdown_requested, source_factory=source_factory,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.initialize)
        self.request_start_both.connect(self._worker.start_both, Qt.ConnectionType.QueuedConnection)
        self.request_record_both.connect(self._worker.start_recording, Qt.ConnectionType.QueuedConnection)
        self.request_stop_recording.connect(self._worker.stop_recording, Qt.ConnectionType.QueuedConnection)
        self.request_stop_stream.connect(self._worker.stop_stream, Qt.ConnectionType.QueuedConnection)
        self.request_stop_both.connect(self._worker.stop_both, Qt.ConnectionType.QueuedConnection)
        self.request_inspect_group.connect(self._worker.inspect_group, Qt.ConnectionType.QueuedConnection)
        self.request_shutdown.connect(self._worker.request_shutdown, Qt.ConnectionType.QueuedConnection)
        self._worker.snapshot_ready.connect(self._on_snapshot)
        self._worker.action_finished.connect(self._on_action_finished)
        self._worker.action_failed.connect(self._on_action_failed)
        self._worker.shutdown_finished.connect(self._on_shutdown_finished)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

        page = QVBoxLayout(self)
        page.setContentsMargins(12, 10, 12, 12)
        page.setSpacing(8)
        title = QLabel("Synthetic Grouped Recording Demonstration")
        title.setStyleSheet("color: palette(text); font-size: 18px; font-weight: 600;")
        page.addWidget(title)
        warning = QLabel(
            "Development-only: both sources are fake timing generators. This window does not discover or open cameras, "
            "create image frames, or use the normal application database."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: palette(text); font-weight: 600;")
        page.addWidget(warning)
        self.acknowledgement = QCheckBox("I acknowledge this harness is synthetic-only and uses a disposable database.")
        self.acknowledgement.setStyleSheet("color: palette(text);")
        self.acknowledgement.toggled.connect(self._update_controls)
        page.addWidget(self.acknowledgement)

        options = QFormLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.setHorizontalSpacing(8)
        options.setVerticalSpacing(4)
        self.rate_a = QComboBox()
        self.rate_a.addItem("320×240 GRBG · 100 synthetic events/s", 100)
        self.rate_a.addItem("320×240 GRBG · 187 synthetic events/s", 187)
        options.addRow("Camera A simulated mode", self.rate_a)
        self.rate_b = QComboBox()
        self.rate_b.addItem("1280×720 MJPG · 24 synthetic events/s", 24)
        self.rate_b.addItem("1280×720 MJPG · 30 synthetic events/s", 30)
        options.addRow("Camera B simulated mode", self.rate_b)
        self.duration = QComboBox()
        for seconds in (1, 5, 10):
            self.duration.addItem(f"{seconds} s", float(seconds))
        options.addRow("Target duration", self.duration)
        page.addLayout(options)

        self.keep_capture_running_checkbox = QCheckBox(
            "Keep synthetic capture running after recording finishes"
        )
        self.keep_capture_running_checkbox.setChecked(False)
        self.keep_capture_running_checkbox.setToolTip(
            "When unchecked, a naturally completed fixed-duration recording stops both fake capture sources "
            "after database finalization is acknowledged. Manual Stop Recording always leaves capture running."
        )
        self.keep_capture_running_checkbox.setStyleSheet("color: palette(text);")
        self.keep_capture_running_checkbox.toggled.connect(self._update_controls)
        page.addWidget(self.keep_capture_running_checkbox)

        buttons = QHBoxLayout()
        self.start_both_button = QPushButton("Start Both")
        self.record_both_button = QPushButton("Record Both")
        self.stop_recording_button = QPushButton("Stop Recording")
        self.stop_a_button = QPushButton("Stop A")
        self.stop_b_button = QPushButton("Stop B")
        self.stop_both_button = QPushButton("Stop Both")
        self.inspect_button = QPushButton("Inspect Synthetic Experiment")
        for button in (
            self.start_both_button, self.record_both_button, self.stop_recording_button,
            self.stop_a_button, self.stop_b_button, self.stop_both_button, self.inspect_button,
        ):
            buttons.addWidget(button)
        page.addLayout(buttons)
        self.start_both_button.clicked.connect(self._request_start)
        self.record_both_button.clicked.connect(self._request_record)
        self.stop_recording_button.clicked.connect(self._request_stop_recording)
        self.stop_a_button.clicked.connect(lambda: self._request_stop_stream("A"))
        self.stop_b_button.clicked.connect(lambda: self._request_stop_stream("B"))
        self.stop_both_button.clicked.connect(self._request_stop_both)
        self.inspect_button.clicked.connect(self._request_inspect)

        self.state_label = QLabel("Acknowledge synthetic-only mode before starting.")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: palette(text); font-weight: 600;")
        page.addWidget(self.state_label)
        self.capture_status_label = QLabel("Synthetic capture is stopped.")
        self.capture_status_label.setWordWrap(True)
        self.capture_status_label.setStyleSheet("color: palette(text);")
        page.addWidget(self.capture_status_label)
        header = QFormLayout()
        self.group_id_label = QLabel("—")
        self.group_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.elapsed_label = QLabel("0.0 s")
        self.writer_label = QLabel("No database writer")
        self.writer_label.setWordWrap(True)
        self.final_status_label = QLabel("Not started")
        for label in (self.group_id_label, self.elapsed_label, self.writer_label, self.final_status_label):
            label.setStyleSheet("color: palette(text);")
        header.addRow("Synthetic group experiment ID", self.group_id_label)
        header.addRow("Recording elapsed", self.elapsed_label)
        header.addRow("Writer / finalization", self.writer_label)
        header.addRow("Recording status", self.final_status_label)
        page.addLayout(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._stream_panels[0])
        self.splitter.addWidget(self._stream_panels[1])
        page.addWidget(self.splitter, 1)
        self.inspection_label = QLabel("No experiment inspected yet.")
        self.inspection_label.setWordWrap(True)
        self.inspection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.inspection_label.setStyleSheet("color: palette(text);")
        page.addWidget(self.inspection_label)

        self._update_controls()

    @Slot()
    def _request_start(self) -> None:
        self._set_requested("Start Both requested; awaiting synthetic source startup.")
        self.request_start_both.emit(int(self.rate_a.currentData()), int(self.rate_b.currentData()))

    @Slot()
    def _request_record(self) -> None:
        self._set_requested("Record Both requested; awaiting writer and stream-start acknowledgement.")
        self.request_record_both.emit(
            float(self.duration.currentData()),
            self.keep_capture_running_checkbox.isChecked(),
        )

    @Slot()
    def _request_stop_recording(self) -> None:
        self._set_requested("Stop Recording requested; awaiting durable finalization.")
        self.request_stop_recording.emit()

    def _request_stop_stream(self, slot: str) -> None:
        self._set_requested(f"Stop {slot} requested; awaiting that synthetic stream's acknowledgement.")
        self.request_stop_stream.emit(slot)

    @Slot()
    def _request_stop_both(self) -> None:
        self._set_requested("Stop Both requested; finalizing recording before stopping sources.")
        self.request_stop_both.emit()

    @Slot()
    def _request_inspect(self) -> None:
        if self._pending_action is not None or self._close_pending or self._close_complete:
            return
        self._set_requested("Inspecting the isolated synthetic group…")
        self.inspection_label.setText("Loading committed samples and preparing bounded plots…")
        self.request_inspect_group.emit()

    def _set_requested(self, message: str) -> None:
        self._pending_action = message
        self.state_label.setText(message)
        self._update_controls()

    @Slot(object)
    def _on_snapshot(self, view: SyntheticHarnessView) -> None:
        self._latest_view = view
        if self._pending_action is None and not self._close_pending:
            self.state_label.setText(view.operation)
        self.capture_status_label.setText(view.capture_status_message)
        self.group_id_label.setText(view.session_id or "—")
        self.elapsed_label.setText(f"{view.elapsed_seconds:.1f} s")
        self.writer_label.setText(view.writer_status)
        self.final_status_label.setText(view.recording_state)
        for panel, stream in zip(self._stream_panels, view.streams, strict=True):
            panel.update_view(stream)
        self._update_controls()

    @Slot(str, object)
    def _on_action_finished(self, action: str, result: object) -> None:
        self._pending_action = None
        if action == "inspect" and self._shutdown_requested.is_set():
            self._update_controls()
            return
        if action == "inspect":
            self.inspection_label.setText(self._format_inspection(result))
            if isinstance(result, dict) and isinstance(result.get("grouped_analysis"), GroupedExperimentAnalysis):
                if self._explorer_dialog is not None:
                    self._explorer_dialog.close()
                self._explorer_dialog = GroupedExperimentExplorerDialog(result["grouped_analysis"], self)
                self._explorer_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                self._explorer_dialog.show()
        elif action == "record_both" and isinstance(result, dict):
            if result.get("cancelled"):
                self.state_label.setText("Recording startup cancelled during shutdown.")
            elif result.get("final_status"):
                self.state_label.setText(f"Grouped startup ended with status {result['final_status']}.")
            elif result.get("fully_started"):
                self.state_label.setText("Recording acknowledged: both synthetic streams are attached to the writer.")
            else:
                started = ", ".join(result.get("started_slots", ())) or "none"
                self.state_label.setText(f"Partial recording acknowledged; active synthetic slot(s): {started}.")
        elif action == "stop_recording" and isinstance(result, dict):
            self.state_label.setText(
                f"Finalization transaction committed with status {result.get('status')}; "
                f"data complete={result.get('data_complete', 'unknown')}."
            )
        elif action == "auto_stop" and isinstance(result, dict):
            self.state_label.setText(
                f"Recording finalized as {result.get('recording_status')}; "
                "both synthetic capture sources stopped automatically."
            )
        else:
            self.state_label.setText(f"{action.replace('_', ' ').title()} acknowledged: {result}")
        self._update_controls()

    @Slot(str, str)
    def _on_action_failed(self, action: str, message: str) -> None:
        self._pending_action = None
        if action == "inspect" and self._shutdown_requested.is_set():
            self._update_controls()
            return
        if action == "auto_stop":
            self.state_label.setText(f"Automatic capture stop needs attention: {message}")
        else:
            self.state_label.setText(f"{action.replace('_', ' ').title()} failed: {message}")
        self._update_controls()

    @Slot(bool, str)
    def _on_shutdown_finished(self, complete: bool, message: str) -> None:
        if not complete:
            self._close_pending = True
            self.state_label.setText(f"Shutdown waiting: {message}")
            return
        if "unresolved" in message.lower():
            self.state_label.setText(message)
            QMessageBox.warning(self, "Synthetic recording finalization unresolved", message)
        else:
            self.state_label.setText(message)
        self._thread.quit()

    @Slot()
    def _on_thread_finished(self) -> None:
        self._close_complete = True
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.close)

    @staticmethod
    def _format_inspection(result: object) -> str:
        if not isinstance(result, dict):
            return "Synthetic group inspection returned no details."
        stream_text = "; ".join(
            f"{item['slot']}: {item['camera_model']}, {item['status']}, "
            f"{item['persisted_samples']} persisted, {item['failures']} read failures, "
            f"data_complete={item['data_complete']}"
            for item in result["streams"]
        )
        recovery = result.get("read_only_recovery")
        recovery_text = ""
        if isinstance(recovery, dict):
            findings = recovery.get("findings", ())
            finding_text = ", ".join(
                f"{item['classification']} ({item['status']})" for item in findings
            ) or "no unresolved runs found"
            recovery_text = (
                f" · Read-only recovery inspection: schema v{recovery['schema_version']}, "
                f"database lock {recovery['lock_state']}, {finding_text}. No recovery action was taken."
            )
        return (
            f"Saved synthetic group {result['session_id']} · status={result['status']} · "
            f"data_complete={result['data_complete']} · streams: {stream_text}{recovery_text}"
        )

    def _update_controls(self, _checked: bool = False) -> None:
        acknowledged = self.acknowledgement.isChecked()
        view = self._latest_view
        states = {stream.slot: stream.capture_state for stream in view.streams} if view else {"A": "stopped", "B": "stopped"}
        active = {slot for slot, state in states.items() if state in {"starting", "running", "stopping"}}
        recording_state = view.recording_state if view else "Not recording"
        recording_active = recording_state.startswith("Recording") or recording_state.startswith("Starting")
        busy = self._pending_action is not None or self._close_pending or self._close_complete
        self.start_both_button.setEnabled(acknowledged and not busy and not active)
        auto_stop_pending = bool(view and view.automatic_capture_stop_pending)
        self.record_both_button.setEnabled(
            acknowledged and not busy and bool(active) and not recording_active and not auto_stop_pending
        )
        self.stop_recording_button.setEnabled(acknowledged and not busy and recording_active)
        self.stop_a_button.setEnabled(acknowledged and not busy and "A" in active)
        self.stop_b_button.setEnabled(acknowledged and not busy and "B" in active)
        self.stop_both_button.setEnabled(acknowledged and not busy and bool(active or recording_active))
        self.inspect_button.setEnabled(acknowledged and not busy and bool(view and view.session_id))
        self.rate_a.setEnabled(acknowledged and not busy and not active)
        self.rate_b.setEnabled(acknowledged and not busy and not active)
        self.duration.setEnabled(acknowledged and not busy and not recording_active)
        self.keep_capture_running_checkbox.setEnabled(
            acknowledged and not busy and not recording_active and not auto_stop_pending
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Keep the three supported Bazzite desktop widths side by side. At
        # genuinely narrow widths, stack the cards; their metrics remain
        # accessible through each card's own vertical scroll area.
        orientation = Qt.Orientation.Horizontal if self.width() >= 900 else Qt.Orientation.Vertical
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._close_complete:
            event.accept()
            return
        if self._explorer_dialog is not None:
            self._explorer_dialog.close()
        event.ignore()
        if self._close_pending:
            return
        self._close_pending = True
        self._shutdown_requested.set()
        self.state_label.setText("Window close requested; waiting for safe asynchronous shutdown.")
        self._update_controls()
        self.request_shutdown.emit()


def parse_demo_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cel-synthetic-recording-demo",
        description="Run a disposable, fake-camera-only grouped recording demonstration.",
    )
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    parser.add_argument("--acknowledge-synthetic-only", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parse_demo_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = SyntheticRecordingWindow()
    window.show()
    return app.exec()
