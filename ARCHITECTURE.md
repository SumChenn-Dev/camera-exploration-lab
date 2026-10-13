# Camera Exploration Lab architecture

## Layers and scope

```text
PySide6 desktop shell
    ├── Live Workbench (preview + persistent performance panel)
    ├── History (experiments and imported measurements)
    ├── Analysis / Exports (future tabs)
    ├── camera backend boundary
    │     ├── V4L2/OpenCV capture worker (implemented)
    │     └── CHDKPTP process adapter (future)
    └── application services
          ├── camera discovery and identity registry
          ├── independent capture / preview / processing stages
          ├── bounded experiment recorder
          └── SQLite repository and CSV importer
```

The core owns camera discovery, capture and preview, experiment metadata and timing, and local history. The first device backends are native V4L2 cameras: the Sony PlayStation Eye and AVerMedia Live Streamer CAM 313 have tested mode profiles. Canon CHDKPTP integration remains future work behind the backend boundary.

Specialized computer-vision experiments should be optional extensions. An extension may receive frames through its own bounded processing stage and emit metrics with the `processing` stream scope. It must not block the camera-owning thread or Qt event loop, and its packages, models, and algorithm-specific controls must stay outside the core dependency set. Wardrobe inventory is a separate application, not a Camera Exploration Lab module.

## Dependencies

| Area | Choice | Status |
| --- | --- | --- |
| Data models, CSV, logs, CLI | Python standard library | Implemented |
| Storage | Python `sqlite3` | Implemented; schema version 2 adds summaries and indexes |
| V4L2 discovery | `/sys/class/video4linux`, `/dev/v4l/by-id` | Implemented read-only |
| Desktop | PySide6 | Optional `desktop` extra; Live Workbench and History implemented |
| Graphs | PyQtGraph | Optional `desktop` extra; detailed graph views are future work |
| Frame capture and processing | NumPy + `opencv-python-headless` | Optional `desktop` extra; V4L2 capture worker implemented |
| Canon remote control | CHDKPTP process adapter | Future backend; no Canon control is part of this phase |

The headless OpenCV wheel avoids bundling a second Qt runtime alongside PySide6. Linux video nodes and USB identity are read through sysfs. The core does not require `v4l2-ctl` or a running database server.

## Identity and discovery

`V4L2Discovery` enumerates sysfs video nodes without opening devices or setting controls. If a `/dev/v4l/by-id` alias points to a node, that alias becomes the stable identifier. The current `/dev/videoN` node is saved separately as the open path because its number can change. Without a by-id alias, discovery derives an endpoint identity from USB IDs plus serial or port topology and the V4L2 interface index, then falls back to the sysfs device path.

The camera model supports `v4l2`, `chdkptp`, and `unknown` backends. A future CHDKPTP adapter will expose cameras through the same selection and session APIs; the installed presence of a CHDKPTP executable is not treated as proof that a camera is connected.

## Capture, preview, and measurement ownership

The camera-owning OpenCV worker runs on a dedicated thread. It overwrites a one-frame latest-frame slot for preview, so a slow UI skips stale display frames instead of building a frame queue. A high capture rate therefore does not require the UI to render at that rate. Preview FPS is calculated from frames rendered by the UI; capture FPS is calculated from successful `VideoCapture.read()` returns. Future image-processing stages report their own FPS.

The experiment recorder receives timestamp-only events through a bounded queue of at most 8,192 items. A dedicated writer stores measurements in batches of at most 128 rows. Frame objects are not sent to that queue. If the timing queue fills, recording is interrupted while camera capture continues. Manual stop, duration completion, capture loss, application shutdown, and database errors have explicit statuses and termination reasons.

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

The local database can contain camera serial numbers, machine-specific source paths, experiment names, and notes. It is user data and must not be committed or shared without review. New local databases default to the XDG data directory. Source CSV files are read in place; the importer checks that a file's content hash is stable during import and commits one source atomically.

## Bazzite packaging and launch

Install the desktop extra into a per-user virtual environment. The Qt application runs on the KDE host and reads host V4L2 devices. Do not install application dependencies into the immutable host image for this project. For offline setup, build and retain a wheelhouse for the target Python version and architecture, then install with pip's `--no-index` option. The tested launcher instructions are in [README.md](README.md).

The Live Workbench gives roughly 65–70% of the main area to preview and keeps capture FPS, preview FPS, frame interval, read failures, and settings visible in a persistent side panel. Recording status and progress stay in the workbench. History has its own tab. Graphs, comparisons, and exports remain future tabs.
