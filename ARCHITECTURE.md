# Camera Exploration Lab architecture

## Layers and scope

```text
PySide6 desktop shell
    ├── Live Workbench (single-camera preview, recording, and persistent performance panel)
    ├── Multi-Camera (two independent live V4L2 capture slots; Pass 1)
    ├── History (experiments and imported measurements)
    ├── Analysis (read-only timing analysis and two-run comparison)
    ├── Exports (future tab)
    ├── camera backend boundary
    │     ├── V4L2/OpenCV capture worker (implemented)
    │     └── CHDKPTP process adapter (future)
    └── application services
          ├── camera discovery and identity registry
          ├── independent capture / preview / processing stages
          ├── bounded experiment recorder
          ├── grouped capture-event adapter/coordinator (development-gated; not UI-wired)
          ├── asynchronous experiment analysis
          ├── asynchronous searchable History model
          └── SQLite repository and CSV importer
```

The core owns camera discovery, capture and preview, experiment metadata and timing, and local history. The first device backends are native V4L2 cameras. The single-camera Workbench retains known tested presets; the Multi-Camera workspace enumerates each selected device's reported modes before offering them. Canon CHDKPTP integration remains future work behind the backend boundary.

Specialized computer-vision experiments should be optional extensions. An extension may receive frames through its own bounded processing stage and emit metrics with the `processing` stream scope. It must not block the camera-owning thread or Qt event loop, and its packages, models, and algorithm-specific controls must stay outside the core dependency set. Wardrobe inventory is a separate application, not a Camera Exploration Lab module.

## Dependencies

| Area | Choice | Status |
| --- | --- | --- |
| Data models, CSV, logs, CLI | Python standard library | Implemented |
| Storage | Python `sqlite3` | Schema v2 is active; v3 grouped schema is synthetic-test-only and not enabled at startup |
| V4L2 discovery | sysfs identity plus read-only `VIDIOC_QUERYCAP` | Implemented; image-capture nodes only |
| Capture-mode enumeration | read-only `VIDIOC_ENUM_FMT`, `VIDIOC_ENUM_FRAMESIZES`, and `VIDIOC_ENUM_FRAMEINTERVALS` | Implemented in Multi-Camera; only reported discrete options and range endpoints are offered |
| Desktop | PySide6 | Optional `desktop` extra; Live Workbench, History, and Analysis implemented |
| Graphs | PyQtGraph | Optional `desktop` extra; interval/FPS plots and interval-distribution comparison implemented |
| Frame capture and processing | NumPy + `opencv-python-headless` | Optional `desktop` extra; V4L2 capture worker implemented |
| Canon remote control | CHDKPTP process adapter | Future backend; no Canon control is part of this phase |

The headless OpenCV wheel avoids bundling a second Qt runtime alongside PySide6. Linux video nodes and USB identity are read through sysfs. The core does not require `v4l2-ctl` or a running database server.

## Identity and discovery

`V4L2Discovery` uses sysfs for identity and briefly opens each present node read-only to issue `VIDIOC_QUERYCAP`; it does not set camera controls. When the driver advertises `V4L2_CAP_DEVICE_CAPS`, discovery uses that effective per-node mask instead of the broader `capabilities` mask. Only `VIDEO_CAPTURE` and `VIDEO_CAPTURE_MPLANE` nodes appear in the camera selector. Metadata-only nodes are filtered out, while valid virtual capture devices such as OBS Virtual Camera remain eligible. If a `/dev/v4l/by-id` alias points to a node, that alias becomes the stable identifier. The current `/dev/videoN` node is saved separately as the open path because its number can change. Without a by-id alias, discovery derives an endpoint identity from USB IDs plus serial or port topology and the V4L2 interface index, then falls back to the sysfs device path.

The camera model supports `v4l2`, `chdkptp`, and `unknown` backends. A future CHDKPTP adapter will expose cameras through the same selection and session APIs; the installed presence of a CHDKPTP executable is not treated as proof that a camera is connected.

## Capture, preview, and measurement ownership

The camera-owning OpenCV worker runs on a dedicated thread. It overwrites a one-frame latest-frame slot for preview, so a slow UI skips stale display frames instead of building a frame queue. A high capture rate therefore does not require the UI to render at that rate. Preview FPS is calculated from frames rendered by the UI; capture FPS is calculated from successful `VideoCapture.read()` returns. Future image-processing stages report their own FPS.

