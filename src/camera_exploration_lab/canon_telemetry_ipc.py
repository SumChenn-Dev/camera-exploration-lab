"""Bounded local IPC consumer for a separate CHDKPTP telemetry producer.

The CEL process only receives scalar telemetry over a Unix-domain socket. It
never opens PTP/USB, and the source process clock is retained separately from
the CEL host-monotonic receive timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time
from typing import Any, Callable

from .performance import (
    Availability,
    BoundedTelemetryBuffer,
    ClockDomain,
    MeasurementKind,
    Metric,
    TelemetrySample,
    reading,
)


PROTOCOL_NAME = "cel.canon.telemetry"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
MAX_READINGS = 32
MAX_SOURCES = 8
MAX_PROTOCOL_INT64 = (1 << 63) - 1
MAX_METRIC_VALUE = 1_000_000_000.0
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

_CANON_METRICS = {
    Metric.REQUESTED_FPS,
    Metric.CHDKPTP_FETCH_FPS,
    Metric.CONTENT_CHANGE_RATE,
    Metric.CHDKPTP_GUI_REPORTED_FPS,
    Metric.CHDKPTP_FETCH_DURATION_MS,
    Metric.SOFTWARE_SKIPS,
    Metric.CHDKPTP_FETCH_ATTEMPTS,
    Metric.CHDKPTP_POLL_ATTEMPTS,
    Metric.DEVICE_POLL_CHECKS,
    Metric.CHDKPTP_FETCH_COUNT,
    Metric.CHDKPTP_DROPPED_COUNTER,
    Metric.CHDKPTP_REPORTED_THROUGHPUT_KBPS,
    Metric.CHDKPTP_REPORTED_USB_SPEED_KBPS,
    Metric.CONTENT_SAMPLE_COUNT,
    Metric.EXACT_REPEAT_COUNT,
    Metric.CONNECTION_STATE,
}


class ProtocolError(ValueError):
    """A bounded, non-sensitive protocol validation failure."""


class ConsumerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    LISTENING = "listening"
    CONNECTED = "connected"
    STALE = "stale"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CanonSource:
    key: str
    label: str
    producer_id: str
    camera_id: str
    simulated: bool


@dataclass(frozen=True, slots=True)
class ConsumerSnapshot:
    state: ConsumerState
    socket_path: str
    last_received_ns: int | None
    received_messages: int
    rejected_messages: int
    oversized_messages: int
    sequence_gaps: int
    ring_overwrites: int
    error: str | None


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    source: CanonSource
    sample: TelemetrySample


def default_socket_path() -> Path:
    """Return a per-user runtime endpoint without using a shared socket name."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
    else:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        base = Path(tempfile.gettempdir()) / f"cel-runtime-{uid}"
    return base / "camera-exploration-lab" / "canon-telemetry-v1.sock"


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ProtocolError(f"{field} must be a short identifier using letters, digits, dot, dash or underscore")
    return value


def _reject_json_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number {value} is not allowed")


