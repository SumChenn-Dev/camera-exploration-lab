# CEL-013 Pass 2D.1 — Synthetic Grouped History and Analysis

## Scope and activation boundary

This pass adds a read-only grouped experiment view to the standalone synthetic recording harness. It uses the schema-v3 relationships created by Pass 2A and the existing PyQtGraph navigation controls. The normal desktop application still initializes schema v2; its History and Analysis workspaces do not import or activate this viewer. No real history database is migrated or opened by this feature.

The harness creates a fresh marked synthetic database in a temporary directory. The existing **Inspect Synthetic Experiment** action reads the most recent synthetic group on the harness orchestration thread and opens a window with **Grouped History** and **Comparative Analysis** tabs. That database remains available only while the harness is running and is removed during clean shutdown.

Launch from the repository root with the CEL environment:

```bash
QT_QPA_PLATFORM=xcb \
  "$HOME/.local/share/camera-exploration-lab/venv/bin/python" \
  tools/run_synthetic_recording_ui.py \
  --synthetic-only --acknowledge-synthetic-only
```

The harness separately requires an in-window synthetic-only acknowledgement. Start Both, record a synthetic group, stop or let it finish, and choose **Inspect Synthetic Experiment**. Choose **Open in Grouped Analysis** from the History tab to view the plots. No physical devices, image arrays, arbitrary database paths, or default application database are accepted. Closing the harness removes the disposable database.

## Grouped History read model

`grouped_analysis.py` builds an immutable `GroupedExperimentAnalysis` from an existing query-only schema-v3 SQLite snapshot. `GroupedExperimentRepository.read_group_snapshot()` rejects writable connections. Group membership comes only from `group_recordings` and `benchmark_streams`, never from matching camera labels or timestamp proximity.

Each A/B stream model exposes its camera identity snapshot, requested configuration, driver-reported/verified mode when stored, requested and driver-reported FPS, run and stream states, lifecycle duration, actual committed `frame_arrival` row count, committed read-failure row count, last sequence index, observed sequence gaps, queue/persistence loss counters, timestamps and timing provenance, summary values, interval statistics, and completeness warnings. Counts are queried with `COUNT(*)`; the last zero-based index is shown separately and is never used as a row count. Read failures remain separate from successful frame-arrival rows.

The History tab shows the group once with its two child streams, requested/start/stop/finalization markers, per-stream duration and counts, timing summaries, warnings, and lifecycle event table. Partial, interrupted, failed, empty, incomplete, and indeterminate-after-crash states retain distinct labels. Viewing or navigating to Analysis never finalizes a group, repairs events, clears a lease, rewrites summaries, or changes measurements.

## Analysis calculations and alignment

- Frame intervals use host arrival timestamps for adjacent persisted rows only when their stream-local sequence values are consecutive. A gap is excluded from the ordinary interval series and counted separately. A gap means a missing CEL timing record; it does not establish that the camera sensor dropped a frame.
- For contiguous pairs, integer `elapsed_ns` is preferred. `elapsed_seconds` is a compatibility fallback. If stored `interval_ns` disagrees with adjacent timestamps, the timestamp difference is used and a warning marks the data incomplete. Zero and negative intervals are counted and excluded from positive interval statistics and histograms. A first-row interval without an earlier persisted endpoint is treated as a recording-boundary interval and excluded.
- Mean, median, p95, p99, minimum, maximum, standard deviation, coefficient of variation, and histogram frequencies use the full eligible persisted data. The median averages the center pair for even counts; p95/p99 use nearest-rank `ceil(p × n)`. One frame yields zero intervals; two frames yield one interval but do not produce a rate estimate. Rate windows require at least two valid transitions.
- Interval coefficient of variation is population standard deviation divided by the mean interval, expressed as a percentage. It describes interval spread relative to that stream's average interval; it is not a camera-quality score by itself.
- Capture-FPS windows are computed as transition count divided by summed interval duration in a bounded, non-overlapping elapsed-time window. Rate windows never use preview FPS, requested FPS, interpolation, or sequence-number alignment. Percentage deviation is `100 × (window FPS − that stream's own observed mean FPS) / that same mean FPS`; zero is each stream's baseline.
- Frame-interval distributions show shares of valid positive intervals in logarithmic-width bins. Zero intervals remain visible in counts and warnings but are not forced onto the log axis. When positive raw intervals are absent the plot states why rather than showing an empty unlabeled graph.
- Per-stream elapsed-time view subtracts each stream's first persisted arrival timestamp. Shared experiment elapsed-time view is enabled only when group and stream provenance identify one monotonic host clock epoch and all persisted arrival rows have integer elapsed timestamps. Curves keep independent sample times and do not pair frames. The first-arrival offset is host-observed only; a common Record Both command does not establish hardware synchronization.