The Multi-Camera workspace composes two instances of the same `OpenCVCaptureWorker` used by the single-camera workbench; it does not create a second capture engine. Each instance owns one V4L2/OpenCV handle, capture clock, read-failure count, requested mode, negotiated readback, and one-frame latest buffer. The Qt workspace polls both snapshots on its GUI timer and converts at most one latest frame per slot per update. New captures overwrite stale preview frames; the UI never queues every captured frame. OpenCV format decode occurs inside each independent worker's `read()`; GUI image conversion and scaling occur on the GUI thread after bounded handoff.

Device choices are based on effective capture capabilities and read-only V4L2 format, frame-size, and frame-interval enumeration. Numeric FPS choices come from intervals reported by the selected endpoint; if the driver reports a continuous or stepwise interval range, only the advertised boundary rates are offered. If no rate is reported, the UI can request driver-default FPS without asserting a numeric request. A mode is a request, and OpenCV driver readback plus observed host throughput remain separate. Known-camera labels only influence default selection among modes the endpoint reported. Endpoint duplication is checked using the real path behind the selected by-id alias with the current node as fallback, so different symlink names for the same node cannot be selected together.

Each live slot has independent Start/Stop controls and metrics. Start Both returns after spawning both worker threads, then waits for each thread's running or failed state before reporting full or partial success. A failure or disconnect in one stream does not stop the other. Recovery is deliberate: refresh discovery and press Start again. No automatic reconnect or USB reset is attempted. The slot splitter is horizontal when wide and stacks vertically on narrower windows. Pass 1 is live preview only; it does not expose paired recording in the GUI or change the application's schema-v2 startup behavior.

The streams may be requested together, but they are not hardware synchronized. Their host arrival timestamps are generated independently after each OpenCV read returns; a shared Start Both button does not align sensor exposure. Capture FPS measures successful returns in each worker, preview FPS measures frames converted/presented by each GUI pane, frame count counts successful returns in that worker, and read failures count unsuccessful reads. Elapsed capture time begins when the selected driver configuration has been read back. None of these is a sensor timestamp or absolute latency measurement.

The existing single-camera experiment recorder receives timestamp-only events through a bounded queue of at most 8,192 items. A dedicated writer stores measurements in batches of at most 128 rows. Frame objects are not sent to that queue. If the timing queue fills, recording is interrupted while camera capture continues. Manual stop, duration completion, capture loss, application shutdown, and database errors have explicit statuses and termination reasons.

CEL-013 Pass 2B.2 adds an exclusive, removable timing-listener subscription to `OpenCVCaptureWorker` and a `CaptureEventAdapter`/`GroupedCaptureRecordingBridge` that can forward each worker's post-`read()` scalar event directly to the existing grouped coordinator. It does not create another capture loop, pass images, add per-frame Qt signals, or modify the latest-frame preview slot. The bridge's lifecycle methods are synchronous and must run on a non-GUI orchestration thread. The Multi-Camera UI does not import or construct it; there is still no user-facing Record Both control.

The only runnable grouped-adapter entry point is `tools/benchmark_capture_event_adapter.py`. It requires `--synthetic-only`, creates a fresh marked schema-v3 database inside a temporary directory, accepts no database path, and removes that directory on exit. It never calls OpenCV or discovers devices. Normal `Database.initialize()` remains schema v2. Pass 2B.3 adds cooperative shared OS ownership to the existing single-camera recorder and grouped coordinator, but the adapter remains disconnected from the GUI and cannot be pointed at the application's existing database; production schema activation and grouped UI recording remain separately gated.

For a recording started while preview is already running, the first stored frame arrival can carry an interval from before the experiment began. That boundary interval is excluded from the interval summary; subsequent intervals join two arrivals within the recording. Capture, preview, processing, transport, camera-reported, and unknown measurements use separate stream scopes.

Neither the duration of `read()` nor the time between host-side frame returns establishes sensor exposure latency or sensor-to-display latency. The recorded UTC arrival timestamp is taken after `VideoCapture.read()` returns; the monotonic clock is used for intervals and elapsed time.

