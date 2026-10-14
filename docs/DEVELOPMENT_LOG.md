# Development Log

This log records completed engineering milestones and the public repository setup. It avoids including individual experiment measurements or local database contents.

## Phase 0 — Project foundation

- Added the Python package structure, SQLite schema, camera and session models, logging, camera discovery, and historical CSV importer.
- Preserved imported source rows alongside normalized measurements and made unchanged imports idempotent.
- Added automated tests with synthetic CSV data and temporary SQLite databases; no physical camera is needed for the test suite.

## Phase 1 — Live Workbench

- Added the preview-first PySide6 workbench with independent capture and GUI preview loops.
- Added camera selection, capture mode selection, negotiated-setting readback, and persistent capture FPS, preview FPS, frame interval, and read-failure metrics.
- Added mocked-camera tests and verified native USB capture on the Bazzite host with the Sony PlayStation Eye and AVerMedia CAM 313.

## Phase 2A — Persistent experiment recording

- Added named sessions with notes, fixed-duration and manual stop, bounded streaming timing storage, explicit completion/interruption state, summaries, and a basic History view.
- Stored requested settings separately from driver-reported settings. Capture, preview, and reserved processing measurements use separate scopes.
- Added migration and recording tests using synthetic timings and temporary databases. The 39-test suite passed during Phase 2A review.
- A completed physical recording was inspected in History while live preview continued. Detailed measurements remain in the local experiment database.

## First public repository

- Published an independent, privacy-reviewed initial snapshot on GitHub. The public checkout starts from its own root commit and does not contain the original private development history.
- The public checkout is the canonical location for future development. The original checkout and its three-milestone history remain preserved separately.
- Local databases, backups, imported measurements, and captured media remain outside Git tracking.
- Future commits require file, history, identity, and privacy review before publication. The read-only review command and camera-free Actions workflow are documented in [Development Workflow](DEVELOPMENT_WORKFLOW.md).

## Verified Bazzite public-checkout transition

- Confirmed the public remote and local checkout at the initial public commit before the transition.
- Repointed and verified the existing editable install to the canonical public checkout; dependency checks passed.
- Verified the KDE launcher opens the workbench with the existing research database, both webcams are discoverable and preview successfully, and earlier History records remain visible.
- The original private checkout and database remain preserved. No camera controls or private data were changed or copied into this repository.

## Known follow-up work

Manual interruption, physical disconnect/reconnect, and restart persistence need additional hands-on verification. Analysis graphs, comparisons, exports, broad camera controls, and the CHDKPTP backend remain future work. See the [Exploration Notebook](EXPLORATION_NOTEBOOK.md) for the maintained roadmap.
