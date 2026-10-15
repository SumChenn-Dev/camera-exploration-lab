"""Nonblocking bridge from capture-worker timing callbacks to grouped storage.

This layer carries only ``CaptureTimingEvent`` metadata. It is not imported by
the desktop UI and does not create databases, migrate schemas, or open cameras.
Lifecycle calls wait for coordinator/writer acknowledgements and must be made
from an orchestration thread, never from a Qt callback or capture callback.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping, Protocol

from .capture import (
    CaptureSnapshot,
    CaptureState,
    CaptureTimingEvent,
    CaptureTimingSubscription,
)
from .grouped_recording import (
    GroupedExperimentRecord,
)
from .grouped_recording_coordinator import (
    CoordinatorState,
    CoordinatedStreamState,
    GroupedRecordingCoordinator,
)
from .models import utc_now_iso


class CaptureAdapterError(RuntimeError):
    """The capture event adapter could not safely attach or detach."""


class AdapterShutdownTimeout(CaptureAdapterError):
    """An already-running callback did not leave the adapter before timeout."""


class GroupedRecordingPersistenceError(CaptureAdapterError):
    """The coordinator writer failed; no successful finalization is available."""


class AdapterState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    ACTIVE = "active"
    DETACHING = "detaching"
    DETACHED = "detached"
    FAILED = "failed"


class CaptureTimingSource(Protocol):
    """Small worker surface required by the adapter; fake sources can implement it."""

    def snapshot(self) -> CaptureSnapshot: ...

    def subscribe_timing_listener(
        self,
        listener: Callable[[CaptureTimingEvent], None],
    ) -> CaptureTimingSubscription: ...


@dataclass(frozen=True, slots=True)
class CaptureAdapterSnapshot:
    slot: str
    state: AdapterState
    frame_events: int
    read_failure_events: int
    rejected_submissions: int
    ignored_events: int
    callback_errors: int
    callbacks_in_flight: int
    last_error_type: str | None


@dataclass(frozen=True, slots=True)
class StreamStartOutcome:
    slot: str
    started: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GroupedStartReport:
    streams: tuple[StreamStartOutcome, StreamStartOutcome]
    finalized_record: GroupedExperimentRecord | None = None

    @property
    def started_slots(self) -> tuple[str, ...]:
        return tuple(item.slot for item in self.streams if item.started)

    @property
    def failed_slots(self) -> tuple[str, ...]:
        return tuple(item.slot for item in self.streams if not item.started)

    @property
    def fully_started(self) -> bool:
        return all(item.started for item in self.streams)


class CaptureEventAdapter:
    """Bind one active capture worker to one grouped coordinator stream.

    Successful read callbacks pass the worker's monotonic timestamp and UTC
    marker unchanged to the coordinator. Each callback performs one bounded
    ``put_nowait`` submission; it never forwards a frame, waits for SQLite, or
    emits a per-frame Qt signal.
    """

    def __init__(
        self,
        slot: str,
        worker: CaptureTimingSource,
        coordinator: GroupedRecordingCoordinator,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        now_iso: Callable[[], str] = utc_now_iso,
    ) -> None:
        if slot not in {"A", "B"}:
            raise ValueError("slot must be A or B")
        self.slot = slot
        self.worker = worker
        self.coordinator = coordinator
        self._clock_ns = clock_ns
        self._now_iso = now_iso
        self._condition = threading.Condition()
        self._state = AdapterState.NEW
        self._active = False
        self._subscription: CaptureTimingSubscription | None = None
        self._callbacks_in_flight = 0
        self._frame_events = 0
        self._read_failure_events = 0
        self._rejected_submissions = 0
        self._ignored_events = 0
        self._callback_errors = 0
        self._last_error_type: str | None = None

    def start(self) -> None:
        """Persist the stream start event, then open its timing intake boundary."""
        with self._condition:
            if self._state is not AdapterState.NEW:
                raise CaptureAdapterError("capture adapter can only be started once")
            self._state = AdapterState.STARTING

        if self.worker.snapshot().state is not CaptureState.RUNNING:
            with self._condition:
                self._state = AdapterState.FAILED
            raise CaptureAdapterError("capture worker is not running")

        try:
            subscription = self.worker.subscribe_timing_listener(self._on_timing_event)
        except Exception:
            with self._condition:
                self._state = AdapterState.FAILED
            raise
        with self._condition:
            self._subscription = subscription

        try:
            # The coordinator's durable stream start is the event boundary.
            # Callbacks observed while that transaction is being acknowledged
            # are deliberately excluded; the first accepted read-return is
            # retained as the first-arrival marker.
            self.coordinator.start_stream(
                self.slot,
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )
            if self.worker.snapshot().state is not CaptureState.RUNNING:
                self.coordinator.fail_stream(
                    self.slot,
                    "capture_ended_during_start",
                    host_monotonic_ns=self._clock_ns(),
                    observed_at=self._now_iso(),
                )
                raise CaptureAdapterError("capture worker ended while its recording stream started")
        except Exception:
            subscription.close()
            with self._condition:
                self._subscription = None
                self._active = False
                self._state = AdapterState.FAILED
            raise

        with self._condition:
            self._active = True
            self._state = AdapterState.ACTIVE

    def _on_timing_event(self, event: CaptureTimingEvent) -> None:
        with self._condition:
            if not self._active:
                self._ignored_events += 1
                return
            if event.kind not in {"frame_arrival", "read_failure"}:
                self._ignored_events += 1
                return
            self._callbacks_in_flight += 1

        try:
            if event.kind == "frame_arrival":
                result = self.coordinator.submit_frame_arrival(
                    self.slot,
                    host_monotonic_ns=event.monotonic_ns,
                    observed_at=event.observed_at,
                )
                with self._condition:
                    self._frame_events += 1
            else:
                result = self.coordinator.submit_read_failure(
                    self.slot,
                    host_monotonic_ns=event.monotonic_ns,
                    observed_at=event.observed_at,
                )
                with self._condition:
                    self._read_failure_events += 1
            if not result.accepted and result.reason != "stream_not_recording":
                with self._condition:
                    self._rejected_submissions += 1
        except Exception as exc:
            # Timing delivery must never escape into or stall the capture loop.
            with self._condition:
                self._callback_errors += 1
                self._last_error_type = type(exc).__name__
        finally:
            with self._condition:
                self._callbacks_in_flight -= 1
                self._condition.notify_all()

    def detach(self, *, timeout: float = 2.0) -> None:
        """Close intake and wait boundedly for callbacks already in submission."""
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        with self._condition:
            self._active = False
            subscription = self._subscription
            self._subscription = None
            if self._state not in {AdapterState.DETACHED, AdapterState.FAILED}:
                self._state = AdapterState.DETACHING
        if subscription is not None:
            subscription.close()

        deadline = time.monotonic() + timeout
        with self._condition:
            while self._callbacks_in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterShutdownTimeout(
                        f"stream {self.slot} still has a timing callback in flight"
                    )
                self._condition.wait(remaining)
            if self._state is not AdapterState.FAILED:
                self._state = AdapterState.DETACHED

    def snapshot(self) -> CaptureAdapterSnapshot:
        with self._condition:
            return CaptureAdapterSnapshot(
                slot=self.slot,
                state=self._state,
                frame_events=self._frame_events,
                read_failure_events=self._read_failure_events,
                rejected_submissions=self._rejected_submissions,
                ignored_events=self._ignored_events,
                callback_errors=self._callback_errors,
                callbacks_in_flight=self._callbacks_in_flight,
                last_error_type=self._last_error_type,
            )


class BridgeState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RECORDING = "recording"
    FINALIZED = "finalized"
    FAILED = "failed"


class GroupedCaptureRecordingBridge:
    """Orchestrate two adapters around the existing grouped coordinator.

    This is deliberately not wired to ``MultiCameraWorkspace``. Its synchronous
    lifecycle methods are for a future non-GUI orchestration worker and the
    synthetic developer harness only. It owns no camera handles or SQLite
    connection and never stops a camera when recording ends.
    """

    def __init__(
        self,
        coordinator: GroupedRecordingCoordinator,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        now_iso: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.coordinator = coordinator
        self._clock_ns = clock_ns
        self._now_iso = now_iso
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._state = BridgeState.CREATED
        self._adapters: dict[str, CaptureEventAdapter] = {}
        self._final_record: GroupedExperimentRecord | None = None

    def start_recording(
        self,
        workers: Mapping[str, CaptureTimingSource | None],
    ) -> GroupedStartReport:
        """Arm two already-live workers; a missing/failed slot stays explicit."""
        with self._operation_lock:
            return self._start_recording(workers)

    def _start_recording(
        self,
        workers: Mapping[str, CaptureTimingSource | None],
    ) -> GroupedStartReport:
        with self._lock:
            if self._state is not BridgeState.CREATED:
                raise CaptureAdapterError("grouped recording can only be started once")
            self._state = BridgeState.STARTING
        try:
            self.coordinator.start()
        except Exception:
            with self._lock:
                self._state = BridgeState.FAILED
            raise

        outcomes: list[StreamStartOutcome] = []
        for slot in ("A", "B"):
            if self.coordinator.snapshot().state is not CoordinatorState.RUNNING:
                outcomes.append(StreamStartOutcome(slot, False, "persistence_unavailable"))
                continue
            worker = workers.get(slot)
            if worker is None or self._worker_state(worker) is not CaptureState.RUNNING:
                self.coordinator.fail_stream_start(
                    slot,
                    "capture_not_running",
                    host_monotonic_ns=self._clock_ns(),
                    observed_at=self._now_iso(),
                )
                outcomes.append(StreamStartOutcome(slot, False, "capture_not_running"))
                continue

            adapter = CaptureEventAdapter(
                slot, worker, self.coordinator,
                clock_ns=self._clock_ns,
                now_iso=self._now_iso,
            )
            self._adapters[slot] = adapter
            try:
                adapter.start()
            except Exception as exc:
                try:
                    adapter.detach(timeout=2.0)
                except AdapterShutdownTimeout:
                    pass
                self._fail_unstarted_stream(slot)
                outcomes.append(StreamStartOutcome(slot, False, type(exc).__name__))
            else:
                outcomes.append(StreamStartOutcome(slot, True))

        started = any(item.started for item in outcomes)
        if not started and self.coordinator.snapshot().state is CoordinatorState.RUNNING:
            self._final_record = self.coordinator.finalize(reason_code="no_stream_started")
            with self._lock:
                self._state = BridgeState.FINALIZED
        elif self.coordinator.snapshot().state is CoordinatorState.FAILED:
            with self._lock:
                self._state = BridgeState.FAILED
        else:
            with self._lock:
                self._state = BridgeState.RECORDING
        return GroupedStartReport(tuple(outcomes), self._final_record)

    @staticmethod
    def _worker_state(worker: CaptureTimingSource) -> CaptureState | None:
        try:
            return worker.snapshot().state
        except Exception:
            return None

    def _fail_unstarted_stream(self, slot: str) -> None:
        snapshot = self.coordinator.snapshot()
        if snapshot.state is not CoordinatorState.RUNNING:
            return
        stream = next(item for item in snapshot.streams if item.slot == slot)
        if stream.state is CoordinatedStreamState.PENDING:
            self.coordinator.fail_stream_start(
                slot,
                "capture_listener_attach_failed",
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )
        elif stream.state is CoordinatedStreamState.RECORDING:
            self.coordinator.fail_stream(
                slot,
                "capture_adapter_start_failed",
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )

    def capture_ended(
        self,
        slot: str,
        *,
        failed: bool,
        reason_code: str = "capture_stream_ended",
        timeout: float = 2.0,
    ) -> CoordinatedStreamState | None:
        with self._operation_lock:
            return self._capture_ended(
                slot, failed=failed, reason_code=reason_code, timeout=timeout
            )

    def _capture_ended(
        self,
        slot: str,
        *,
        failed: bool,
        reason_code: str,
        timeout: float,
    ) -> CoordinatedStreamState | None:
        """Detach one stream and persist its terminal state; sibling stays live."""
        if slot not in {"A", "B"}:
            raise ValueError("slot must be A or B")
        adapter = self._adapters.get(slot)
        if adapter is not None:
            adapter.detach(timeout=timeout)
        coordinator_state = self.coordinator.snapshot().state
        if coordinator_state is CoordinatorState.FAILED:
            with self._lock:
                self._state = BridgeState.FAILED
            raise GroupedRecordingPersistenceError(
                "group writer failed; stream termination cannot be durably finalized"
            )
        if coordinator_state is not CoordinatorState.RUNNING:
            return None
        stream = next(item for item in self.coordinator.snapshot().streams if item.slot == slot)
        if stream.state is CoordinatedStreamState.PENDING:
            self.coordinator.fail_stream_start(
                slot,
                reason_code,
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )
            return CoordinatedStreamState.FAILED
        if stream.state is not CoordinatedStreamState.RECORDING:
            return stream.state
        if failed:
            self.coordinator.fail_stream(
                slot,
                reason_code,
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )
        else:
            self.coordinator.stop_stream(
                slot,
                reason_code=reason_code,
                host_monotonic_ns=self._clock_ns(),
                observed_at=self._now_iso(),
            )
        return next(item.state for item in self.coordinator.snapshot().streams if item.slot == slot)

    def stop_recording(
        self,
        *,
        reason_code: str = "manual_stop",
        timeout: float = 10.0,
    ) -> GroupedExperimentRecord:
        """Finalize timing while leaving both capture workers and previews running."""
        with self._operation_lock:
            return self._stop_recording(reason_code=reason_code, timeout=timeout)

    def _stop_recording(
        self,
        *,
        reason_code: str,
        timeout: float,
    ) -> GroupedExperimentRecord:
        if self._final_record is not None:
            return self._final_record
        self._detach_all(timeout=timeout)
        snapshot = self.coordinator.snapshot()
        if snapshot.state is CoordinatorState.FAILED:
            with self._lock:
                self._state = BridgeState.FAILED
            raise GroupedRecordingPersistenceError(
                "group writer failed; recording was not finalized successfully"
            )
        if snapshot.state is not CoordinatorState.RUNNING:
            raise CaptureAdapterError("grouped recording is not active")
        self._final_record = self.coordinator.stop_both(
            reason_code=reason_code,
            timeout=timeout,
        )
        with self._lock:
            self._state = BridgeState.FINALIZED
        return self._final_record

    def shutdown(self, *, timeout: float = 2.0) -> bool:
        """Close sample intake before bounded coordinator cancellation/finalization."""
        with self._operation_lock:
            return self._shutdown(timeout=timeout)

    def _shutdown(self, *, timeout: float) -> bool:
        try:
            self._detach_all(timeout=timeout)
        except AdapterShutdownTimeout:
            return False
        result = self.coordinator.cancel(timeout=timeout)
        state = self.coordinator.snapshot().state
        with self._lock:
            self._state = BridgeState.FINALIZED if state is CoordinatorState.FINALIZED else BridgeState.FAILED
        return result

    def _detach_all(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for slot in ("A", "B"):
            adapter = self._adapters.get(slot)
            if adapter is not None:
                adapter.detach(timeout=max(0.0, deadline - time.monotonic()))

    def snapshot(self) -> tuple[BridgeState, tuple[CaptureAdapterSnapshot, ...]]:
        with self._lock:
            adapters = tuple(self._adapters[slot].snapshot() for slot in ("A", "B") if slot in self._adapters)
            return self._state, adapters
