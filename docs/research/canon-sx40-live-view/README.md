# Canon PowerShot SX40 HS — CHDKPTP Live-View Performance Research

Status: exploratory; reported summary arithmetic is reproducible from sanitized CSVs, with method gaps stated below
Platform under test: Canon PowerShot SX40 HS with CHDK, CHDKPTP GTK/LGI GUI on Linux
Publication scope: performance data and analysis only; no camera serial, username, host path, room image, face image, or private local configuration is included.

## Why this exists

Camera Exploration Lab (CEL) is evaluating a future Canon CHDKPTP backend. During live-view work, the SX40 HS initially appeared to deliver only roughly 10–16 frames/s in the GUI even when a higher target FPS was requested. A sequence of controlled tests was used to separate camera transfer speed, GUI rendering, software frame skipping, GTK scheduling, background USB device discovery, and repeated image content.

The strongest result in these data is one within-session A/B/A comparison: suspending CHDKPTP device-list polling during Live View was associated with roughly twice as many completed CHDKPTP live-view payload fetches per second, and performance returned near baseline when polling resumed. This is a large observed effect in this setup, not a statistical-significance claim or a universal throughput prediction.

## Privacy and reproducibility

The CSV files under `results/` are sanitized public copies. Wall-clock `timestamp` columns were removed and replaced by `sample_index`; original local filenames and host paths are not published. Raw originals should remain in the private/local Canon lab archive. Screenshots are intentionally excluded because some contained a person and/or room details.

`manifest.json` records SHA-256 hashes of both the local source files used to build this bundle and the sanitized public CSVs. This allows the maintainer to prove which private source dataset produced a public copy without exposing its local path.

## Calculation and interpretation

Rows marked settling are excluded from the stage summaries; rows marked ok are included. The first dataset has four settling rows per stage, datasets 02–04 have three, and datasets 05–06 have four. In dataset 01, each reported actual-FPS value is the arithmetic mean of the 25 valid per-second rows in that stage. In datasets 04–06, rates are recalculated as the sum of event counts divided by the sum of recorded sample_s durations, rather than treating rounded per-row rates as exact. The A/B/A table's average USB fetch time is weighted by fetch attempts; its maximum callback gap is the maximum valid-window value, and polling duration is weighted by the number of checks. The smart-polling values are pooled by stage; the reported 33.20/s is the arithmetic mean of its two separate 60-FPS target stages. The only blank measurement cells are 29 avg_device_check_ms entries in the paused_B condition, where device_checks is zero and a per-check mean is undefined. All other published data cells are populated.

Dataset 04 contains one A/B/A sequence with 25 valid one-second summary rows per condition and about 25.4–25.8 seconds of valid samples per condition. Adjacent rows are parts of a continuous run, not independent experimental replicates. The data support a strong within-session association; they do not provide repeat-session uncertainty or a formal statistical test.

Dataset 06's changed-content rate is recalculated from changed_over_threshold divided by total sample_s. The CSV identifies its method only as rgba-string; it does not publish the threshold or the complete comparison predicate. Therefore the transfer and changed-count arithmetic can be checked, but the content-change classification cannot be independently reproduced from the public files alone.

## Experimental progression

### 1. Initial requested-FPS sweep

The first automated 30 → 45 → 60 → 30 → 45 → 60 FPS sweep showed that simply raising the requested rate did not produce sustained higher GUI FPS. Post-settling arithmetic means of the CHDKPTP GUI-reported actual-FPS statistic were approximately:

| Stage | Requested FPS | CHDKPTP GUI-reported FPS |
|---:|---:|---:|
| 1 | 30 | 13.66 |
| 2 | 45 | 11.61 |
| 3 | 60 | 10.43 |
| 4 | 30 | 10.51 |
| 5 | 45 | 10.66 |
| 6 | 60 | 10.36 |

The `Dropped` counter was then traced to CHDKPTP's own deliberate skip mechanism rather than proven camera-side frame loss.

### 2. Software frame-skipping A/B test

The GUI source showed that when `gui_live_dropframes` is enabled and the previous frame takes longer than the requested interval, CHDKPTP schedules a skipped update. Disabling this removed deliberate skips and helped in some stages, but it did not explain the full throughput ceiling. Performance remained variable, generally in the low-to-mid teens.

