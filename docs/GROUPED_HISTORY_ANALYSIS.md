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

The synthetic suite includes 0, 1, 2, 242, 995, and 12,000 frame-arrival fixtures; it checks zero-based index/count distinction, missing sequence values, zero/negative intervals, histogram and FPS behavior, rare short and long outlier retention, dark/light palette rendering, layout resizing, read-only behavior, and abandoned-lease presentation. `tools/benchmark_grouped_analysis.py --synthetic-only --repeats 3` creates disposable synthetic databases for 30-second, 5-minute, and 30-minute 100+24 and 187+30 event/s scenarios. It reports each untraced Analysis load time and median. `--measure-memory`, `--profile-largest`, and `--offscreen-gui-largest` run separate diagnostic passes on the largest case so tracing/profiling does not contaminate timed results. These are synthetic SQLite/application measurements, not camera throughput.

The old 18.88-second Analysis value in the Pass 2E.1 readiness report was not a clean load time: that benchmark started `tracemalloc` before fixture insertion and kept it active while `load_grouped_analysis()` ran. Allocation tracing greatly slowed the Python per-row pass. The corrected baseline below uses three untraced loads per generated database, on the same Python/SQLite environment, then the optimized implementation uses three untraced loads. Values are medians. The largest-case memory pass is separate.

| Duration / synthetic rates | Rows | Baseline median | Optimized median | Change | Returned interval points (A/B) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 30 s · 100 + 24 events/s | 3,720 | 28 ms | 25 ms | 11% faster | 1,199 / 719 |
| 30 s · 187 + 30 events/s | 6,510 | 45 ms | 40 ms | 11% faster | 2,398 / 899 |
| 5 min · 100 + 24 events/s | 37,200 | 257 ms | 206 ms | 20% faster | 1,199 / 2,398 |
| 5 min · 187 + 30 events/s | 65,100 | 467 ms | 365 ms | 22% faster | 2,398 / 2,398 |
| 30 min · 100 + 24 events/s | 223,200 | 1.523 s | 1.253 s | 18% faster | 1,199 / 2,398 |
| 30 min · 187 + 30 events/s | 390,600 | 2.677 s | 2.222 s | 17% faster | 2,398 / 2,398 |

The workloads ran under Linux x86_64 with the CEL virtual environment's Python 3.14.7 and SQLite 3.51.2. Database row counts matched direct SQLite counts; gap-free interval counts remained exactly one less than the number of frame-arrival rows. All six post-change synthetic databases passed `integrity_check` and `foreign_key_check`. No new index was needed. The combined aggregate query uses the existing measurement index without a temporary sort. Removing the redundant `sample_index IS NULL` sort term removes the full order-by temporary sort; SQLite still uses a small block sort for the final `id` tie-break and the exact percentile query still sorts values by interval rank. Those remaining sorts preserve deterministic tie handling and exact percentiles.

The largest optimized analysis pass retained a maximum of 2,398 interval points and 1,800 FPS windows per stream while calculating statistics over all 390,600 rows. The separate Python `tracemalloc` pass peaked at 2,686 KiB during Analysis; SQLite native and Qt allocations are outside that number. The baseline analysis-only traced peak from the earlier isolated measurement was about 2.69 MiB, so memory remained effectively flat. Separate warm-cache probes measured connection plus group snapshot at 0.69 ms, tuple cursor scan/materialization at 250 ms for A and 42 ms for B, exact percentile SQL for both streams at 718 ms, read-only recovery inspection at 83 ms, and 1,000 metadata-only time-alignment checks at 0.60 ms. These passes are deliberately measured separately and are not additive to full load time. cProfile showed 5.88 million calls and 3.90 seconds under instrumentation: `_load_stream` 3.812 s cumulative, plot bucket updates 1.210 s cumulative, SQLite execute calls 0.929 s cumulative, exact percentile queries 0.767 s cumulative, and read-only recovery inspection 0.093 s cumulative. These nested profiler values overlap and are not additive. The streaming pass combines tuple materialization, sequence-contiguous interval checks, full-data statistics, and bucket updates to avoid retaining all rows. Before the change the profiler counted about 12.1 million calls; the old separate bucket helper path and eager `dict.setdefault(..., Bucket())` allocation were major avoidable costs.

Offscreen Qt measurement on the largest grouped model took 88 ms for plot-widget construction and 20 ms for first `show()` plus event processing. Querying, row streaming, interval/statistical preparation, and decimation run on the harness-owned worker thread; widget creation and drawing of already bounded tuples remain on the Qt thread. These offscreen numbers do not describe physical KDE display performance. The responsive UI tests cover 1024×768, 1366×768, and 1920×1080, but a visual Bazzite review is still needed.

The benchmark's repeated timings include synthetic database query and recovery inspection work in `load_grouped_analysis()`. cProfile provides function-level attribution; `_load_stream` combines cursor row materialization, sequence-contiguous interval checks, full-data statistics, and bounded bucket accumulation in one streaming pass to avoid retaining full rows in memory. The optimized path preserves exact percentile inputs, row counts, sequence gaps, timestamp provenance, host start offsets, and outliers. Cancellation uses SQLite's progress handler plus periodic checks during streaming. The synthetic harness disables duplicate Inspect requests and cancels an in-progress read on close, suppressing stale results. It reuses the selected group's recovery finding and schema/lock summary returned with Analysis instead of performing a second redundant recovery scan; it does not retain other groups' recovery findings. Read-only digest tests verify that analysis does not alter the database.

## Remaining gates

Grouped History/Analysis is not enabled in the ordinary application. Production use remains blocked on reviewed schema-v3 activation and durable backup/restore, end-to-end ownership compatibility, grouped recording UI lifecycle and recovery policy, and a separate production History/Analysis integration review. CEL-013 Pass 2D.1 adds no media storage, database migration on startup, camera access, or hardware synchronization claim.
