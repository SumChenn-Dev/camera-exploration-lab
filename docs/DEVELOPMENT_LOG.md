# Development Log

This log summarizes the three completed local milestones. It describes delivered behavior and verification without embedding per-run measurements or local development history in the public snapshot.

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

## Public snapshot preparation

The first public candidate is assembled from an explicit reviewed file list and has an independent initial commit. The original three-commit development history and local experiment data remain in the original working repository; neither is copied into this snapshot.

## Known follow-up work

Manual interruption, physical disconnect/reconnect, and restart persistence need additional hands-on verification. Analysis graphs, comparisons, exports, broad camera controls, and the CHDKPTP backend remain future work. See the [Exploration Notebook](EXPLORATION_NOTEBOOK.md) for the maintained roadmap.
