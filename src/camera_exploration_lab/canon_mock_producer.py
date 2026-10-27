"""Camera-free CHDKPTP telemetry producer for IPC tests and demonstrations."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any, Callable

from .canon_telemetry_ipc import PROTOCOL_NAME, PROTOCOL_VERSION, encode_message


class MockCanonTelemetryProducer:
    """Small producer client; it has no camera, USB, PTP, or CHDKPTP dependency."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        producer_id: str = "chdkptp-mock",
        camera_id: str = "canon-sx40-hs",
        camera_label: str = "Canon PowerShot SX40 HS",
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.producer_id = producer_id
        self.camera_id = camera_id
        self.camera_label = camera_label
        self.clock_ns = clock_ns
        self._socket: socket.socket | None = None

    def connect(self) -> None:
        if self._socket is not None:
            raise RuntimeError("mock producer is already connected")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(self.socket_path))
        self._socket = client

    def attach(self, client: Any) -> None:
        """Attach a connected stream-like object, useful for in-memory tests."""
        if self._socket is not None:
            raise RuntimeError("mock producer is already connected")
        if not callable(getattr(client, "sendall", None)) or not callable(getattr(client, "close", None)):
            raise ValueError("mock producer requires a connected stream-like object")
        self._socket = client

    def close(self) -> None:
        client, self._socket = self._socket, None
        if client is not None:
            client.close()

    def send_raw(self, payload: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("mock producer is not connected")
        self._socket.sendall(payload)

    def _send(self, message: dict[str, Any]) -> None:
        self.send_raw(encode_message(message))

    def send_status(self, sequence: int, state: str = "connected", *, simulated: bool = True) -> None:
        self._send({
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "type": "camera_status",
            "producer_id": self.producer_id,
            "camera_id": self.camera_id,
            "camera_label": self.camera_label,
            "simulated": simulated,
            "sequence": sequence,
            "timestamp": {"clock_domain": "source_monotonic", "value_ns": self.clock_ns()},
            "camera_state": state,
        })

    def send_sample(
        self,
        sequence: int,
        *,
        fetch_fps: float | None = None,
        content_change_rate: float | None = None,
        fetch_duration_ms: float = 27.0,
        software_skips: int = 0,
        poll_attempts: int = 1,
        device_checks: int = 0,
        requested_fps: float | None = 60.0,
        simulated: bool = True,
    ) -> None:
        if fetch_fps is None:
            fetch_fps = (15.75, 31.64, 16.12, 30.5)[sequence % 4]
        if content_change_rate is None:
            content_change_rate = 0.0 if sequence % 5 == 0 else 27.9
        readings = [
            self._reading("chdkptp_payload_fetch_fps", fetch_fps, "completed CHDKPTP payload fetches/s", "derived", "Mock host-side completed payload fetch rate; not sensor exposure FPS."),
            self._reading("chdkptp_fetch_call_duration_ms", fetch_duration_ms, "ms/fetch call", "observed", "Mock CHDKPTP host-monotonic fetch-call duration; not USB bus latency."),
            self._reading("threshold_content_change_rate", content_change_rate, "threshold-classified changes/s", "estimated", "Mock threshold-classified decoded-content change estimate; not sensor FPS."),
            self._reading("software_skips", software_skips, "count per recorded sample", "observed", "Mock CHDKPTP software skip count; not sensor frame loss."),
            self._reading("chdkptp_fetch_attempts", 1, "count per recorded sample", "observed", "Mock fetch attempt count."),
            self._reading("chdkptp_poll_attempts", poll_attempts, "count per recorded sample", "observed", "Mock CHDKPTP polling attempts."),
            self._reading("device_poll_checks", device_checks, "count per recorded sample", "observed", "Mock device-list polling checks."),
        ]
        if requested_fps is not None:
            readings.insert(0, self._reading("requested_fps", requested_fps, "frames/s", "requested", "Mock requested target; not an achieved rate."))
        if sequence % 5 == 0:
            readings.extend((
                self._reading("content_sample_count", 10, "content samples per recorded sample", "observed", "Mock decoded-content comparison sample count."),
                self._reading("exact_repeat_count", 10, "exact repeats per recorded sample", "observed", "Mock repeated decoded-content samples."),
            ))
        self._send({
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "type": "measurement",
            "producer_id": self.producer_id,
            "camera_id": self.camera_id,
            "camera_label": self.camera_label,
            "simulated": simulated,
            "sequence": sequence,
            "timestamp": {"clock_domain": "source_monotonic", "value_ns": self.clock_ns()},
            "readings": readings,
            "future_extension": {"ignored_by_v1": True},
        })

    @staticmethod
    def _reading(metric: str, value: int | float, unit: str, kind: str, provenance: str) -> dict[str, Any]:
        return {
            "metric": metric,
            "value": value,
            "unit": unit,
            "availability": "available",
            "kind": kind,
            "provenance": provenance,
        }