### 3. Pipeline profiling

Instrumentation counted successful `stats:end_xfer(bytes)` calls after a CHDKPTP live-view payload fetch completed. This distinguishes completed client-side payload fetches from polling attempts; it does not count USB bus packets or establish unique camera frames. Redraw preparation was small (about 0.4 ms at 50% viewport and about 1 ms at 100%), while USB fetches were typically around 25–30 ms. Large callback gaps still appeared.

### 4. Background device-polling A/B/A — key finding

CHDKPTP's GUI schedules `timer_update_connection_status()` using `gui_dev_check_interval`, default 500 ms. The callback calls `chdku.list_devices()` and refreshes the camera list if needed.

One A/B/A sequence held the requested live-view target at 60 FPS and changed only the device-polling behavior:

| Condition | Completed CHDKPTP payload fetches/s | Avg USB fetch | Max callback gap | Device checks |
|---|---:|---:|---:|---:|
| Normal A | 15.75 | 26.5 ms | 384.0 ms | 50 |
| Device polling paused | **31.64** | 29.9 ms | **118.8 ms** | 0 |
| Normal A2 | 16.12 | 25.8 ms | 407.7 ms | 50 |

Observed device checks averaged roughly 289 ms in both normal stages. The B interval more than doubled the pooled payload-fetch rate and A2 returned near A. This A/B/A pattern supports an association with polling, but the single sequence cannot exclude order, carryover, or other time-varying effects.

This is the largest measured host-side effect in the current dataset. It remains a single-device, single-host, single-sequence result.

### 5. Smart-polling FPS sweep

A session-only "smart polling" prototype suspends expensive device enumeration while Live View is active and restores normal polling after leaving Live View. With software frame skipping off and a 50% viewport, the automatic forward/reverse FPS sweep produced:

| Requested target | Pass 1 | Pass 2 | Mean completed payload fetches/s |
|---:|---:|---:|---:|
| 30 | 26.13 | 26.68 | **26.40** |
| 45 | 31.44 | 27.79 | **29.62** |
| 60 | 34.42 | 31.98 | **33.20** |

Across the 60-FPS target stages, completed CHDKPTP payload-fetch rate was therefore above 30 fetches/s in both valid 60-FPS target stages even though 60 unique video frames/s was not achieved or claimed.

### 6. Content-freshness experiment

A subsequent test sampled decoded RGBA content to distinguish completed CHDKPTP live-view payload fetches from samples whose compared image content crossed the test's change threshold. The camera viewed a continuously moving ceiling fan/light scene.

| Stage | Target | Completed payload fetches/s | Changed-content samples/s |
|---:|---:|---:|---:|
| 1 | 45 | 37.78 | 27.46 |
| 2 | 60 | 43.58 | 28.38 |
| 3 | 60 | 43.91 | 27.92 |
| 4 | 45 | 38.54 | 28.48 |

Increasing the requested target from 45 to 60 increased completed CHDKPTP payload fetches substantially, but the published threshold-based content-change count stayed close to 28/s. This is consistent with a camera/display-buffer refresh cadence near 30 Hz, but **does not prove a 30 FPS sensor-exposure rate**. Exact repeats can occur because CHDKPTP retrieves the same framebuffer more than once; conversely, exposure/noise changes can make a nominally static scene differ. The threshold and full image-comparison predicate are not included in the CSV, so that content-change classification needs additional method documentation before it can be independently reproduced.

## Camera power-saving finding

The physical LCD and live-view image previously stopped after inactivity while the PTP connection remained present. CHDK configuration `alt_prevent_shutdown` was changed from value 1 to value 3 (`Disable LCD Off = Always`). Afterward, the camera display remained active until manually powered off during testing. This removed a major confounder from long benchmarks.

## Current working high-throughput configuration

For experiments on this specific SX40 HS / Linux setup:

- CHDK `Disable LCD Off`: Always
- CHDKPTP device-list polling: suspend only while active Live View is running; restore afterward
- `gui_live_dropframes`: off during controlled benchmarks
- Viewfinder: enabled
- UI overlay: disabled
- Viewfinder 1:1: off (50% display conversion)
- Requested FPS: 60 when maximizing completed transfers; 45 may be sufficient when minimizing redundant fetches

These are research settings, not universal recommendations for all CHDK-supported cameras.

## What appears to be known already

Older CHDK/CHDKPTP discussions document a related but broader limitation: the GUI's timer-based live-view loop can behave badly on Linux when the work performed per frame approaches or exceeds the requested timer interval. CHDK developer discussion from 2012 explicitly describes that behavior. Historical Raspberry Pi testing also reported live-view performance in the low teens on older hardware.

References:

- CHDK forum, "CHDK PTP interface" (2012), discussion around post #716: https://chdk.setepontos.com/d/4338-chdk-ptp-interface/721
- CHDK forum, "chdkptp - alternative ptp client" Raspberry Pi live-view discussion: https://chdk.setepontos.com/d/6231-chdkptp-alternative-ptp-client/204
- CHDKPTP usage documentation / GUI options: https://github.com/simonswine/chdkptp/blob/master/USAGE.TXT (moving branch; pin a revision in a formal upstream report).

### What may be new/useful

A targeted web search did **not** surface an existing public report showing this exact A/B/A result: the default 500 ms GUI device-list check (`gui_dev_check_interval`) taking roughly 289 ms on this Linux setup and reducing SX40 HS completed CHDKPTP live-view payload fetches from about 31.6/s to about 16/s. That does not establish novelty; it only means an equivalent report was not found in the searched sources. The raw/sanitized dataset here is intended to make the observation reproducible and reviewable by the CHDK community.

## Limitations

1. Results apply to one Canon PowerShot SX40 HS, one CHDK/CHDKPTP setup, and one Linux host environment.
2. Completed CHDKPTP live-view payload fetches are not equivalent to unique sensor exposures.
3. The content-freshness test compares sampled decoded RGBA data and can undercount or overcount true scene updates.
4. Exact test firmware/build identifiers should be added before presenting the results as a formal upstream performance report.
5. A future controlled moving-pattern target is preferable to the ceiling-fan/light scene because the latter introduces autofocus, motion blur, glare, exposure, and possible LED flicker.
6. Device polling should not simply be removed globally; the current prototype suspends it only during active Live View so normal connection discovery can resume afterward.
7. The polling A/B/A comparison is one sequence and has not been repeated in separate sessions or on another host/camera.
8. Dataset 06 does not encode the content-change threshold or full comparison predicate; its content-change classifications are not independently reproducible from these public files alone.

## Next experiments

- Use a constrained target that fits the physical mount: a moving high-contrast pattern within the camera's existing field of view, rather than requiring camera repositioning.
- Determine whether the ~28 changing-sample/s plateau corresponds to framebuffer refresh cadence by measuring content signatures at higher sampling precision.
- Measure presentation cadence separately from USB transfer cadence.
- Add CHDK build, CHDKPTP revision, host kernel/libusb version, and camera mode to every benchmark manifest.
- Repeat the device-polling A/B/A test on at least one additional CHDK-supported Canon body if available.

## Archiver behavior and review

The archive helper now requires an explicit source CSV, defaults its destination to the Git worktree containing the helper, supports linked Git worktrees, and refuses to replace an existing result. It leaves source files unchanged and never stages or commits. The repository ignores CSV and JSONL files by default, so a maintainer must review and explicitly stage approved publication artifacts.

The helper removes timestamp columns, replaces any supplied sample_index with a generated row index, and rejects common identifying column names and obvious email or home-path values. This is a guardrail, not a complete privacy scanner: every generated CSV still requires human privacy review before publication. The public source SHA-256 is a content fingerprint; it does not expose a path and cannot be independently checked without the private source file.

## Data files

- `01_initial_fps_sweep.csv`
- `02_dropframe_ab.csv`
- `03_live_pipeline_profile.csv`
- `04_device_polling_ab.csv`
- `05_smart_polling_fps_sweep.csv`
- `06_content_freshness.csv`

See `manifest.json` for row counts and hashes.