## SQLite records and provenance

- `cameras` stores stable identity, backend, current device path, and identity source.
- `sessions` groups a live experiment or imported source.
- `benchmark_runs` stores a run's mode, lifecycle status, and configuration.
- `setting_snapshots` stores requested and driver-reported values separately, including the readback method and verification status.
- `measurements` stores raw timestamped values, units, sample index, interval, method, and stream scope.
- `benchmark_summaries` stores frame count, read failures, duration, capture FPS, preview FPS, the reserved processing FPS field, and interval percentiles.
- `raw_records` stores each parsed source CSV row as JSON, linked to normalized measurements.
- `import_sources` stores source path, SHA-256, file size, import time, and row counts. `(source_path, sha256)` prevents duplicate imports while allowing changed source contents to be imported as a new version.

Schema version 2 adds the summary table and indexes without rewriting earlier imported rows. A version 1 database is upgraded on initialization. Stale live experiments are recovered as interrupted on startup; after a crash, their duration is bounded by the last committed timing sample because the exact stop time is unavailable.

### CEL-013 Pass 2A grouped schema foundation

Pass 2A adds an explicitly invoked, synthetic-only schema-v3 migration and grouped-session repository. The production application remains on SCHEMA_VERSION = 2: ordinary initialization does not call the v3 migration, and it continues to reject a database newer than v2. The v3 entry point requires a marked synthetic test database and is not exposed through the GUI or CLI. No real user database has been migrated.

The grouped model reuses sessions as one experiment parent and gives it two benchmark_runs, mapped to camera streams A and B through benchmark_streams. Grouped membership is explicit; legacy single-camera and imported sessions remain unchanged and retain their existing sessions.camera_id meaning. Stream-level provenance includes host monotonic elapsed nanoseconds, timestamp source/clock/collection point, timing method, identity snapshot, independent run/settings/summary, and distinct read-failure, queue-loss, persistence-loss, detected sample-sequence-gap and preview-skip counters. Sequence gaps mark the dataset incomplete but do not prove camera or sensor drops. OpenCV driver timestamp fields remain NULL unless actually provided by a future backend.

The future coordinator contract is nonblocking and bounded: every rejected queue sample increments a stream-local loss counter, later enqueue attempts continue when capacity returns, sequence gaps remain visible, and any lost timing sample makes the dataset incomplete. Queue gaps are persistence loss, not evidence of sensor or driver dropped frames. The repository schema and API do not yet connect to capture workers, add recording UI, enable a writer lease end-to-end, or save media. See [Grouped Recording Schema](docs/GROUPED_RECORDING_SCHEMA.md).

The local database can contain camera serial numbers, machine-specific source paths, experiment names, and notes. It is user data and must not be committed or shared without review. New local databases default to the XDG data directory. Source CSV files are read in place; the importer checks that a file's content hash is stable during import and commits one source atomically.

### CEL-013 Pass 2B.1 synthetic grouped coordinator

`grouped_recording_coordinator.py` composes the schema-v3 `GroupedExperimentRepository` without importing capture, OpenCV, or Qt. It takes one bounded metadata queue per child run and writes fair batches through one dedicated daemon SQLite writer. Per-stream CEL sequence numbers are assigned before nonblocking enqueue; full queues count timing-persistence loss and future submissions continue when space returns. A database write failure is terminal for the coordinator, counts accepted but uncommitted samples as persistence loss, attempts an incomplete marker, and never claims successful finalization.

The schema-v3 lease is acquired transactionally before the writer starts. Grouped repository writes verify the session and owner token in an immediate write transaction, and finalization plus lease release commit atomically. Stale leases are not stolen automatically. This contract protects grouped coordinator instances only; the existing schema-v2 single-camera recorder does not participate in the lease. The coordinator remains synthetic-only and is not wired to live workers, the Multi-Camera UI, History, Analysis, or normal database startup. See [Grouped Recording Coordinator](docs/GROUPED_RECORDING_COORDINATOR.md) for queue limits, failure states, timing semantics, and measured synthetic persistence results.

## Historical analysis

