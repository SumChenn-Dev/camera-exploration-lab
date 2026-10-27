from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from camera_exploration_lab.canon_mock_producer import MockCanonTelemetryProducer
from camera_exploration_lab.canon_telemetry_ipc import (
    MAX_FRAME_BYTES,
    MAX_METRIC_VALUE,
    MAX_PROTOCOL_INT64,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    CanonTelemetryConsumer,
    ConsumerState,
    ProtocolError,
    encode_message,
    parse_message,
)
from camera_exploration_lab.performance import Availability, ClockDomain, Metric


class MemoryStreamSocket:
    """Deterministic stream-shaped test double; does not open an OS socket."""

    def __init__(self, state: dict, side: int) -> None:
        self._state = state
        self._side = side
        self._timeout = 0.1
        self._closed = False

    @classmethod
    def pair(cls):
        state = {"condition": threading.Condition(), "queues": [bytearray(), bytearray()], "closed": [False, False]}
        return cls(state, 0), cls(state, 1)

    def settimeout(self, timeout: float) -> None:
        self._timeout = timeout

    def sendall(self, payload: bytes) -> None:
        peer = 1 - self._side
        condition = self._state["condition"]
        with condition:
            if self._closed or self._state["closed"][peer]:
                raise BrokenPipeError("synthetic peer is closed")
            self._state["queues"][peer].extend(payload)
            condition.notify_all()

    def recv(self, size: int) -> bytes:
        condition = self._state["condition"]
        deadline = time.monotonic() + self._timeout
        with condition:
            while not self._state["queues"][self._side]:
                if self._closed or self._state["closed"][1 - self._side]:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                condition.wait(remaining)
            queue = self._state["queues"][self._side]
            result = bytes(queue[:size])
            del queue[:size]
            return result

    def close(self) -> None:
        condition = self._state["condition"]
        with condition:
            if not self._closed:
                self._closed = True
                self._state["closed"][self._side] = True
                condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class CanonTelemetryProtocolTests(unittest.TestCase):
    @staticmethod
    def message() -> dict:
        return {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "type": "measurement",
            "producer_id": "chdkptp-test",
            "camera_id": "canon-sx40-hs",
            "camera_label": "Synthetic Canon",
            "simulated": True,
            "sequence": 7,
            "timestamp": {"clock_domain": "source_monotonic", "value_ns": 123456789},
            "readings": [{
                "metric": Metric.CHDKPTP_FETCH_FPS.value,
                "value": 31.25,
                "unit": "completed CHDKPTP payload fetches/s",
                "availability": "available",
                "kind": "derived",
                "provenance": "Synthetic completed payload fetch rate.",
            }],
        }

    def test_roundtrip_preserves_metric_clock_domain_and_host_receipt_separately(self) -> None:
        message = self.message()
        payload = encode_message(message)
        self.assertTrue(payload.endswith(b"\n"))
        from camera_exploration_lab.canon_telemetry_ipc import _decode_json_line
        parsed = parse_message(_decode_json_line(payload[:-1]), 9_000_000_000)
        self.assertEqual(parsed.sample.get(Metric.CHDKPTP_FETCH_FPS).value, 31.25)
        self.assertEqual(parsed.sample.clock_domain, ClockDomain.SOURCE_MONOTONIC)
        self.assertEqual(parsed.sample.timestamp_ns, 123456789)
        self.assertEqual(parsed.sample.received_timestamp_ns, 9_000_000_000)
        self.assertNotEqual(parsed.sample.timestamp_ns, parsed.sample.received_timestamp_ns)
        self.assertTrue(parsed.source.simulated)

    def test_source_elapsed_timestamp_is_retained_without_host_clock_comparison(self) -> None:
        message = self.message()
        message["timestamp"] = {"clock_domain": "source_elapsed", "elapsed_s": 3.125}
        parsed = parse_message(message, 77_000_000_000)
        self.assertEqual(parsed.sample.clock_domain, ClockDomain.SOURCE_ELAPSED)
        self.assertEqual(parsed.sample.source_elapsed_s, 3.125)
        self.assertEqual(parsed.sample.timestamp_ns, 3_125_000_000)
        self.assertEqual(parsed.sample.received_timestamp_ns, 77_000_000_000)

    def test_unknown_fields_and_unknown_metrics_are_ignored_additively(self) -> None:
        message = self.message()
        message["future_field"] = {"format": 2}
        message["readings"].append({"metric": "future_unknown_metric", "value": "ignored"})
        parsed = parse_message(message, 1)
        self.assertEqual(len(parsed.sample.readings), 1)

    def test_unavailable_values_are_preserved_and_bad_known_values_fail_closed(self) -> None:
        message = self.message()
        message["readings"][0].update(value=None, availability="unavailable", kind=None)
        parsed = parse_message(message, 1)
        metric = parsed.sample.get(Metric.CHDKPTP_FETCH_FPS)
        self.assertEqual(metric.availability, Availability.UNAVAILABLE)
        self.assertIsNone(metric.value)

        message["readings"][0].update(value=31.25, availability="available", kind="derived", unit="frames/s")
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)
        message["readings"][0].update(unit="completed CHDKPTP payload fetches/s", value=True)
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)

    def test_protocol_version_and_timestamps_require_bounded_integer_domains(self) -> None:
        message = self.message()
        message["version"] = 1.0
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)

        message = self.message()
        message["timestamp"]["value_ns"] = MAX_PROTOCOL_INT64 + 1
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)

        message = self.message()
        message["timestamp"] = {"clock_domain": "source_elapsed", "elapsed_s": 10**1000}
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)

        message = self.message()
        message["readings"][0]["value"] = MAX_METRIC_VALUE * 1_000_000
        with self.assertRaisesRegex(ProtocolError, "scalar limit"):
            parse_message(message, 1)

    def test_unsupported_version_unknown_clock_and_nonfinite_json_are_rejected(self) -> None:
        message = self.message()
        message["version"] = PROTOCOL_VERSION + 1
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)
        message = self.message()
        message["timestamp"]["clock_domain"] = "host_monotonic"
        with self.assertRaises(ProtocolError):
            parse_message(message, 1)
        with self.assertRaises(ProtocolError):
            encode_message({"value": float("nan")})


class CanonTelemetryConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cel-canon-ipc-")
        self.socket_path = Path(self.temporary.name) / "canon.sock"
        self.consumer = CanonTelemetryConsumer(self.socket_path, capacity=8, stale_after_s=0.5)
        self.producers: list[MockCanonTelemetryProducer] = []
        self.peer_threads: list[threading.Thread] = []
        self.producer = self.make_producer()
        self.connect_pair(self.producer)

    def tearDown(self) -> None:
        for producer in self.producers:
            producer.close()
        self.consumer.request_stop()
        for thread in self.peer_threads:
            thread.join(1.0)
            self.assertFalse(thread.is_alive(), "synthetic IPC reader thread did not stop")
        self.temporary.cleanup()

    def make_producer(self, *, camera_id: str = "canon-sx40-hs") -> MockCanonTelemetryProducer:
        producer = MockCanonTelemetryProducer(
            self.socket_path,
            producer_id="chdkptp-mock",
            camera_id=camera_id,
        )
        self.producers.append(producer)
        return producer

    def connect_pair(self, producer: MockCanonTelemetryProducer, consumer: CanonTelemetryConsumer | None = None) -> None:
        target = consumer or self.consumer
        server_side, producer_side = MemoryStreamSocket.pair()
        producer.attach(producer_side)

        def serve() -> None:
            target._begin_connection()
            with server_side:
                target._serve_client(server_side)
            if not target._stop_event.is_set():
                target._set_state(ConsumerState.LISTENING, "Producer disconnected; waiting for reconnect.")

        thread = threading.Thread(target=serve, name="cel-test-canon-ipc", daemon=True)
        thread.start()
        self.peer_threads.append(thread)

    def test_mock_measurements_are_bounded_and_source_labeled(self) -> None:
        self.producer.send_status(0)
        self.producer.send_sample(1, fetch_fps=31.64, fetch_duration_ms=29.9)
        self.assertTrue(self.consumer.wait_for_messages(2, 1.0))
        self.assertEqual(self.consumer.snapshot().state, ConsumerState.CONNECTED)
        samples = self.consumer.telemetry_buffer.snapshot()
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[-1].source, "canon-chdkptp:chdkptp-mock:canon-sx40-hs")
        self.assertEqual(samples[-1].get(Metric.CHDKPTP_FETCH_FPS).value, 31.64)
        self.assertEqual(samples[-1].get(Metric.CHDKPTP_FETCH_DURATION_MS).value, 29.9)
        self.assertIsNotNone(samples[-1].received_timestamp_ns)
        self.assertIsNotNone(samples[-1].get(Metric.HOST_TELEMETRY_RECEIVE_GAP_MS))
        self.assertEqual(self.consumer.sources[0].simulated, True)

    def test_malformed_and_unsupported_messages_are_rejected_without_killing_stream(self) -> None:
        self.producer.send_raw(b"not-json\n")
        unsupported = {"protocol": PROTOCOL_NAME, "version": 99, "type": "measurement"}
        self.producer.send_raw((json.dumps(unsupported) + "\n").encode())
        self.producer.send_sample(0)
        self.assertTrue(self.consumer.wait_for_messages(1, 1.0))
        deadline = time.monotonic() + 1.0
        while self.consumer.snapshot().rejected_messages < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        snapshot = self.consumer.snapshot()
        self.assertEqual(snapshot.rejected_messages, 2)
        self.assertEqual(snapshot.received_messages, 1)
        self.assertEqual(snapshot.state, ConsumerState.CONNECTED)

    def test_rejected_regressive_timestamp_does_not_count_a_sequence_gap(self) -> None:
        first = CanonTelemetryProtocolTests.message()
        self.producer.send_raw(encode_message(first))
        self.assertTrue(self.consumer.wait_for_messages(1, 1.0))

        rejected = CanonTelemetryProtocolTests.message()
        rejected["sequence"] = 10
        rejected["timestamp"]["value_ns"] = 1
        self.producer.send_raw(encode_message(rejected))

        next_valid = CanonTelemetryProtocolTests.message()
        next_valid["sequence"] = 8
        next_valid["timestamp"]["value_ns"] = 123456790
        self.producer.send_raw(encode_message(next_valid))
        self.assertTrue(self.consumer.wait_for_messages(2, 1.0))
        deadline = time.monotonic() + 1.0
        while self.consumer.snapshot().rejected_messages < 1 and time.monotonic() < deadline:
            time.sleep(0.001)

        snapshot = self.consumer.snapshot()
        self.assertEqual(snapshot.rejected_messages, 1)
        self.assertEqual(snapshot.sequence_gaps, 0)

    def test_extreme_numeric_reading_is_rejected_without_ending_producer_stream(self) -> None:
        message = CanonTelemetryProtocolTests.message()
        message["readings"][0]["value"] = int("9" * 4000)
        self.producer.send_raw(encode_message(message))
        self.producer.send_sample(0)
        self.assertTrue(self.consumer.wait_for_messages(1, 1.0))
        deadline = time.monotonic() + 1.0
        while self.consumer.snapshot().rejected_messages < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        snapshot = self.consumer.snapshot()
        self.assertEqual(snapshot.rejected_messages, 1)
        self.assertEqual(snapshot.received_messages, 1)
        self.assertEqual(snapshot.state, ConsumerState.CONNECTED)

    def test_oversized_frame_is_rejected_and_connection_can_reconnect(self) -> None:
        self.producer.send_raw(b"x" * (MAX_FRAME_BYTES + 1))
        deadline = time.monotonic() + 1.0
        while self.consumer.snapshot().oversized_messages < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.consumer.snapshot().oversized_messages, 1)
        self.producer.close()
        self.assertTrue(self.consumer.wait_for_state(ConsumerState.LISTENING, 1.0))
        self.producer = self.make_producer()
        self.connect_pair(self.producer)
        self.producer.send_sample(0)
        self.assertTrue(self.consumer.wait_for_messages(1, 1.0))
        self.assertEqual(self.consumer.snapshot().state, ConsumerState.CONNECTED)

    def test_stale_state_uses_host_receipt_time_not_source_timestamp(self) -> None:
        now = [5_000_000_000]
        temporary = tempfile.TemporaryDirectory(prefix="cel-canon-stale-")
        path = Path(temporary.name) / "stale.sock"
        consumer = CanonTelemetryConsumer(path, clock_ns=lambda: now[0], stale_after_s=0.5)
        producer = MockCanonTelemetryProducer(path, clock_ns=lambda: 50)
        peer_threads: list[threading.Thread] = []
        try:
            server_side, producer_side = MemoryStreamSocket.pair()
            producer.attach(producer_side)
            consumer._begin_connection()

            def serve() -> None:
                with server_side:
                    consumer._serve_client(server_side)
                if not consumer._stop_event.is_set():
                    consumer._set_state(ConsumerState.LISTENING)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            peer_threads.append(thread)
            producer.send_sample(0)
            self.assertTrue(consumer.wait_for_messages(1, 1.0))
            now[0] += 600_000_000
            self.assertEqual(consumer.snapshot().state, ConsumerState.STALE)
            sample = consumer.telemetry_buffer.snapshot()[0]
            self.assertEqual(sample.timestamp_ns, 50)
            self.assertEqual(sample.received_timestamp_ns, 5_000_000_000)
        finally:
            producer.close()
            consumer.request_stop()
            for thread in peer_threads:
                thread.join(1.0)
                self.assertFalse(thread.is_alive())
            temporary.cleanup()

    def test_sequence_gaps_and_ring_overwrites_are_reported_separately(self) -> None:
        self.producer.send_status(0)
        for sequence in (2, 3, 4, 5, 6, 7, 8, 9):
            self.producer.send_sample(sequence)
        self.assertTrue(self.consumer.wait_for_messages(9, 1.0))
        snapshot = self.consumer.snapshot()
        self.assertEqual(snapshot.sequence_gaps, 1)
        self.assertEqual(snapshot.ring_overwrites, 1)
        read = self.consumer.telemetry_buffer.read_since(-1)
        self.assertEqual(read.missed_entries, 1)
        self.assertEqual(len(read.entries), 8)

    def test_camera_sources_remain_distinct_on_one_local_listener(self) -> None:
        self.producer.send_status(0)
        self.producer.send_sample(1)
        self.producer.close()
        self.assertTrue(self.consumer.wait_for_state(ConsumerState.LISTENING, 1.0))
        second = self.make_producer(camera_id="canon-sx40-hs-secondary")
        self.connect_pair(second)
        second.send_status(0)
        second.send_sample(1, fetch_fps=12.0)
        self.assertTrue(self.consumer.wait_for_messages(4, 1.0))
        samples = self.consumer.telemetry_buffer.snapshot()
        by_source = {sample.source: sample for sample in samples if sample.get(Metric.CHDKPTP_FETCH_FPS)}
        self.assertEqual(len(by_source), 2)
        values = {sample.source: sample.get(Metric.CHDKPTP_FETCH_FPS).value for sample in by_source.values()}
        self.assertIn(12.0, values.values())
        self.assertEqual(len(self.consumer.sources), 2)

    def test_existing_non_socket_path_is_never_unlinked_or_replaced(self) -> None:
        # An occupied non-socket path is rejected without replacement or unlink.
        self.producer.close()
        self.socket_path.write_text("keep this file", encoding="utf-8")
        sentinel = self.socket_path.read_bytes()
        second = CanonTelemetryConsumer(self.socket_path)
        second.start()
        self.assertTrue(second.wait_until_ready(1.0))
        snapshot = second.snapshot()
        self.assertEqual(snapshot.state, ConsumerState.ERROR)
        self.assertEqual(self.socket_path.read_bytes(), sentinel)
        second.request_stop()
        self.assertTrue(second.wait_stopped(1.0))
        self.socket_path.unlink()

    def test_symlink_endpoint_is_rejected_without_touching_target(self) -> None:
        self.producer.close()
        target = Path(self.temporary.name) / "endpoint-target"
        target.write_text("leave target intact", encoding="utf-8")
        self.socket_path.symlink_to(target)
        second = CanonTelemetryConsumer(self.socket_path)
        second.start()
        self.assertTrue(second.wait_until_ready(1.0))
        self.assertEqual(second.snapshot().state, ConsumerState.ERROR)
        self.assertTrue(self.socket_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "leave target intact")
        second.request_stop()
        self.assertTrue(second.wait_stopped(1.0))

    def test_socket_owned_by_another_user_is_left_untouched(self) -> None:
        unexpected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unexpected.bind(str(self.socket_path))
        unexpected.close()
        with patch("camera_exploration_lab.canon_telemetry_ipc.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(FileExistsError, "not owned"):
                CanonTelemetryConsumer._remove_stale_socket(self.socket_path)
        self.assertTrue(self.socket_path.exists())
        self.socket_path.unlink()

    def test_lock_symlink_is_rejected_without_touching_target(self) -> None:
        target = Path(self.temporary.name) / "lock-target"
        target.write_text("leave target intact", encoding="utf-8")
        lock_path = self.socket_path.with_name(f"{self.socket_path.name}.lock")
        lock_path.symlink_to(target)
        with self.assertRaises(OSError):
            CanonTelemetryConsumer._acquire_endpoint_lock(self.socket_path)
        self.assertTrue(lock_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "leave target intact")

    def test_concurrent_listeners_serialize_stale_socket_recovery(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.close()
        self.producer.close()
        first = CanonTelemetryConsumer(self.socket_path)
        second = CanonTelemetryConsumer(self.socket_path)
        start = threading.Barrier(3)
        starters = [
            threading.Thread(target=lambda consumer=consumer: (start.wait(), consumer.start()), daemon=True)
            for consumer in (first, second)
        ]
        for thread in starters:
            thread.start()
        start.wait()
        for thread in starters:
            thread.join(1.0)
            self.assertFalse(thread.is_alive(), "listener starter did not return")
        self.assertTrue(first.wait_until_ready(1.0))
        self.assertTrue(second.wait_until_ready(1.0))
        states = {first.snapshot().state, second.snapshot().state}
        self.assertEqual(states, {ConsumerState.LISTENING, ConsumerState.ERROR})
        active = self.socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(active.st_mode))
        first.request_stop()
        second.request_stop()
        self.assertTrue(first.wait_stopped(1.0))
        self.assertTrue(second.wait_stopped(1.0))
        self.assertTrue(self.socket_path.with_name(self.socket_path.name + ".lock").exists())

    def test_stale_unix_socket_is_recovered_and_rebound_owner_only(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.close()

        self.consumer.start()
        self.assertTrue(self.consumer.wait_until_ready(1.0))
        self.assertEqual(self.consumer.snapshot().state, ConsumerState.LISTENING)
        endpoint = self.socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(endpoint.st_mode))
        self.assertEqual(stat.S_IMODE(endpoint.st_mode), 0o600)
        self.consumer.request_stop()
        self.assertTrue(self.consumer.wait_stopped(1.0))
        self.assertFalse(self.socket_path.exists())

    def test_active_unix_socket_is_not_removed_by_competing_listener(self) -> None:
        self.producer.close()
        self.consumer.start()
        self.assertTrue(self.consumer.wait_until_ready(1.0))
        active = self.socket_path.lstat()
        identity = (active.st_dev, active.st_ino)

        contender = CanonTelemetryConsumer(self.socket_path)
        contender.start()
        self.assertTrue(contender.wait_until_ready(1.0))
        self.assertEqual(contender.snapshot().state, ConsumerState.ERROR)
        after = self.socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(after.st_mode))
        self.assertEqual((after.st_dev, after.st_ino), identity)
        contender.request_stop()
        self.assertTrue(contender.wait_stopped(1.0))
        self.consumer.request_stop()
        self.assertTrue(self.consumer.wait_stopped(1.0))

    def test_real_unix_socket_accepts_mock_and_allows_reconnect(self) -> None:
        self.producer.close()
        self.consumer.start()
        self.assertTrue(self.consumer.wait_until_ready(1.0))
        self.assertEqual(self.consumer.snapshot().state, ConsumerState.LISTENING)

        first = self.make_producer()
        first.connect()
        first.send_status(0)
        first.send_sample(1, fetch_fps=22.5)
        self.assertTrue(self.consumer.wait_for_messages(2, 1.0))
        first.close()
        self.assertTrue(self.consumer.wait_for_state(ConsumerState.LISTENING, 1.0))

        second = self.make_producer()
        second.connect()
        second.send_status(0)
        second.send_sample(1, fetch_fps=28.0)
        self.assertTrue(self.consumer.wait_for_messages(4, 1.0))
        values = [
            sample.get(Metric.CHDKPTP_FETCH_FPS).value
            for sample in self.consumer.telemetry_buffer.snapshot()
            if sample.get(Metric.CHDKPTP_FETCH_FPS) is not None
        ]
        self.assertEqual(values, [22.5, 28.0])
        self.consumer.request_stop()
        self.assertTrue(self.consumer.wait_stopped(1.0))

    def test_socket_parent_must_be_private(self) -> None:
        public_parent = Path(self.temporary.name) / "public"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)
        with self.assertRaises(PermissionError):
            CanonTelemetryConsumer._secure_parent(public_parent / "canon.sock")

    def test_socket_parent_symlink_is_rejected(self) -> None:
        real_parent = Path(self.temporary.name) / "real"
        real_parent.mkdir(mode=0o700)
        link_parent = Path(self.temporary.name) / "alias"
        link_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(PermissionError):
            CanonTelemetryConsumer._secure_parent(link_parent / "canon.sock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
