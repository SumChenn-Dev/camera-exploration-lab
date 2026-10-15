"""Synthetic-testable coordinator for schema-v3 grouped timing recordings.

This module deliberately has no capture, Qt, OpenCV, or GUI imports. Producers
submit small host-timing records through bounded per-stream queues; one daemon
writer thread owns all post-lease SQLite writes. It is not connected to the
live application in this milestone.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from .grouped_recording import (
    GroupStatus,
    GroupedExperimentRecord,
    GroupedExperimentRepository,
    RecordingLease,
    StreamStatus,
)
from .models import MeasurementSample, StreamScope, utc_now_iso


DEFAULT_STREAM_QUEUE_CAPACITY = 2048
DEFAULT_BATCH_SIZE = 128
MAX_CONTROL_COMMANDS = 16
WRITER_POLL_SECONDS = 0.02
COMMAND_TIMEOUT_SECONDS = 10.0


class CoordinatorError(RuntimeError):
    """Base error for the grouped recording coordinator."""


class CoordinatorStateError(CoordinatorError):
    """An operation is invalid for the current coordinator/stream state."""


class CoordinatorTimeoutError(CoordinatorError):
    """A writer command did not complete before its caller's deadline."""


class CoordinatorState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    FAILED = "failed"
    FINALIZED = "finalized"


class CoordinatedStreamState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    accepted: bool
    slot: str
    sample_index: int | None
    queue_depth: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StreamCoordinatorSnapshot:
    slot: str
    benchmark_id: str
    state: CoordinatedStreamState
    queue_depth: int
    queue_capacity: int
    queue_high_water: int
    accepted_samples: int
    committed_samples: int
    queue_rejections: int
    persistence_losses: int
    observed_frames: int
    observed_read_failures: int
    first_host_arrival_elapsed_ns: int | None
    last_host_arrival_elapsed_ns: int | None
    start_elapsed_ns: int | None
    end_elapsed_ns: int | None


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    state: CoordinatorState
    session_id: str
    writer_healthy: bool
    lease_held: bool
    writer_cpu_ns: int
    streams: tuple[StreamCoordinatorSnapshot, StreamCoordinatorSnapshot]
    error: str | None = None


@dataclass(slots=True)
class _QueuedMeasurement:
    sample: MeasurementSample


@dataclass(slots=True)
class _Command:
    operation: str
    slot: str | None = None
    elapsed_ns: int | None = None
    observed_at: str | None = None
    reason_code: str | None = None
    target_status: StreamStatus | None = None
    requested_group_status: GroupStatus | None = None
    event_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    result: GroupedExperimentRecord | None = None


_COUNTERS = (
    "capture_frame_count",
    "read_failure_count",
    "timing_queue_loss_count",
    "persistence_loss_count",
)