The Analysis workspace reads the existing schema version 2 through a SQLite URI opened in read-only mode with query-only enabled. It does not initialize or migrate the database, and the Phase 2B follow-up adds no schema changes. A dedicated Qt worker performs the queries so larger histories do not block the event loop. Camera-family filters use USB vendor/product IDs when known, with normalized model names as a fallback; these keys are for browsing only and never update or merge stored identities. The exact camera key, identity source, and an opaque short fingerprint remain visible in selectors and technical details. Session A and Session B default to different runs from the same family when available; cross-camera comparison remains available.

History uses a Qt table model and sort/filter proxy. It searches the session label, camera model, record source, notes, capture configuration and settings, requested FPS, date, and status. Search is debounced in the UI; history indexing and selected-record detail queries use background workers and query-only database connections. The Analyze selected action maps the current sorted/filtered row back to its source model record and opens that exact session. Filtering and sorting are in-memory presentation operations and do not change historical records.

The interval timeline uses persisted capture-scoped frame_interval_ms values, falling back to interval nanoseconds on frame_arrival rows when needed. When recorder elapsed values are present they supply the horizontal coordinate; otherwise the chart uses cumulative persisted intervals and says so on the axis. The first interval can be absent when recording began during preview because its earlier endpoint may be outside the experiment boundary.

Frame-interval min, max, mean, median, p95, and p99 are calculated from stored sample values. The median averages the two center values for an even sample count; p95 and p99 use the nearest-rank rule, ceil(p × n). The FPS curve groups interval endpoints into non-overlapping windows. Each point is the number of recorded interval transitions divided by their summed duration in that window; the target width is one second and widens for very long runs to keep the plotted curve bounded. A window containing only zero-duration intervals has no defined rate. Mean delivered capture FPS is derived from all persisted intervals when available. If raw intervals are absent, only explicitly capture-scoped saved FPS samples or the saved run summary are used.

Requested driver frame rate, driver-reported rate, observed capture rate, GUI preview rate, and processing rate remain separate. The curve estimates host frame-arrival throughput. Recorded UTC timestamps are taken after OpenCV read returns, and intervals use host monotonic time; neither reveals sensor exposure timing, cross-camera synchronization, or sensor-to-display latency.

