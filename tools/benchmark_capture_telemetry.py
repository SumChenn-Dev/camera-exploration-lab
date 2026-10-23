#!/usr/bin/env python3
"""Compare the synthetic capture hot path with telemetry enabled and disabled.

This benchmark uses a finite fake CaptureLike source and never opens a device.
It measures Python worker throughput, not camera or driver performance.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
import tracemalloc
from collections import defaultdict
from typing import Any

from camera_exploration_lab.capture import OpenCVCaptureWorker
from camera_exploration_lab.capture_modes import CaptureMode


_FRAME = object()


class _FiniteCapture:
    def __init__(self, count: int, stop_worker: Any) -> None:
        self.count = count
        self.stop_worker = stop_worker
        self.reads = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def set(self, _prop: int, _value: float) -> bool:
        return True

    def get(self, prop: int) -> float:
        return {3: 640.0, 4: 480.0, 5: 60.0, 6: 0.0}.get(prop, 0.0)

    def read(self) -> tuple[bool, object]:
        self.reads += 1
        if self.reads == self.count:
            self.stop_worker()
        return True, _FRAME

    def release(self) -> None:
        self.released = True


class _TelemetryDisabledWorker(OpenCVCaptureWorker):
    """Same capture path, with only the telemetry publication hook disabled."""

    def _publish_capture_telemetry(self, **_kwargs: Any) -> None:
        return None


def _run_once(frame_count: int, *, telemetry: bool, recorder_listener: bool,
              measure_memory: bool = False) -> dict[str, int | float]:
    worker_ref: list[OpenCVCaptureWorker] = []
    listener_count = [0]

    def capture_factory(_device: str) -> _FiniteCapture:
        return _FiniteCapture(frame_count, worker_ref[0].request_stop)

    worker_type = OpenCVCaptureWorker if telemetry else _TelemetryDisabledWorker
    worker = worker_type(
        "synthetic://finite-capture",
        CaptureMode("synthetic-640x480", 640, 480, 60, "MJPG"),
        capture_factory=capture_factory,
        worker_label="telemetry-benchmark",
    )
    worker_ref.append(worker)
    if recorder_listener:
        worker.subscribe_timing_listener(lambda _event: listener_count.__setitem__(0, listener_count[0] + 1))

    if measure_memory:
        tracemalloc.start()
    started_ns = time.perf_counter_ns()
    worker.start()
    worker.join(timeout=60)
    elapsed_ns = time.perf_counter_ns() - started_ns
    if worker.is_alive():
        worker.request_stop()
        worker.join(timeout=5)
        raise RuntimeError("synthetic capture worker did not stop within 60 seconds")
    if measure_memory:
        _current, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    else:
        peak_bytes = 0

    snapshot = worker.snapshot()
    if snapshot.frame_count != frame_count:
        raise RuntimeError(f"expected {frame_count} frames, got {snapshot.frame_count}")
    if recorder_listener and listener_count[0] != frame_count:
        raise RuntimeError(f"recorder listener received {listener_count[0]} of {frame_count} events")
    buffer = worker.telemetry_buffer
    samples_kept = len(buffer.snapshot()) if telemetry else 0
    overwritten = buffer.overwritten_count if telemetry else 0
    if telemetry and samples_kept != min(frame_count, buffer.capacity):
        raise RuntimeError("telemetry ring sample count did not match its configured bound")
    if telemetry and overwritten != max(0, frame_count - buffer.capacity):
        raise RuntimeError("telemetry ring overwrite count did not match the synthetic workload")

    elapsed_seconds = elapsed_ns / 1_000_000_000
    return {
        "elapsed_ns": elapsed_ns,
        "events_per_second": frame_count / elapsed_seconds,
        "frame_count": snapshot.frame_count,
        "listener_events": listener_count[0],
        "ring_capacity": buffer.capacity,
        "ring_samples_kept": samples_kept,
        "ring_overwritten": overwritten,
        "tracemalloc_peak_bytes": peak_bytes,
    }


def run_benchmark(frame_count: int, repeats: int) -> dict[str, object]:
    measurements: dict[str, list[dict[str, int | float]]] = defaultdict(list)
    configs = ((False, False), (True, False), (False, True), (True, True))
    for repeat in range(repeats):
        ordered = configs if repeat % 2 == 0 else tuple(reversed(configs))
        for telemetry, listener in ordered:
            key = f"telemetry_{'on' if telemetry else 'off'}_recorder_listener_{'on' if listener else 'off'}"
            result = _run_once(frame_count, telemetry=telemetry, recorder_listener=listener)
            measurements[key].append(result)
            print(f"repeat {repeat + 1}/{repeats} {key}: {result['events_per_second']:.0f} events/s", flush=True)

    memory: dict[str, int] = {}
    for telemetry, listener in configs:
        key = f"telemetry_{'on' if telemetry else 'off'}_recorder_listener_{'on' if listener else 'off'}"
        memory[key] = int(_run_once(
            frame_count, telemetry=telemetry, recorder_listener=listener, measure_memory=True
        )["tracemalloc_peak_bytes"])

    summaries: dict[str, dict[str, float | int]] = {}
    for key, runs in measurements.items():
        rates = [float(run["events_per_second"]) for run in runs]
        summaries[key] = {
            "median_events_per_second": statistics.median(rates),
            "min_events_per_second": min(rates),
            "max_events_per_second": max(rates),
            "tracemalloc_peak_bytes_separate_run": memory[key],
            "ring_capacity": int(runs[-1]["ring_capacity"]),
            "ring_samples_kept": int(runs[-1]["ring_samples_kept"]),
            "ring_overwritten": int(runs[-1]["ring_overwritten"]),
        }

    deltas: dict[str, float] = {}
    for listener in (False, True):
        off = summaries[f"telemetry_off_recorder_listener_{'on' if listener else 'off'}"]["median_events_per_second"]
        on = summaries[f"telemetry_on_recorder_listener_{'on' if listener else 'off'}"]["median_events_per_second"]
        deltas[f"recorder_listener_{'on' if listener else 'off'}"] = (float(on) / float(off) - 1.0) * 100.0

    return {
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "workload": "finite synthetic OpenCV CaptureLike loop; no camera or frame image allocation",
            "frames_per_run": frame_count,
            "repeats_per_mode": repeats,
        },
        "results": summaries,
        "telemetry_rate_change_percent_vs_disabled": deltas,
        "interpretation": (
            "Synthetic Python worker throughput only. It does not predict V4L2, USB, camera, decode, or preview performance."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=20_000, help="successful synthetic reads per run")
    parser.add_argument("--repeats", type=int, default=5, help="timed runs per mode")
    args = parser.parse_args()
    if args.frames < 2 or args.repeats < 1:
        parser.error("--frames must be at least 2 and --repeats at least 1")
    result = run_benchmark(args.frames, args.repeats)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