class GroupedRecordingCoordinator:
    """Coordinate two independent timing streams using one SQLite writer.

    The only producer-side work is a short state lock, timestamp/sample
    construction, and ``Queue.put_nowait``. No producer waits for SQLite or
    queue capacity. Lifecycle methods are synchronous orchestration methods;
    future GUI code must call them outside the Qt event thread.
    """

    def __init__(
        self,
        repository: GroupedExperimentRepository,
        session_id: str,
        *,
        queue_capacity: int = DEFAULT_STREAM_QUEUE_CAPACITY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        now_iso: Callable[[], str] = utc_now_iso,
        command_timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int) or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1 or batch_size > 1024:
            raise ValueError("batch_size must be between 1 and 1024")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        group = repository.get_group(session_id)
        if group is None:
            raise CoordinatorStateError("grouped experiment does not exist")
        if group.status != GroupStatus.PENDING:
            raise CoordinatorStateError("coordinator requires a pending grouped experiment")
        self.repository = repository
        self.session_id = session_id
        self._group = group
        self._run_ids = {stream.slot: stream.benchmark_id for stream in group.streams}
        self._timing_methods = {stream.slot: stream.timing_method for stream in group.streams}
        if tuple(sorted(self._run_ids)) != ("A", "B"):
            raise CoordinatorStateError("group must contain slots A and B")
        self._target_seconds = group.duration_target_seconds
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._clock_ns = clock_ns
        self._now_iso = now_iso
        self._command_timeout = command_timeout_seconds
        self._epoch_ns = clock_ns()

        self._queues = {
            slot: queue.Queue[_QueuedMeasurement](maxsize=queue_capacity)
            for slot in ("A", "B")
        }
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=MAX_CONTROL_COMMANDS)
        self._pending_commands: list[_Command] = []
        self._active_command: _Command | None = None
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._progress = threading.Condition(self._lock)
        self._state = CoordinatorState.CREATED
        self._stream_state = {
            slot: CoordinatedStreamState.PENDING for slot in ("A", "B")
        }
        self._terminal_commands: dict[str, _Command | None] = {
            slot: None for slot in ("A", "B")
        }
        self._finalize_command: _Command | None = None
        self._accepting = {slot: False for slot in ("A", "B")}
        self._next_sequence = {slot: 0 for slot in ("A", "B")}
        self._previous_arrival_ns: dict[str, int | None] = {slot: None for slot in ("A", "B")}
        self._first_arrival: dict[str, int | None] = {slot: None for slot in ("A", "B")}
        self._last_arrival: dict[str, int | None] = {slot: None for slot in ("A", "B")}
        self._start_elapsed: dict[str, int | None] = {slot: None for slot in ("A", "B")}
        self._end_elapsed: dict[str, int | None] = {slot: None for slot in ("A", "B")}
        self._high_water = {slot: 0 for slot in ("A", "B")}
        self._accepted = {slot: 0 for slot in ("A", "B")}
        self._committed = {slot: 0 for slot in ("A", "B")}
        self._committed_frame_arrivals = {slot: 0 for slot in ("A", "B")}
        self._committed_read_failures = {slot: 0 for slot in ("A", "B")}
        self._observed_frames = {slot: 0 for slot in ("A", "B")}
        self._observed_failures = {slot: 0 for slot in ("A", "B")}
        self._queue_rejections = {slot: 0 for slot in ("A", "B")}
        self._persistence_losses = {slot: 0 for slot in ("A", "B")}
        self._counter_totals = {
            slot: {counter: 0 for counter in _COUNTERS} for slot in ("A", "B")
        }
        self._counter_persisted = {
            slot: {counter: 0 for counter in _COUNTERS} for slot in ("A", "B")
        }
        self._writer_error: str | None = None
        self._writer_healthy = True
        self._writer_cpu_ns = 0
        self._lease: RecordingLease | None = None
        self._thread: threading.Thread | None = None
        self._thread_closed = threading.Event()
        self._final_record: GroupedExperimentRecord | None = None
        self._last_turn = "B"

    def start(self) -> None:
        """Acquire the database lease, then start the single writer thread."""
        with self._lock:
            if self._state != CoordinatorState.CREATED:
                raise CoordinatorStateError("coordinator can only be started once")
            lease = self.repository.acquire_recording_lease(
                self.session_id,
                owner_pid=os.getpid(),
                owner_process_start_ticks=_process_start_ticks(),
                acquired_at=self._now_iso(),
            )
            self._lease = lease
            self._state = CoordinatorState.RUNNING
            self._thread = threading.Thread(
                target=self._writer_main,
                name=f"cel-group-writer-{self.session_id[:8]}",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._state = CoordinatorState.FAILED
                self._writer_healthy = False
                self.repository.release_recording_lease(lease.owner_token)
                self._lease = None
                raise
            started_event = _Command(
                "event",
                elapsed_ns=0,
                observed_at=self._now_iso(),
                event_type="recording_coordinator_started",
                details={"clock_epoch_id": self._group.clock_epoch_id},
            )
            self._enqueue_command_locked(started_event)
        self._wait_command(started_event, None)

    def start_stream(
        self,
        slot: str,
        *,
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
        timeout: float | None = None,
    ) -> None:
        slot = _require_slot(slot)
        elapsed_ns, wall = self._event_time(host_monotonic_ns, observed_at)
        with self._lock:
            self._require_running()
            if self._stream_state[slot] != CoordinatedStreamState.PENDING:
                raise CoordinatorStateError(f"stream {slot} is not pending")
            command = _Command("start", slot, elapsed_ns, wall)
            self._stream_state[slot] = CoordinatedStreamState.STARTING
            try:
                self._enqueue_command_locked(command)
            except BaseException:
                self._stream_state[slot] = CoordinatedStreamState.PENDING
                raise
        self._wait_command(command, timeout)

    def fail_stream_start(
        self,
        slot: str,
        reason_code: str = "capture_start_failed",
        *,
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
        timeout: float | None = None,
    ) -> None:
        slot = _require_slot(slot)
        _validate_reason_code(reason_code)
        elapsed_ns, wall = self._event_time(host_monotonic_ns, observed_at)
        with self._lock:
            self._require_running()
            if self._stream_state[slot] != CoordinatedStreamState.PENDING:
                raise CoordinatorStateError(f"stream {slot} is not awaiting startup")
            command = self._queue_terminal_locked(
                slot, elapsed_ns, wall, reason_code, StreamStatus.FAILED
            )
        self._wait_command(command, timeout)

    def submit_frame_arrival(
        self,
        slot: str,
        *,
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
    ) -> SubmissionResult:
        """Submit one OpenCV read-return arrival without waiting for capacity."""
        slot = _require_slot(slot)
        host = self._clock_ns() if host_monotonic_ns is None else _nonnegative_int(
            "host_monotonic_ns", host_monotonic_ns
        )
        wall = observed_at or self._now_iso()
        with self._lock:
            if self._state == CoordinatorState.FAILED and self._stream_state[slot] in {
                CoordinatedStreamState.RECORDING, CoordinatedStreamState.STOPPING,
                CoordinatedStreamState.FAILED,
            }:
                return self._count_after_writer_failure(slot, frame=True)
            if self._state != CoordinatorState.RUNNING or not self._accepting[slot]:
                return SubmissionResult(False, slot, None, self._queues[slot].qsize(), "stream_not_recording")
            elapsed_ns = self._elapsed_from(host)
            previous = self._previous_arrival_ns[slot]
            if previous is not None and host < previous:
                raise ValueError("host monotonic timestamps must not move backwards within a stream")
            sequence = self._next_sequence[slot]
            self._next_sequence[slot] += 1
            interval_ns = host - previous if previous is not None else None
            self._previous_arrival_ns[slot] = host
            if self._first_arrival[slot] is None:
                self._first_arrival[slot] = elapsed_ns
            self._last_arrival[slot] = elapsed_ns
            self._observed_frames[slot] += 1
            self._counter_totals[slot]["capture_frame_count"] += 1
            sample = MeasurementSample(
                benchmark_id=self._run_ids[slot],
                metric="frame_arrival",
                value=1.0,
                unit="frame",
                stream_scope=StreamScope.CAPTURE,
                observed_at=wall,
                elapsed_seconds=elapsed_ns / 1_000_000_000,
                sample_index=sequence,
                interval_ns=interval_ns,
                method=self._timing_methods[slot],
                elapsed_ns=elapsed_ns,
                # OpenCV read-return timing does not provide driver timestamps.
                source_timestamp_ns=None,
                source_sequence=None,
                source_clock=None,
            )
            return self._enqueue_sample_locked(slot, sample, sequence)

    def submit_read_failure(
        self,
        slot: str,
        *,
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
    ) -> SubmissionResult:
        """Record a host-observed read failure, separate from queue loss."""
        slot = _require_slot(slot)
        host = self._clock_ns() if host_monotonic_ns is None else _nonnegative_int(
            "host_monotonic_ns", host_monotonic_ns
        )
        wall = observed_at or self._now_iso()
        with self._lock:
            if self._state == CoordinatorState.FAILED and self._stream_state[slot] in {
                CoordinatedStreamState.RECORDING, CoordinatedStreamState.STOPPING,
                CoordinatedStreamState.FAILED,
            }:
                return self._count_after_writer_failure(slot, frame=False)
            if self._state != CoordinatorState.RUNNING or not self._accepting[slot]:
                return SubmissionResult(False, slot, None, self._queues[slot].qsize(), "stream_not_recording")
            elapsed_ns = self._elapsed_from(host)
            self._observed_failures[slot] += 1
            self._counter_totals[slot]["read_failure_count"] += 1
            sample = MeasurementSample(
                benchmark_id=self._run_ids[slot],
                metric="read_failure",
                value=1.0,
                unit="failure",
                stream_scope=StreamScope.CAPTURE,
                observed_at=wall,
                elapsed_seconds=elapsed_ns / 1_000_000_000,
                method=f"{self._timing_methods[slot]}_read_failure",
                elapsed_ns=elapsed_ns,
                source_timestamp_ns=None,
                source_sequence=None,
                source_clock=None,
            )
            return self._enqueue_sample_locked(slot, sample, None)

    def stop_stream(
        self,
        slot: str,
        *,
        reason_code: str = "manual_stop",
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
        timeout: float | None = None,
    ) -> CoordinatedStreamState:
        slot = _require_slot(slot)
        _validate_reason_code(reason_code)
        elapsed_ns, wall = self._event_time(host_monotonic_ns, observed_at)
        with self._lock:
            self._require_running()
            state = self._stream_state[slot]
            if state in {
                CoordinatedStreamState.COMPLETED,
                CoordinatedStreamState.INTERRUPTED,
                CoordinatedStreamState.FAILED,
            }:
                return state
            if state == CoordinatedStreamState.STOPPING:
                command = self._terminal_commands[slot]
                if command is None:
                    raise CoordinatorStateError(f"stream {slot} has no pending terminal transition")
            elif state == CoordinatedStreamState.RECORDING:
                target = self._terminal_status_for_stop(slot, elapsed_ns)
                command = self._queue_terminal_locked(
                    slot, elapsed_ns, wall, reason_code, target
                )
            else:
                raise CoordinatorStateError(f"stream {slot} is not recording")
        self._wait_command(command, timeout)
        return self.snapshot().streams[0 if slot == "A" else 1].state

    def fail_stream(
        self,
        slot: str,
        reason_code: str = "capture_stream_failed",
        *,
        host_monotonic_ns: int | None = None,
        observed_at: str | None = None,
        timeout: float | None = None,
    ) -> None:
        slot = _require_slot(slot)
        _validate_reason_code(reason_code)
        elapsed_ns, wall = self._event_time(host_monotonic_ns, observed_at)
        with self._lock:
            self._require_running()
            state = self._stream_state[slot]
            if state == CoordinatedStreamState.STOPPING:
                command = self._terminal_commands[slot]
                if command is None:
                    raise CoordinatorStateError(f"stream {slot} has no pending terminal transition")
            elif state in {CoordinatedStreamState.RECORDING, CoordinatedStreamState.PENDING}:
                command = self._queue_terminal_locked(
                    slot, elapsed_ns, wall, reason_code, StreamStatus.FAILED
                )
            else:
                raise CoordinatorStateError(f"stream {slot} cannot fail from {state}")
        self._wait_command(command, timeout)

    def finalize(
        self,
        *,
        reason_code: str = "manual_stop",
        outcome: GroupStatus | None = None,
        timeout: float | None = None,
    ) -> GroupedExperimentRecord:
        _validate_reason_code(reason_code)
        with self._lock:
            if self._state == CoordinatorState.FINALIZED and self._final_record is not None:
                return self._final_record
            self._require_running()
            if any(self._stream_state[slot] not in _TERMINAL_COORDINATOR_STATES for slot in ("A", "B")):
                raise CoordinatorStateError("both streams must be stopped or failed before group finalization")
            command = self._request_finalization_locked(
                elapsed_ns=self._elapsed_from(self._clock_ns()),
                observed_at=self._now_iso(),
                reason_code=reason_code,
                requested_group_status=outcome,
            )
        self._wait_command(command, timeout)
        if command.result is None:
            raise CoordinatorError("writer finalized without returning a group record")
        self.wait_closed(timeout=timeout)
        return command.result

    def stop_both(
        self,
        *,
        reason_code: str = "manual_stop",
        timeout: float | None = None,
    ) -> GroupedExperimentRecord:
        """Stop both slots, drain accepted samples, then finalize once."""
        _validate_reason_code(reason_code)
        now = self._clock_ns()
        elapsed_ns = self._elapsed_from(now)
        wall = self._now_iso()
        commands: list[_Command] = []
        with self._lock:
            self._require_running()
            for slot in ("A", "B"):
                state = self._stream_state[slot]
                if state in _TERMINAL_COORDINATOR_STATES:
                    continue
                if state == CoordinatedStreamState.STOPPING:
                    command = self._terminal_commands[slot]
                    if command is None:
                        raise CoordinatorStateError(f"stream {slot} has no pending terminal transition")
                else:
                    target = (
                        self._terminal_status_for_stop(slot, elapsed_ns)
                        if state == CoordinatedStreamState.RECORDING
                        else StreamStatus.INTERRUPTED
                    )
                    command = self._queue_terminal_locked(
                        slot, elapsed_ns, wall, reason_code, target
                    )
                commands.append(command)
        for command in commands:
            self._wait_command(command, timeout)
        return self.finalize(reason_code=reason_code, timeout=timeout)

    def cancel(
        self,
        *,
        reason_code: str = "application_shutdown",
        timeout: float = 2.0,
    ) -> bool:
        """Boundedly stop all streams and finalize as interrupted when possible."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        terminal_state = False
        with self._lock:
            if self._state == CoordinatorState.CREATED:
                self._state = CoordinatorState.FINALIZED
                return True
            terminal_state = self._state in {
                CoordinatorState.FINALIZED,
                CoordinatorState.FAILED,
            }
        if terminal_state:
            return self.wait_closed(timeout=timeout)
        _validate_reason_code(reason_code)
        elapsed_ns = self._elapsed_from(self._clock_ns())
        wall = self._now_iso()
        terminal_state = False
        with self._lock:
            if self._state in {CoordinatorState.FINALIZED, CoordinatorState.FAILED}:
                terminal_state = True
            else:
                self._require_running()
                for slot in ("A", "B"):
                    state = self._stream_state[slot]
                    if state in _TERMINAL_COORDINATOR_STATES:
                        continue
                    if state == CoordinatedStreamState.STOPPING:
                        continue
                    self._queue_terminal_locked(
                        slot, elapsed_ns, wall, reason_code, StreamStatus.INTERRUPTED
                    )
                final_command = self._request_finalization_locked(
                    elapsed_ns=elapsed_ns,
                    observed_at=wall,
                    reason_code=reason_code,
                    requested_group_status=GroupStatus.INTERRUPTED,
                )
        if terminal_state:
            return self.wait_closed(timeout=timeout)
        completed = final_command.done.wait(timeout)
        if completed:
            self._thread_join(timeout)
        with self._lock:
            finalized = self._state == CoordinatorState.FINALIZED
        return completed and self._thread_closed.is_set() and finalized

    def wait_closed(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._thread_join(timeout)
        return self._thread_closed.is_set()

    def wait_for_committed(self, slot: str, count: int, timeout: float | None = None) -> bool:
        """Wait for a synthetic producer/observer until ``count`` rows commit."""
        slot = _require_slot(slot)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("count must be a nonnegative integer")
        with self._progress:
            self._progress.wait_for(
                lambda: self._committed[slot] >= count or self._state == CoordinatorState.FAILED,
                timeout=timeout,
            )
            return self._committed[slot] >= count

    def snapshot(self) -> CoordinatorSnapshot:
        with self._lock:
            stream_snapshots = tuple(
                StreamCoordinatorSnapshot(
                    slot=slot,
                    benchmark_id=self._run_ids[slot],
                    state=self._stream_state[slot],
                    queue_depth=self._queues[slot].qsize(),
                    queue_capacity=self._queue_capacity,
                    queue_high_water=self._high_water[slot],
                    accepted_samples=self._accepted[slot],
                    committed_samples=self._committed[slot],
                    queue_rejections=self._queue_rejections[slot],
                    persistence_losses=self._persistence_losses[slot],
                    observed_frames=self._observed_frames[slot],
                    observed_read_failures=self._observed_failures[slot],
                    first_host_arrival_elapsed_ns=self._first_arrival[slot],
                    last_host_arrival_elapsed_ns=self._last_arrival[slot],
                    start_elapsed_ns=self._start_elapsed[slot],
                    end_elapsed_ns=self._end_elapsed[slot],
                )
                for slot in ("A", "B")
            )
            return CoordinatorSnapshot(
                state=self._state,
                session_id=self.session_id,
                writer_healthy=self._writer_healthy,
                lease_held=self._lease is not None,
                writer_cpu_ns=self._writer_cpu_ns,
                streams=stream_snapshots,  # type: ignore[arg-type]
                error=self._writer_error,
            )

    def _enqueue_sample_locked(
        self,
        slot: str,
        sample: MeasurementSample,
        sequence: int | None,
    ) -> SubmissionResult:
        if not self._writer_healthy:
            self._persistence_losses[slot] += 1
            self._counter_totals[slot]["persistence_loss_count"] += 1
            return SubmissionResult(False, slot, sequence, self._queues[slot].qsize(), "writer_failed")
        try:
            self._queues[slot].put_nowait(_QueuedMeasurement(sample))
        except queue.Full:
            self._queue_rejections[slot] += 1
            self._counter_totals[slot]["timing_queue_loss_count"] += 1
            return SubmissionResult(False, slot, sequence, self._queues[slot].qsize(), "queue_full")
        depth = self._queues[slot].qsize()
        self._accepted[slot] += 1
        self._high_water[slot] = max(self._high_water[slot], depth)
        self._wake.set()
        return SubmissionResult(True, slot, sequence, depth)

    def _count_after_writer_failure(self, slot: str, *, frame: bool) -> SubmissionResult:
        sequence = None
        if frame:
            sequence = self._next_sequence[slot]
            self._next_sequence[slot] += 1
            self._observed_frames[slot] += 1
            self._counter_totals[slot]["capture_frame_count"] += 1
        else:
            self._observed_failures[slot] += 1
            self._counter_totals[slot]["read_failure_count"] += 1
        self._persistence_losses[slot] += 1
        self._counter_totals[slot]["persistence_loss_count"] += 1
        return SubmissionResult(False, slot, sequence, self._queues[slot].qsize(), "writer_failed")

    def _event_time(
        self,
        host_monotonic_ns: int | None,
        observed_at: str | None,
    ) -> tuple[int, str]:
        host = self._clock_ns() if host_monotonic_ns is None else _nonnegative_int(
            "host_monotonic_ns", host_monotonic_ns
        )
        return self._elapsed_from(host), observed_at or self._now_iso()

    def _elapsed_from(self, host_monotonic_ns: int) -> int:
        elapsed = host_monotonic_ns - self._epoch_ns
        if elapsed < 0:
            raise ValueError("host timestamp predates the coordinator monotonic epoch")
        return _nonnegative_int("elapsed_ns", elapsed)

    def _enqueue_command_locked(self, command: _Command) -> None:
        try:
            self._commands.put_nowait(command)
        except queue.Full as exc:
            raise CoordinatorStateError("bounded lifecycle command queue is full") from exc
        self._wake.set()

    def _queue_terminal_locked(
        self,
        slot: str,
        elapsed_ns: int,
        observed_at: str,
        reason_code: str,
        target_status: StreamStatus,
    ) -> _Command:
        pending = self._terminal_commands[slot]
        if pending is not None and not pending.done.is_set():
            return pending
        command = _Command(
            "terminal", slot, elapsed_ns, observed_at, reason_code, target_status
        )
        previous_state = self._stream_state[slot]
        previous_accepting = self._accepting[slot]
        self._stream_state[slot] = CoordinatedStreamState.STOPPING
        self._accepting[slot] = False
        try:
            self._enqueue_command_locked(command)
        except BaseException:
            self._stream_state[slot] = previous_state
            self._accepting[slot] = previous_accepting
            raise
        self._terminal_commands[slot] = command
        return command

    def _request_finalization_locked(
        self,
        *,
        elapsed_ns: int,
        observed_at: str,
        reason_code: str,
        requested_group_status: GroupStatus | None,
    ) -> _Command:
        if self._finalize_command is not None:
            return self._finalize_command
        command = _Command(
            "finalize",
            elapsed_ns=elapsed_ns,
            observed_at=observed_at,
            reason_code=reason_code,
            requested_group_status=requested_group_status,
        )
        self._enqueue_command_locked(command)
        self._finalize_command = command
        return command

    def _wait_command(self, command: _Command, timeout: float | None) -> None:
        deadline = self._command_timeout if timeout is None else timeout
        if deadline <= 0:
            raise ValueError("timeout must be positive")
        if not command.done.wait(deadline):
            raise CoordinatorTimeoutError("recording writer did not confirm the command before timeout")
        if command.error is not None:
            raise CoordinatorError(str(command.error)) from command.error

    def _require_running(self) -> None:
        if self._state != CoordinatorState.RUNNING or not self._writer_healthy:
            raise CoordinatorStateError(f"coordinator is not healthy and running ({self._state})")

    def _terminal_status_for_stop(self, slot: str, elapsed_ns: int) -> StreamStatus:
        started = self._start_elapsed[slot]
        if self._target_seconds is None or started is None:
            return StreamStatus.INTERRUPTED
        duration = max(0, elapsed_ns - started) / 1_000_000_000
        if duration >= self._target_seconds:
            return StreamStatus.COMPLETED
        return StreamStatus.INTERRUPTED

    def _writer_main(self) -> None:
        try:
            while True:
                cpu_started = time.thread_time_ns()
                try:
                    if self._writer_failed_or_finalized():
                        return
                    command = self._take_ready_command()
                    if command is not None:
                        self._active_command = command
                        self._process_command(command)
                        self._active_command = None
                        continue
                    slot = self._next_nonempty_stream()
                    if slot is not None:
                        self._persist_one_batch(slot)
                        continue
                    self._wake.wait(WRITER_POLL_SECONDS)
                    self._wake.clear()
                finally:
                    with self._lock:
                        self._writer_cpu_ns += time.thread_time_ns() - cpu_started
        except BaseException as exc:
            cpu_started = time.thread_time_ns()
            try:
                self._handle_writer_failure(exc)
            finally:
                with self._lock:
                    self._writer_cpu_ns += time.thread_time_ns() - cpu_started
        finally:
            cpu_started = time.thread_time_ns()
            try:
                self._signal_pending_commands()
                self._thread_closed.set()
            finally:
                with self._lock:
                    self._writer_cpu_ns += time.thread_time_ns() - cpu_started

    def _writer_failed_or_finalized(self) -> bool:
        with self._lock:
            return self._state in {CoordinatorState.FAILED, CoordinatorState.FINALIZED}

    def _take_ready_command(self) -> _Command | None:
        while len(self._pending_commands) < MAX_CONTROL_COMMANDS:
            try:
                self._pending_commands.append(self._commands.get_nowait())
            except queue.Empty:
                break
        for index, command in enumerate(self._pending_commands):
            if command.operation == "event":
                return self._pending_commands.pop(index)
            if command.operation in {"start", "terminal"}:
                assert command.slot is not None
                if command.operation == "start" or self._queues[command.slot].empty():
                    return self._pending_commands.pop(index)
            elif command.operation == "finalize":
                queues_empty = all(self._queues[slot].empty() for slot in ("A", "B"))
                all_terminal = all(
                    self._stream_state[slot] in _TERMINAL_COORDINATOR_STATES
                    for slot in ("A", "B")
                )
                if queues_empty and all_terminal:
                    return self._pending_commands.pop(index)
            else:
                raise CoordinatorStateError("unknown writer lifecycle command")
        return None

    def _next_nonempty_stream(self) -> str | None:
        order = ("A", "B") if self._last_turn == "B" else ("B", "A")
        for slot in order:
            if not self._queues[slot].empty() and self._stream_state[slot] in {
                CoordinatedStreamState.RECORDING,
                CoordinatedStreamState.STOPPING,
            }:
                self._last_turn = slot
                return slot
        return None

    def _persist_one_batch(self, slot: str) -> None:
        batch: list[MeasurementSample] = []
        for _ in range(self._batch_size):
            try:
                batch.append(self._queues[slot].get_nowait().sample)
            except queue.Empty:
                break
        if not batch:
            return
        assert self._lease is not None
        written = self.repository.record_stream_measurements(
            self.session_id, slot, batch, owner_token=self._lease.owner_token
        )
        with self._progress:
            self._committed[slot] += written
            self._committed_frame_arrivals[slot] += sum(
                sample.metric == "frame_arrival" for sample in batch
            )
            self._committed_read_failures[slot] += sum(
                sample.metric == "read_failure" for sample in batch
            )
            self._progress.notify_all()

    def _process_command(self, command: _Command) -> None:
        assert self._lease is not None
        assert command.elapsed_ns is not None and command.observed_at is not None
        if command.operation == "event":
            assert command.event_type is not None
            self.repository.add_event(
                self.session_id,
                command.event_type,
                observed_at=command.observed_at,
                elapsed_ns=command.elapsed_ns,
                details=command.details,
                owner_token=self._lease.owner_token,
            )
        elif command.operation == "start":
            assert command.slot is not None
            self.repository.set_stream_status(
                self.session_id,
                command.slot,
                StreamStatus.RECORDING,
                observed_at=command.observed_at,
                elapsed_ns=command.elapsed_ns,
                owner_token=self._lease.owner_token,
            )
            with self._progress:
                self._stream_state[command.slot] = CoordinatedStreamState.RECORDING
                self._accepting[command.slot] = True
                self._start_elapsed[command.slot] = command.elapsed_ns
                both_recording = all(
                    self._stream_state[slot] == CoordinatedStreamState.RECORDING
                    for slot in ("A", "B")
                )
                self._progress.notify_all()
            if both_recording:
                self.repository.validate_ready_to_record(self.session_id)
                self.repository.set_group_status(
                    self.session_id,
                    GroupStatus.RECORDING,
                    observed_at=command.observed_at,
                    elapsed_ns=command.elapsed_ns,
                    owner_token=self._lease.owner_token,
                )
        elif command.operation == "terminal":
            assert command.slot is not None and command.target_status is not None
            self._flush_stream_metadata(
                command.slot,
                observed_at=command.observed_at,
                elapsed_ns=command.elapsed_ns,
            )
            self.repository.set_stream_status(
                self.session_id,
                command.slot,
                command.target_status,
                observed_at=command.observed_at,
                elapsed_ns=command.elapsed_ns,
                reason_code=command.reason_code,
                owner_token=self._lease.owner_token,
            )
            with self._progress:
                self._stream_state[command.slot] = CoordinatedStreamState(command.target_status.value)
                self._accepting[command.slot] = False
                self._end_elapsed[command.slot] = command.elapsed_ns
                self._terminal_commands[command.slot] = None
                self._progress.notify_all()
        elif command.operation == "finalize":
            self._flush_all_stream_metadata(
                observed_at=command.observed_at,
                elapsed_ns=command.elapsed_ns,
            )
            target = command.requested_group_status or self._choose_final_status(command.reason_code or "manual_stop")
            durations = {
                slot: self._duration_for_slot(slot) for slot in ("A", "B")
            }
            record = self.repository.finalize_group(
                self.session_id,
                target,
                ended_at=command.observed_at,
                duration_by_slot=durations,
                termination_reason=command.reason_code or "manual_stop",
                owner_token=self._lease.owner_token,
            )
            with self._progress:
                self._final_record = record
                self._state = CoordinatorState.FINALIZED
                self._lease = None
                self._progress.notify_all()
            command.result = record
            command.done.set()
            return
        else:
            raise CoordinatorStateError("unknown writer lifecycle command")
        command.done.set()

    def _flush_stream_metadata(
        self,
        slot: str,
        *,
        observed_at: str | None = None,
        elapsed_ns: int | None = None,
    ) -> None:
        assert self._lease is not None
        increments: dict[str, int] = {}
        with self._lock:
            for counter in _COUNTERS:
                delta = self._counter_totals[slot][counter] - self._counter_persisted[slot][counter]
                if delta:
                    increments[counter] = delta
            first = self._first_arrival[slot]
            last = self._last_arrival[slot]
        if increments:
            self.repository.increment_stream_counters(
                self.session_id,
                slot,
                increments,
                observed_at=observed_at or self._now_iso(),
                elapsed_ns=(
                    elapsed_ns if elapsed_ns is not None
                    else self._elapsed_from(self._clock_ns())
                ),
                owner_token=self._lease.owner_token,
            )
            with self._lock:
                for counter, delta in increments.items():
                    self._counter_persisted[slot][counter] += delta
        if first is not None and last is not None:
            self.repository.note_host_arrival_bounds(
                self.session_id,
                slot,
                first_elapsed_ns=first,
                last_elapsed_ns=last,
                owner_token=self._lease.owner_token,
            )

    def _flush_all_stream_metadata(
        self,
        *,
        observed_at: str | None = None,
        elapsed_ns: int | None = None,
    ) -> None:
        for slot in ("A", "B"):
            self._flush_stream_metadata(slot, observed_at=observed_at, elapsed_ns=elapsed_ns)

    def _choose_final_status(self, reason_code: str) -> GroupStatus:
        with self._lock:
            if reason_code in {"application_shutdown", "cancelled", "writer_interrupted"}:
                return GroupStatus.INTERRUPTED
            persisted_frames = sum(self._committed_frame_arrivals.values())
            persisted_read_failures = sum(self._committed_read_failures.values())
            if persisted_frames == 0 and persisted_read_failures == 0:
                return GroupStatus.FAILED
            completed = all(
                self._stream_state[slot] == CoordinatedStreamState.COMPLETED
                for slot in ("A", "B")
            )
            intact = all(
                self._counter_totals[slot]["timing_queue_loss_count"] == 0
                and self._counter_totals[slot]["persistence_loss_count"] == 0
                and self._accepted[slot] == self._committed[slot]
                for slot in ("A", "B")
            )
            reached_duration = self._target_seconds is not None and all(
                self._duration_for_slot(slot) >= self._target_seconds
                for slot in ("A", "B")
            )
            both_have_arrivals = all(
                self._committed_frame_arrivals[slot] > 0 for slot in ("A", "B")
            )
            if completed and intact and reached_duration and both_have_arrivals:
                return GroupStatus.COMPLETED
            return GroupStatus.PARTIAL

    def _duration_for_slot(self, slot: str) -> float:
        started = self._start_elapsed[slot]
        ended = self._end_elapsed[slot]
        if started is None or ended is None:
            return 0.0
        return max(0, ended - started) / 1_000_000_000

    def _handle_writer_failure(self, error: BaseException) -> None:
        detail = _safe_error(error, self.repository.database.path)
        with self._progress:
            if self._state == CoordinatorState.FINALIZED:
                return
            self._writer_healthy = False
            self._state = CoordinatorState.FAILED
            self._writer_error = detail
            self._accepting = {slot: False for slot in ("A", "B")}
            for slot in ("A", "B"):
                if self._stream_state[slot] not in _TERMINAL_COORDINATOR_STATES:
                    self._stream_state[slot] = CoordinatedStreamState.FAILED
                uncommitted = max(0, self._accepted[slot] - self._committed[slot])
                self._persistence_losses[slot] += uncommitted
                self._counter_totals[slot]["persistence_loss_count"] += uncommitted
            self._progress.notify_all()
        # The failed batch is never retried. Make one bounded best-effort
        # metadata attempt; if SQLite is unavailable, the held lease and
        # nonterminal durable group prevent a false success and stale stealing.
        lease = self._lease
        if lease is not None:
            for slot in ("A", "B"):
                try:
                    increments: dict[str, int] = {}
                    with self._lock:
                        for counter in _COUNTERS:
                            delta = self._counter_totals[slot][counter] - self._counter_persisted[slot][counter]
                            if delta:
                                increments[counter] = delta
                    if increments:
                        self.repository.increment_stream_counters(
                            self.session_id,
                            slot,
                            increments,
                            observed_at=self._now_iso(),
                            elapsed_ns=self._elapsed_from(self._clock_ns()),
                            owner_token=lease.owner_token,
                        )
                        with self._lock:
                            for counter, delta in increments.items():
                                self._counter_persisted[slot][counter] += delta
                    self.repository.mark_stream_incomplete(
                        self.session_id,
                        slot,
                        "writer_failure",
                        observed_at=self._now_iso(),
                        details={"error_type": type(error).__name__},
                        owner_token=lease.owner_token,
                    )
                except BaseException:
                    # A full or unavailable database cannot durably record its
                    # own failure. The in-memory snapshot retains the reason.
                    pass
        for slot in ("A", "B"):
            while True:
                try:
                    self._queues[slot].get_nowait()
                except queue.Empty:
                    break
        self._signal_pending_commands(CoordinatorError(detail))

    def _signal_pending_commands(self, error: BaseException | None = None) -> None:
        pending = list(self._pending_commands)
        self._pending_commands.clear()
        if self._active_command is not None:
            pending.append(self._active_command)
            self._active_command = None
        while True:
            try:
                pending.append(self._commands.get_nowait())
            except queue.Empty:
                break
        for command in pending:
            if not command.done.is_set():
                command.error = error or CoordinatorStateError("writer has stopped")
                command.done.set()

    def _thread_join(self, timeout: float | None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


_TERMINAL_COORDINATOR_STATES = {
    CoordinatedStreamState.COMPLETED,
    CoordinatedStreamState.INTERRUPTED,
    CoordinatedStreamState.FAILED,
}


def _process_start_ticks() -> str | None:
    try:
        stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
        return stat[stat.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None


def _require_slot(slot: str) -> str:
    if slot not in {"A", "B"}:
        raise ValueError("slot must be A or B")
    return slot


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > (1 << 63) - 1:
        raise ValueError(f"{name} must be a nonnegative SQLite-range integer")
    return value


def _validate_reason_code(value: str) -> None:
    if not value or len(value) > 64 or not value[0].islower() or not all(
        character.islower() or character.isdigit() or character == "_" for character in value
    ):
        raise ValueError("reason_code must be a short lowercase structured code")


def _safe_error(error: BaseException, database_path: Path) -> str:
    detail = f"{type(error).__name__}: {error}"
    return detail.replace(str(database_path), "[database]")[:500]
