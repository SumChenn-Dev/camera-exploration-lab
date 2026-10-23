# CEL-014 Phase 1 — Live Performance Monitor

## Scope

Phase 1 adds a camera-neutral host telemetry contract, bounded polling buffers on existing OpenCV capture workers, a Live Performance Monitor tab, and historical replay for the six published Canon SX40 HS CHDKPTP benchmark CSVs. It does not add a camera backend, schema migration, recording format, CHDKPTP control, or second USB/PTP connection.

The monitor is available from the normal desktop tab bar. It defaults to **Historical Replay**. **Live CEL Capture** only observes workers that the Single Camera or Multi-Camera workbench has already started. The monitor does not discover devices, open a camera, or require schema v3.

## Telemetry contract

`camera_exploration_lab.performance` defines `Metric`, `Measurement`, and `TelemetrySample`:

- A sample has a timestamp in an explicit clock domain, a source label, an optional stream-local sequence, and a tuple of scalar readings.
- Every reading has a fixed metric identifier and unit, availability (`available`, `not_recorded`, or `unavailable`), measurement kind (`observed`, `requested`, `derived`, `estimated`, or `reported`), and provenance text.
- Non-finite, negative rate/duration/count, wrong-unit, and value/availability mismatches are rejected.
- `not_recorded` means the source could have recorded a field but did not publish it. `unavailable` means this source/method cannot establish it.

Capture and display metrics are intentionally separate:

| Metric | Source and meaning | Not equivalent to |
| --- | --- | --- |
| Requested FPS | Selected capture-mode request | Driver readback or achieved rate |
| Successful OpenCV reads/s | Successful host `VideoCapture.read()` returns in a rolling monotonic window | Unique sensor exposures or sensor frame rate |
| Host frame interval | Difference between successful OpenCV read-return timestamps | Exposure interval or sensor-to-display latency |
| OpenCV `read()` call duration | Host-monotonic duration around the blocking read call | USB-only transfer latency |
| GUI preview updates/s | CEL GUI update cadence measured by the preview workbench | Physical display scanout/presentation FPS |
| CHDKPTP payload fetches/s | Completed `stats:end_xfer(bytes)` payload fetches where the CSV provides it | USB bus packets or unique camera frames |
| CHDKPTP GUI-reported FPS | The reported CHDKPTP statistic stored by the benchmark | CEL preview updates or sensor exposures |
| Threshold content-change rate | Published change-classification count/rate from dataset 06 | Independently reproducible image classification or exposure FPS |
| CHDKPTP fetch-call duration | Host-observed duration for the CHDKPTP fetch call | Physical USB transfer or camera latency |
| Poll attempts, device checks, software skips | The distinct counts/fields present in each source CSV | Sensor-side dropped-frame counts |

CEL does not currently observe physical display presentation timing, sensor exposure timestamps, or camera-internal dropped-frame counts. Those measurements are shown unavailable rather than inferred.

## Capture and recorder ownership

`OpenCVCaptureWorker` retains its one exclusive timing listener, including the slot used by `ExperimentRecorder`. The telemetry ring is a separate worker-owned `BoundedTelemetryBuffer` with default capacity 4,096. It stores immutable scalar samples only; frame arrays are not copied into the monitor. The ring overwrites its oldest sample at capacity and exposes overwritten counts and cursor gaps to readers.

The worker uses the successful `read()` return timestamp for the sample. Measuring the host duration of `read()` also takes a monotonic timestamp immediately before the call. No Qt signal is emitted for every captured frame. The monitor polls selected workers at 10 Hz, reads only newer entries, and keeps bounded plot data. A slow plot can lose old monitor visualization samples and reports the gap; it does not pause capture or affect recorder samples. The capture path constructs scalar readings and a sample tuple, then appends them under the ring's short lock. It copies no image data, performs no database work, and does not use the recorder's exclusive listener slot.

A repeatable synthetic microbenchmark compares the capture worker's telemetry-publication hook enabled and disabled (`tools/benchmark_capture_telemetry.py --frames 20000 --repeats 5`). The disabled mode still measures `read()` duration, so the comparison isolates telemetry reading/sample construction and bounded-ring publication rather than the cost of every CEL-014 timing change. On Linux x86_64 with Python 3.14.7, the latest run's median fake-source throughput was:

| Recorder timing listener | Telemetry publication off | Telemetry publication on | Incremental synthetic cost |
| --- | ---: | ---: | ---: |
| No listener | 242,493 events/s | 72,217 events/s | 9.7 microseconds/event |
| No-op listener | 158,249 events/s | 59,867 events/s | 10.4 microseconds/event |

The synthetic relative throughput change (about -70% and -62%) is not a camera-performance estimate: the no-delay fake source runs orders of magnitude faster than V4L2 capture. The measured incremental time corresponds to approximately 0.97–1.04 ms/s at 100 events/s and 1.82–1.94 ms/s at 187 events/s, or about 0.1–0.2% of one worker-second. Separate `tracemalloc` runs peaked around 4.17–4.19 MB with the 4,096-entry ring full, versus about 86–87 kB with publication disabled. The ring retained 4,096 samples and overwrote 15,904 from each 20,000-event run, confirming its configured memory bound. Live plot histories are independently bounded to 4,096 capture points and 600 preview points. This benchmark does not model concurrent UI polling, camera decode, USB, or driver blocking; no physical performance impact has been measured.

The live view can select the existing single-camera worker or either Multi-Camera slot. It reports requested FPS, successful read rate, GUI preview-update rate, host `read()` call duration, read-return intervals, read failures, and capture state. Preview metrics are passed from the owning workbench as snapshots; the monitor never subscribes to the recorder's callback.

## Historical Canon replay

The application bundle contains exact public copies of:

1. `01_initial_fps_sweep.csv`
2. `02_dropframe_ab.csv`
3. `03_live_pipeline_profile.csv`
4. `04_device_polling_ab.csv`
5. `05_smart_polling_fps_sweep.csv`
6. `06_content_freshness.csv`

At load, the replay reader checks each packaged file's SHA-256 and row count against the published `manifest.json`, then validates CSV headers, finite values, indices, status, and elapsed ordering. All rows, including settling rows, are replayable. Missing fields remain explicitly `not_recorded`; live CEL or sensor-only values absent from the benchmark are `unavailable`.

The CSV `elapsed_s` clock resets at benchmark-stage boundaries, and the public data contains no wall-clock timestamps. Replay therefore concatenates rows in published order for playback. Where available it uses each recorded `sample_s`; otherwise it derives a pacing step from the stage-local elapsed spacing, using the first elapsed value at a new stage. This **synthetic replay schedule** drives Play/Pause/Reset and plot order only; it is not a reconstructed wall clock, inter-stage delay, shared camera clock, or synchronization result. The original stage-local elapsed value and stage/condition are retained and shown separately.

The replay displays requested FPS, CHDKPTP-reported rate, completed payload fetch rate when recorded, threshold-classified content change rate when recorded, CHDKPTP fetch-call duration, software skip/poll/fetch/device-check counts, the CSV benchmark row status, and reported throughput fields where available. These series have separate labels and are never renamed to “sensor FPS,” “display FPS,” or “USB latency.”

## Canon live telemetry boundary

The current working Canon CHDKPTP application owns the SX40 HS USB/PTP connection. CEL must not create a second connection or poll the device while that process is active. A later adapter should consume a documented scalar export or local IPC stream produced by the CHDKPTP owner process. That protocol should carry source sequence, host monotonic timestamp and clock/process identity, metric identifier, unit, availability, and provenance. It should distinguish attempted fetches from completed payload fetches and any separately measured content-change estimate.

The existing Canon research is data and methodology, not permission to copy CHDKPTP code. No GPL implementation source is incorporated here. No Canon command, control, connection, or live telemetry IPC is implemented in Phase 1.

## Verification

The camera-free tests validate metric contracts, bounded overwrite/cursor behavior, nonexclusive recorder-listener coexistence, read-failure states, all six manifest-pinned replay files, malformed/missing measurements, source-clock resets, deterministic pause/resume/reset, and offscreen Qt behavior. The live UI tests inject existing fake workers and confirm that the monitor does not call a capture factory. Offscreen screenshots were reviewed at 1024×768, 1366×768, and 1920×1080 using both light and dark application palettes; the 1024×768 layout keeps controls/metrics visible and exposes the stacked charts through vertical scrolling.
