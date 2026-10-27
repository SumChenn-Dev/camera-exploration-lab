#!/usr/bin/env python3
"""Run an explicitly synthetic CHDKPTP telemetry producer for CEL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from camera_exploration_lab.canon_mock_producer import MockCanonTelemetryProducer
from camera_exploration_lab.canon_telemetry_ipc import PROTOCOL_NAME, PROTOCOL_VERSION, default_socket_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Camera-free CEL Canon telemetry mock producer")
    parser.add_argument("--simulate", action="store_true", help="required acknowledgement that no camera is used")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--scenario", choices=("regular", "repeated", "interrupted", "slow", "malformed", "unsupported"), default="regular")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--reconnect-delay", type=float, default=0.5)
    args = parser.parse_args()
    if not args.simulate:
        parser.error("--simulate is required; this program only emits fabricated telemetry")
    if args.samples < 1 or args.interval <= 0 or args.reconnect_delay < 0:
        parser.error("samples and interval must be positive; reconnect delay cannot be negative")

    producer = MockCanonTelemetryProducer(args.socket)
    sequence = 0

    def open_source() -> None:
        producer.connect()
        nonlocal sequence
        producer.send_status(sequence, "connected", simulated=True)
        sequence += 1

    try:
        open_source()
        if args.scenario == "malformed":
            producer.send_raw(b"{not-json}\n")
        elif args.scenario == "unsupported":
            producer.send_raw((json.dumps({
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION + 1,
                "type": "measurement",
            }) + "\n").encode("utf-8"))
        for index in range(args.samples):
            if args.scenario == "interrupted" and index == args.samples // 2:
                producer.close()
                time.sleep(args.reconnect_delay)
                open_source()
            repeated = args.scenario == "repeated" and index % 3 != 0
            producer.send_sample(
                sequence,
                content_change_rate=0.0 if repeated else None,
                fetch_fps=(16.0, 31.6, 21.0, 30.2)[index % 4],
                fetch_duration_ms=(25.8, 29.9, 27.1, 26.4)[index % 4],
                software_skips=1 if index % 9 == 0 else 0,
                poll_attempts=2 if index % 7 == 0 else 1,
                device_checks=1 if index % 6 == 0 else 0,
                simulated=True,
            )
            sequence += 1
            delay = max(args.interval, 1.0) if args.scenario == "slow" else args.interval
            if index + 1 < args.samples:
                time.sleep(delay)
        print(f"Sent {args.samples} synthetic measurement(s) to {args.socket}.")
        return 0
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        print(f"Could not connect to the CEL telemetry listener: {type(exc).__name__}.", file=sys.stderr)
        return 2
    finally:
        producer.close()


if __name__ == "__main__":
    raise SystemExit(main())