Chart data is streamed from SQLite and reduced to per-bucket minima and maxima, retaining visible spikes and global extrema with a fixed point budget. Percentiles are queried exactly from SQLite. Session FPS curves are overlaid only when both runs have recorder elapsed timestamps. Each curve keeps its own sample times and duration; no interpolation or simultaneous-capture claim is made. Percentage deviation is 100 × (window FPS − that session's mean FPS) / that session's mean FPS, so zero is the session mean. Plot redraws discard stale legends and explicitly re-range after data or display-mode changes; Fit to Data uses visible finite points, Reset View restores the recording duration, and zoom/pan never change stored samples. Interval timelines overlay only when elapsed-time basis, stream scope, and timing method match. Shared log histogram bins show normalized positive intervals with visible point markers; zero samples remain in summaries and are called out because zero cannot be placed on a log axis.

For imported legacy CSV data, a zero interval in the first interval row with method `legacy_csv:interval_ms` is treated as a boundary marker: no earlier frame exists for that row. The normalized measurement and original raw CSV row remain in SQLite, while that one marker is excluded from derived interval statistics and FPS calculations. Other zero values, including live-recorded zeros, remain part of calculations and are reported separately from positive histogram bins. No database schema change is required.

The Analysis page uses a vertical scroll area and adjustable plot splitters. Metric cards stay compact; driver details, timing methods, timestamp scope, interval exclusions, and notes are expanded on demand. Short plot titles and abbreviated comparison headers avoid clipping, with tooltips and horizontal table scrolling for full labels.

Metric cards select foreground text against the current Qt button-surface palette and choose a foreground with at least 4.5:1 contrast when the standard black/white candidates allow it. Their contextual help buttons open the Metrics Guide at the relevant topic; the full guide is also available from both Analysis and History. The guide labels each example as synthetic and explains units, interpretation, misunderstandings, and the formula or measurement method. Its latency guidance explicitly excludes OpenCV read durations and frame intervals as sensor-latency measurements.

### Historical camera identity groups

Read-only inspection of the local history showed two PlayStation Eye camera rows for two different provenance paths. Imported legacy CSVs use an identity inferred from the source path and legacy camera profile, while native V4L2 recordings use the discovered `/dev/v4l/by-id` stable endpoint. The legacy row has no serial-number evidence, so equal model names or USB IDs do not prove that both identity records refer to the same physical unit. No records were merged. The interface groups these records into one model family for browsing by shared USB vendor/product IDs, while source labels and opaque fingerprints keep the two stored identity groups distinguishable. Users can compare across the family without the application asserting physical-unit equivalence.

See [Related Projects](docs/RELATED_PROJECTS.md) for reviewed neighboring tools, areas of overlap, and potential interoperability points. Specialized calibration, camera control, latency testing, astronomy analysis, and computer-vision workflows remain outside the core dependency surface unless added behind optional extension boundaries.

## Bazzite packaging and launch

Install the desktop extra into a per-user virtual environment. The Qt application runs on the KDE host and reads host V4L2 devices. Do not install application dependencies into the immutable host image for this project. For offline setup, build and retain a wheelhouse for the target Python version and architecture, then install with pip's `--no-index` option. The tested launcher instructions are in [README.md](README.md).

The Live Workbench gives roughly 65–70% of the main area to preview and keeps capture FPS, preview FPS, frame interval, read failures, and settings visible in a persistent side panel. Recording status and progress stay in the workbench. History and Analysis have their own tabs; a selected History run can open directly in Analysis. Exports remain future work.

### CEL-013 Pass 2B.3 — shared recording ownership and recovery

The v2 single-camera recorder and v3 grouped coordinator now use one cooperative Linux `flock(LOCK_EX | LOCK_NB)` on the opened SQLite database inode. The OS lock handles same-process and cross-process contention, including symlink and hardlink aliases to the same inode. The v3 grouped coordinator also retains its transactional `recording_leases` row; the OS lock is acquired first, then the SQL lease, then short measurement/event transactions. Finalization commits the terminal experiment state and deletes the SQL lease in one transaction before releasing the OS lock. Lock ordering is always OS inode lock → SQLite transaction; code must not wait for the file lock while holding a SQLite transaction.

The existing v2 schema remains the normal startup schema. No lease table is added to v2 and `Database.initialize()` does not migrate to v3. The existing single-camera recorder stores its unique owner token in session metadata and checks its handle on live-run creation, active sample batches, and finalization. Direct writes targeting an active `live_experiment` run without the matching handle are rejected. The grouped coordinator verifies the same inode owner before each writer transaction and uses its owner token for the existing durable SQL lease. Recovery inspection is read-only by default: `Database.inspect_recording_recovery()` probes the owner lock and reports session/run IDs, statuses, stream counts, last committed event markers, loss counters, and conservative classifications. It never clears v3 leases. Existing v2 startup recovery is serialized under the same lock and is skipped if a current CEL owner is active.

The file lock is cooperative and applies to local Linux filesystems with reliable `flock` semantics. Older CEL versions and unrelated SQLite writers do not honor it, so it cannot protect against them. An abandoned v2 live run is handled by the existing startup recovery; a stale v3 SQL lease blocks new recording until a separately approved recovery operation exists. No code in this milestone automatically steals leases, activates v3 in the GUI, or enables grouped recording there. Failure to commit finalization keeps the owner handle and v3 SQL lease unresolved; process exit releases the kernel lock but does not erase durable recovery evidence.

The `GroupedRecordingCoordinator.create_and_start()` factory acquires shared ownership before inserting parent or child rows. If the durable lease cannot be acquired before a writer starts, it transactionally removes only that factory-created pending group; uncertain cleanup keeps the OS lock held. The lower-level grouped repository remains useful for synthetic schema tests and is not an activated production recording entry point. Single-camera and grouped recording are mutually exclusive against databases opened by the current shared-owner code. A v3 database is still not accepted by ordinary GUI initialization, so cross-recorder enforcement is a tested repository/coordinator capability rather than an enabled desktop workflow.

`flock` is not a universal guarantee for network filesystems, copied database files, old executables, or arbitrary external tools. Production v3 activation remains gated on an explicit migration flow, a verified backup, exclusive local access, pre/post integrity checks, and deliberate recovery review. No real history database was read or migrated for this milestone.