OpenCV read-return timestamps do not establish sensor exposure time, USB transfer latency, camera-only latency, or sensor-to-display latency. Source/driver timestamps remain unavailable unless a future capture backend actually supplies and records them. GUI preview rate and processing rate are separate metrics from capture/frame-arrival rate.

## Bounded data handling and plotting

SQLite measurement rows are traversed as a cursor. Full-recording statistics are accumulated before plots are reduced. FPS buckets average their valid timing intervals; interval plot buckets retain minima and maxima, so spikes and extrema survive decimation. The point cap is 2,400 points by default. Exact percentiles are retrieved with a SQLite window query rather than calculated from reduced graph points.

The viewer is opened from the harness's Qt orchestration worker, so the query/aggregation does not run in the GUI callback. The GUI receives immutable tuples and records, then draws a bounded number of points. At narrow widths stream interval plots stack vertically; scroll areas keep metadata, event rows, controls, and plots accessible. Fit to Data, Reset View, Zoom In/Out, mouse drag pan, Expand Plot, and Restore Layout reuse the existing `PlotNavigationControls` convention.

The synthetic suite includes 0, 1, 2, 242, 995, and 12,000 frame-arrival fixtures; it checks zero-based index/count distinction, missing sequence values, zero/negative intervals, histogram and FPS behavior, outlier retention, dark/light palette rendering, layout resizing, read-only behavior, and abandoned-lease presentation. `tools/benchmark_grouped_analysis.py --synthetic-only` creates disposable synthetic databases for 30-second, 5-minute, and 30-minute 100+24 and 187+30 event/s scenarios and reports row count, SQLite size, insert/query time, statistics count, and bounded plot size. These are synthetic persistence/analysis measurements, not camera throughput.

One local run of that benchmark produced these planning observations:

| Duration and simulated rates | Measurement rows | SQLite file | Insert throughput | Full analysis load | Returned max plot points per stream |
| --- | ---: | ---: | ---: | ---: | ---: |
| 30 s · 100 + 24 events/s | 3,720 | 1.90 MB | 119k rows/s | 28 ms | 1,199 intervals / 30 FPS windows |
| 30 s · 187 + 30 events/s | 6,510 | 3.15 MB | 112k rows/s | 43 ms | 2,398 intervals / 30 FPS windows |
| 5 min · 100 + 24 events/s | 37,200 | 17.32 MB | 104k rows/s | 248 ms | 1,199 intervals / 300 FPS windows |
| 5 min · 187 + 30 events/s | 65,100 | 30.04 MB | 98k rows/s | 448 ms | 2,398 intervals / 300 FPS windows |
| 30 min · 100 + 24 events/s | 223,200 | 103.81 MB | 83k rows/s | 1.49 s | 1,199 intervals / 1,800 FPS windows |
| 30 min · 187 + 30 events/s | 390,600 | 180.62 MB | 78k rows/s | 2.66 s | 2,398 intervals / 1,800 FPS windows |

The recorded row counts matched the loader's direct SQLite counts, all six temporary databases passed `integrity_check` and `foreign_key_check`, and full-data interval counts were one less than gap-free arrival rows because the first row has no prior persisted endpoint. The synthetic 30-minute load completed in about 2.7 seconds in this environment. The harness executes loading on its orchestration thread, not the Qt GUI thread. SQLite sizes and timings are one local synthetic run and are not camera capture results or performance guarantees.

## Remaining gates

Grouped History/Analysis is not enabled in the ordinary application. Production use remains blocked on reviewed schema-v3 activation and durable backup/restore, end-to-end ownership compatibility, grouped recording UI lifecycle and recovery policy, and a separate production History/Analysis integration review. CEL-013 Pass 2D.1 adds no media storage, database migration on startup, camera access, or hardware synchronization claim.