def _decode_json_line(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("message exceeds the 16 KiB frame limit")
    try:
        decoded = json.loads(payload.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("message is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("message root must be an object")
    return decoded


def parse_message(message: dict[str, Any], received_at_ns: int) -> ParsedMessage:
    """Validate protocol v1 and convert a producer message to CEL telemetry.

    Unknown object fields and unknown metric names are ignored for additive
    compatibility. Unknown protocol versions and known-but-disallowed CEL
    metrics fail closed. No source timestamp is compared with the local clock.
    """
    if message.get("protocol") != PROTOCOL_NAME:
        raise ProtocolError("unrecognized telemetry protocol")
    version = message.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported telemetry protocol version: {version!r}")
    message_type = message.get("type")
    if message_type not in ("measurement", "camera_status"):
        raise ProtocolError("message type must be measurement or camera_status")
    producer_id = _safe_identifier(message.get("producer_id"), "producer_id")
    camera_id = _safe_identifier(message.get("camera_id"), "camera_id")
    label = message.get("camera_label", "Canon camera")
    if (
        not isinstance(label, str)
        or not label.strip()
        or len(label) > 96
        or any(ord(character) < 32 or character in "<>" for character in label)
    ):
        raise ProtocolError("camera_label must contain 1 to 96 characters")
    simulated = message.get("simulated", False)
    if not isinstance(simulated, bool):
        raise ProtocolError("simulated must be boolean")
    sequence = message.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or sequence > MAX_PROTOCOL_INT64
    ):
        raise ProtocolError("sequence must be a non-negative signed 64-bit integer")

    timestamp = message.get("timestamp")
    if not isinstance(timestamp, dict):
        raise ProtocolError("timestamp must be an object")
    domain_name = timestamp.get("clock_domain")
    source_elapsed_s: float | None = None
    if domain_name == ClockDomain.SOURCE_MONOTONIC.value:
        clock_domain = ClockDomain.SOURCE_MONOTONIC
        timestamp_ns = timestamp.get("value_ns")
        if (
            isinstance(timestamp_ns, bool)
            or not isinstance(timestamp_ns, int)
            or timestamp_ns < 0
            or timestamp_ns > MAX_PROTOCOL_INT64
        ):
            raise ProtocolError("source monotonic timestamp must be a non-negative signed 64-bit integer")
    elif domain_name == ClockDomain.SOURCE_ELAPSED.value:
        clock_domain = ClockDomain.SOURCE_ELAPSED
        elapsed = timestamp.get("elapsed_s")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (float, int)):
            raise ProtocolError("source elapsed timestamp must be numeric")
        try:
            source_elapsed_s = float(elapsed)
        except (OverflowError, ValueError) as exc:
            raise ProtocolError("source elapsed timestamp must be finite and non-negative") from exc
        if not (0 <= source_elapsed_s <= MAX_PROTOCOL_INT64 / 1_000_000_000):
            raise ProtocolError("source elapsed timestamp exceeds the signed 64-bit nanosecond range")
        timestamp_ns = round(source_elapsed_s * 1_000_000_000)
    else:
        raise ProtocolError("clock_domain must be source_monotonic or source_elapsed")

    if message_type == "camera_status":
        state = message.get("camera_state")
        if state not in {"connected", "disconnected", "reconnecting", "error"}:
            raise ProtocolError("camera_state is not a supported state")
        measurements = (reading(
            Metric.CONNECTION_STATE,
            state,
            kind=MeasurementKind.OBSERVED,
            provenance="Camera connection state reported by the owning CHDKPTP process.",
        ),)
    else:
        raw_readings = message.get("readings")
        if not isinstance(raw_readings, list) or not raw_readings or len(raw_readings) > MAX_READINGS:
            raise ProtocolError(f"readings must contain 1 to {MAX_READINGS} items")
        converted = []
        seen: set[Metric] = set()
        for item in raw_readings:
            if not isinstance(item, dict) or not isinstance(item.get("metric"), str):
                raise ProtocolError("each reading must be an object with a metric name")
            try:
                metric = Metric(item["metric"])
            except ValueError:
                # Unknown metric names are an additive protocol extension.
                continue
            if metric not in _CANON_METRICS:
                raise ProtocolError(f"metric {metric.value!r} is not accepted from a Canon source")
            if metric in seen:
                raise ProtocolError("a message cannot repeat a metric")
            seen.add(metric)
            try:
                availability = Availability(item.get("availability"))
                raw_value = item.get("value")
                if (
                    availability == Availability.AVAILABLE
                    and type(raw_value) in (int, float)
                    and abs(raw_value) > MAX_METRIC_VALUE
                ):
                    raise ProtocolError(
                        f"reading for {metric.value} exceeds the protocol scalar limit"
                    )
                kind_value = item.get("kind")
                kind = MeasurementKind(kind_value) if kind_value is not None else None
                provenance = item.get("provenance")
                if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 512:
                    raise ProtocolError("reading provenance must contain 1 to 512 characters")
                converted_reading = reading(
                    metric,
                    item.get("value"),
                    kind=kind,
                    availability=availability,
                    provenance=provenance,
                )
            except (OverflowError, TypeError, ValueError) as exc:
                if isinstance(exc, ProtocolError):
                    raise
                raise ProtocolError(f"invalid reading for {metric.value}") from exc
            unit = item.get("unit")
            if unit != converted_reading.unit:
                raise ProtocolError(f"unit does not match metric {metric.value}")
            converted.append(converted_reading)
        if not converted:
            raise ProtocolError("message has no recognized Canon telemetry readings")
        measurements = tuple(converted)

    source_key = f"canon-chdkptp:{producer_id}:{camera_id}"
    source = CanonSource(source_key, label.strip(), producer_id, camera_id, simulated)
    sample = TelemetrySample(
        timestamp_ns=timestamp_ns,
        source=source_key,
        clock_domain=clock_domain,
        readings=measurements,
        sequence=sequence,
        source_elapsed_s=source_elapsed_s,
        received_timestamp_ns=received_at_ns,
    )
    return ParsedMessage(source, sample)


def encode_message(message: dict[str, Any]) -> bytes:
    """Serialize one newline-delimited JSON protocol message."""
    try:
        payload = json.dumps(message, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message cannot be serialized as finite JSON") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("message exceeds the 16 KiB frame limit")
    return payload + b"\n"


class CanonTelemetryConsumer:
    """Single-producer AF_UNIX stream server with bounded scalar telemetry.

    The producer is trusted only to send validated telemetry, not commands.
    Socket permissions are owner-only. Only a refused, same-owner socket inode
    may be removed; ambiguous or active endpoint paths are left untouched.
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        capacity: int = 4096,
        stale_after_s: float = 2.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()
        self.telemetry_buffer = BoundedTelemetryBuffer(capacity=capacity)
        self.stale_after_ns = round(stale_after_s * 1_000_000_000)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._state = ConsumerState.STOPPED
        self._last_received_ns: int | None = None
        self._connected_at_ns: int | None = None
        self._received_messages = 0
        self._rejected_messages = 0
        self._oversized_messages = 0
        self._sequence_gaps = 0
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._sources: dict[str, CanonSource] = {}
        self._last_sequences: dict[str, int] = {}
        self._last_source_times: dict[str, tuple[ClockDomain, int]] = {}
        self._last_receive_for_source_ns: dict[str, int] = {}

    @property
    def sources(self) -> tuple[CanonSource, ...]:
        with self._lock:
            return tuple(self._sources.values())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Canon telemetry listener is already active")
            if self._state == ConsumerState.ERROR:
                self._state = ConsumerState.STOPPED
            self._stop_event.clear()
            self._ready_event.clear()
            self._state = ConsumerState.STARTING
            self._last_error = None
            self._thread = threading.Thread(
                target=self._serve,
                name="cel-canon-telemetry-ipc",
                daemon=False,
            )
            self._thread.start()

    def request_stop(self) -> None:
        """Request shutdown without joining or blocking the caller."""
        self._stop_event.set()

    def shutdown(self) -> None:
        """Application-shutdown alias; cleanup completes on the bounded listener thread."""
        self.request_stop()

    def wait_until_ready(self, timeout: float = 1.0) -> bool:
        return self._ready_event.wait(max(0.0, timeout))

    def wait_for_state(self, state: ConsumerState, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            return self._condition.wait_for(
                lambda: self._state == state or self._state == ConsumerState.ERROR,
                max(0.0, deadline - time.monotonic()),
            ) and self._state == state

    def wait_stopped(self, timeout: float = 1.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, timeout))
            if thread.is_alive():
                return False
        return self.snapshot().state in (ConsumerState.STOPPED, ConsumerState.ERROR)

    def wait_for_messages(self, count: int, timeout: float = 1.0) -> bool:
        """Deterministic coordination helper for test harnesses and diagnostics."""
        if count < 0:
            raise ValueError("count cannot be negative")
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            return self._condition.wait_for(
                lambda: self._received_messages >= count or self._state == ConsumerState.ERROR,
                max(0.0, deadline - time.monotonic()),
            ) and self._received_messages >= count

    def snapshot(self, *, now_ns: int | None = None) -> ConsumerSnapshot:
        now = self._clock_ns() if now_ns is None else now_ns
        with self._lock:
            state = self._state
            last = self._last_received_ns
            baseline = last if last is not None else self._connected_at_ns
            if state == ConsumerState.CONNECTED and baseline is not None and now - baseline >= self.stale_after_ns:
                state = ConsumerState.STALE
            return ConsumerSnapshot(
                state=state,
                socket_path=str(self.socket_path),
                last_received_ns=last,
                received_messages=self._received_messages,
                rejected_messages=self._rejected_messages,
                oversized_messages=self._oversized_messages,
                sequence_gaps=self._sequence_gaps,
                ring_overwrites=self.telemetry_buffer.overwritten_count,
                error=self._last_error,
            )

    def _set_state(self, state: ConsumerState, error: str | None = None) -> None:
        with self._condition:
            self._state = state
            self._last_error = error
            self._condition.notify_all()

    @staticmethod
    def _secure_parent(path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError("socket parent must be a real directory, not a symlink")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError("socket directory is not owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("socket directory permissions must exclude group and other users")
        if len(os.fsencode(path)) > 100:
            raise ValueError("Unix socket path is too long")

    @staticmethod
    def _acquire_endpoint_lock(path: Path) -> int:
        """Serialize same-endpoint startup across processes without lock cleanup."""
        lock_path = path.with_name(f"{path.name}.lock")
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("secure telemetry endpoint locking requires O_NOFOLLOW")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("telemetry endpoint lock must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise PermissionError("telemetry endpoint lock is not owned by the current user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("telemetry endpoint lock permissions must exclude group and other users")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileExistsError("another CEL listener owns this telemetry endpoint") from exc
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _remove_stale_socket(path: Path) -> None:
        """Remove only the same socket inode after proving no listener accepts it."""
        try:
            original = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(original.st_mode):
            raise FileExistsError("telemetry endpoint exists and is not a socket; it was left untouched")
        if hasattr(os, "getuid") and original.st_uid != os.getuid():
            raise FileExistsError("telemetry socket is not owned by the current user; it was left untouched")
        identity = (original.st_dev, original.st_ino)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.15)
        try:
            try:
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return
                if exc.errno != errno.ECONNREFUSED:
                    raise FileExistsError(
                        "existing telemetry socket could not be proven inactive; it was left untouched"
                    ) from exc
            else:
                raise FileExistsError("another telemetry listener is active; its endpoint was left untouched")
        finally:
            probe.close()
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise FileExistsError("telemetry endpoint changed during stale-socket inspection; it was left untouched")
        path.unlink()

    def _serve(self) -> None:
        server: socket.socket | None = None
        lock_descriptor: int | None = None
        bound_identity: tuple[int, int] | None = None
        failed = False
        try:
            self._secure_parent(self.socket_path)
            lock_descriptor = self._acquire_endpoint_lock(self.socket_path)
            self._remove_stale_socket(self.socket_path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.settimeout(0.1)
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            socket_stat = self.socket_path.lstat()
            bound_identity = (socket_stat.st_dev, socket_stat.st_ino)
            server.listen(1)
            self._set_state(ConsumerState.LISTENING)
            self._ready_event.set()
            while not self._stop_event.is_set():
                try:
                    client, _address = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(0.1)
                    self._begin_connection()
                    self._serve_client(client)
                if not self._stop_event.is_set():
                    self._set_state(ConsumerState.LISTENING, "Producer disconnected; waiting for reconnect.")
        except Exception as exc:
            failed = True
            self._set_state(ConsumerState.ERROR, f"Telemetry listener failed: {type(exc).__name__}.")
            self._ready_event.set()
        finally:
            if server is not None:
                server.close()
            if bound_identity is not None:
                try:
                    current = self.socket_path.lstat()
                    if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == bound_identity:
                        self.socket_path.unlink()
                except FileNotFoundError:
                    pass
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            if not failed:
                self._set_state(ConsumerState.STOPPED)
            self._ready_event.set()
            with self._condition:
                self._condition.notify_all()

    def _begin_connection(self) -> None:
        with self._condition:
            self._connected_at_ns = self._clock_ns()
            self._last_received_ns = None
            self._last_receive_for_source_ns.clear()
            self._last_sequences.clear()
            self._last_source_times.clear()
            self._state = ConsumerState.CONNECTED
            self._last_error = None
            self._condition.notify_all()

    def _serve_client(self, client: socket.socket) -> None:
        pending = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                self.snapshot()
                continue
            except OSError:
                return
            if not chunk:
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > MAX_FRAME_BYTES:
                        self._record_oversized()
                        return
                    break
                line = bytes(pending[:newline])
                del pending[:newline + 1]
                if len(line) > MAX_FRAME_BYTES:
                    self._record_oversized()
                    return
                if line:
                    self._accept_line(line)

    def _record_oversized(self) -> None:
        with self._condition:
            self._oversized_messages += 1
            self._last_error = "Oversized telemetry frame rejected; producer connection closed."
            self._condition.notify_all()

    def _accept_line(self, line: bytes) -> None:
        try:
            message = _decode_json_line(line)
            received_at = self._clock_ns()
            parsed = parse_message(message, received_at)
            source_key = parsed.source.key
            with self._lock:
                previous_sequence = self._last_sequences.get(source_key)
                sequence_gap = 0
                if previous_sequence is not None:
                    if parsed.sample.sequence is None or parsed.sample.sequence <= previous_sequence:
                        raise ProtocolError("source sequence must increase within a producer connection")
                    sequence_gap = max(0, parsed.sample.sequence - previous_sequence - 1)
                previous_time = self._last_source_times.get(source_key)
                if previous_time is not None:
                    previous_domain, previous_ns = previous_time
                    if previous_domain != parsed.sample.clock_domain or parsed.sample.timestamp_ns < previous_ns:
                        raise ProtocolError("source clock domain changed or source timestamp moved backwards")
                if source_key not in self._sources and len(self._sources) >= MAX_SOURCES:
                    raise ProtocolError("source limit reached for this listener")
                self._sequence_gaps += sequence_gap
                self._last_sequences[source_key] = parsed.sample.sequence or 0
                self._last_source_times[source_key] = (parsed.sample.clock_domain, parsed.sample.timestamp_ns)
                self._sources[source_key] = parsed.source
                previous_receive = self._last_receive_for_source_ns.get(source_key)
                if previous_receive is not None:
                    gap_ms = max(0, received_at - previous_receive) / 1_000_000
                    receive_gap = reading(
                        Metric.HOST_TELEMETRY_RECEIVE_GAP_MS,
                        gap_ms,
                        kind=MeasurementKind.OBSERVED,
                        provenance="Difference between CEL host-monotonic IPC receipt timestamps; not camera transfer latency.",
                    )
                    parsed = ParsedMessage(
                        parsed.source,
                        TelemetrySample(
                            timestamp_ns=parsed.sample.timestamp_ns,
                            source=parsed.sample.source,
                            clock_domain=parsed.sample.clock_domain,
                            readings=parsed.sample.readings + (receive_gap,),
                            sequence=parsed.sample.sequence,
                            source_elapsed_s=parsed.sample.source_elapsed_s,
                            received_timestamp_ns=received_at,
                        ),
                    )
                self._last_receive_for_source_ns[source_key] = received_at
                self._last_received_ns = received_at
                self._received_messages += 1
                self._last_error = None
                self._state = ConsumerState.CONNECTED
                self.telemetry_buffer.append(parsed.sample)
                self._condition.notify_all()
        except ProtocolError as exc:
            with self._condition:
                self._rejected_messages += 1
                self._last_error = str(exc)[:240]
                self._condition.notify_all()
        except (OverflowError, TypeError, ValueError) as exc:
            with self._condition:
                self._rejected_messages += 1
                self._last_error = f"Invalid telemetry message: {type(exc).__name__}"[:240]
                self._condition.notify_all()
