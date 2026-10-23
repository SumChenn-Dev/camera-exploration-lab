# Camera Exploration Lab

Camera Exploration Lab is an offline-first Linux desktop workbench for discovering cameras and running reproducible capture experiments. It is built with Python, PySide6, OpenCV/V4L2, and SQLite.

## Current scope

The current application includes:

- Read-only V4L2 discovery with `/dev/v4l/by-id` identity preferred when available.
- A preview-first Live Workbench for V4L2 cameras, with the preview occupying about two thirds of the primary area and persistent capture/preview diagnostics beside it.
- Camera selection and tested single-camera mode profiles for the Sony PlayStation Eye and AVerMedia Live Streamer CAM 313.
- A separate Multi-Camera tab with two independent V4L2 workers, mode choices enumerated from each device, independent status and metrics, and side-by-side or stacked previews.
- Independent camera capture and GUI preview loops. Each dual preview reads from a one-frame overwrite slot, so 187 FPS capture does not require 187 GUI updates or create an unbounded queue.
- Fixed duration and manual experiment recording, with requested settings, driver readback, notes, timestamps, summary statistics, and raw timing samples stored in SQLite.
- A searchable History tab for local experiment sessions and imported CSV measurements, with camera-family/source filters and stable Analyze-selected navigation.
- An Analysis tab with camera/mode/requested-FPS filters, searchable Session A/B selection, summary cards, timing curves, interval distributions, and same-camera comparisons by default.
- An integrated Metrics Guide available from Analysis and History, with plain-language definitions, units, labeled examples, interpretation guidance, common pitfalls, and measurement methods.
- A Performance Monitor tab with live, read-only CEL capture telemetry and selectable historical replay of six published Canon CHDKPTP benchmark CSVs. The replay is explicitly labeled historical and never opens a camera.

The Analysis tab can be opened from History with the Analyze selected button. It shows a compact experiment header and labeled cards for observed capture FPS, GUI preview FPS, processing FPS when recorded, frame count, interval percentiles, and read failures. The capture plots provide Fit to Data, Reset View, Zoom In/Out, mouse-drag panning, and Expand Plot/Restore Layout controls. At narrower window widths the capture-rate and interval plots stack vertically. The optional deviation axis uses 100 × (window rate − that session's mean) / that session's mean; zero represents the session mean. Open Metrics Guide or use a card's `?` help button for definitions and limitations. Expand Technical details for requested and driver-reported settings, timing methods, timestamp scope, duration source, and notes. Filters and search narrow Session A and B; a different recording from the same model family is selected by default when available. Use Swap A / B or Clear B to change the comparison. Camera-family browsing does not merge stored identities: identity-source labels and opaque fingerprints preserve their provenance.

With two sessions selected, the Analysis tab overlays capture-FPS curves only when both have recorder elapsed timestamps. Different durations and sampling densities retain their own sample points; no interpolation or simultaneous-capture claim is made. The optional deviation view expresses each window relative to that session's observed mean and formats ticks as percentages. Session A and Session B use separate colors, line styles, and legend labels. Interval timelines can be shown or hidden independently and overlay only when timing scope and method match. Interval distributions use shared logarithmic bins for positive values; zero samples are reported separately because a log axis cannot display zero.

Analysis distinguishes requested settings, driver readback, observed capture throughput, GUI preview cadence, and future processing throughput. Its FPS curve uses non-overlapping windows of recorded frame intervals (one second by default; longer windows for very long recordings). Imported or incomplete records may have summary-only values and are labeled accordingly. Frame-arrival timestamps are host observations after OpenCV returns; neither they nor frame intervals measure sensor exposure or sensor-to-display latency. Analysis opens SQLite read-only and does not change the local database. Raw video is not retained by the recorder.

Multi-camera recording and its experiment/database model, exports, extensive camera controls, and Canon CHDKPTP support are future work. The core application is for camera research. Specialized computer-vision experiments will use optional extension packages with their own dependencies and processing metrics. Wardrobe inventory is a separate application and is outside this project.

## Performance Monitor (CEL-014 Phase 1)

The Performance Monitor defaults to **Historical Replay**. Select one of the six published Canon SX40 HS benchmark datasets, then use Play, Pause, and Reset to move through its recorded rows. The bundled CSVs are integrity-checked against their public manifest. Stage-local `elapsed_s` values reset between benchmark stages; the replay chart concatenates those stages for playback only and does not present that axis as a common wall clock. The historical mode never opens a camera and does not turn CSV values into live readings.

**Live CEL Capture** reads a bounded scalar telemetry ring on an existing OpenCV capture worker. It does not open or discover cameras, add a camera-reading loop, enqueue one Qt signal per frame, or replace the recorder's exclusive timing listener. The view separates requested FPS, successful OpenCV reads/s, CEL GUI preview updates/s, host `read()` call duration, and host frame intervals. OpenCV read-call duration includes backend and driver waiting; it is not USB-only latency. Preview updates are not physical display presentations. Neither measure establishes sensor exposure rate or sensor frame drops.

Historical CHDKPTP payload fetches/s, GUI-reported FPS, threshold-classified content changes/s, host fetch-call duration, poll attempts, and software skips retain distinct labels and source provenance. Payload fetches are not unique sensor exposures; the published content-change threshold is not fully specified; CHDKPTP fetch-call duration is not physical USB latency. See [CEL-014 Phase 1 Telemetry](docs/CEL-014_PHASE1_TELEMETRY.md) for the contract and future adapter boundary. Live Canon telemetry is not implemented.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the component boundaries and [docs/EXPLORATION_NOTEBOOK.md](docs/EXPLORATION_NOTEBOOK.md) for ongoing ideas, findings, and design decisions.
The [development log](docs/DEVELOPMENT_LOG.md) summarizes completed milestones. See the [Konsole development workflow](docs/DEVELOPMENT_WORKFLOW.md) for tests, read-only release review, and deliberate GitHub publication. The [initial snapshot audit](docs/PUBLICATION_AUDIT.md) documents the separate one-time privacy check.

## Install on Bazzite

The canonical Bazzite checkout is `$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot`. The existing host setup has been verified against this public checkout, including its editable Python install and KDE launcher. For a new machine, clone the public repository there and run the fresh setup below. Do not recreate the verified host's virtual environment as part of normal development.

```bash
git clone https://github.com/SumChenn-Dev/camera-exploration-lab.git \
  "$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
cd "$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
```

For a fresh Bazzite setup, use a virtual environment in the current user's data area. This leaves the immutable host image and other containers unchanged:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e ".[desktop]"
```

The optional `desktop` extra provides PySide6, PyQtGraph, NumPy, and `opencv-python-headless`. Camera discovery and the data model use the Python standard library and SQLite. No sudo pip, host package installation, or container configuration is needed.

### Offline installation

Build the wheelhouse on a network-connected Linux machine with the same Python version and CPU architecture as the offline Bazzite host. Keep that wheelhouse unchanged to reproduce the same dependency set:

```bash
WHEELHOUSE="$HOME/celab-wheelhouse"
mkdir -p "$WHEELHOUSE"
python3 -m pip download --only-binary=:all: --dest "$WHEELHOUSE" \
  "setuptools>=68" "PySide6>=6.7" "pyqtgraph>=0.13.7" \
  "numpy>=1.26" "opencv-python-headless>=4.9"
sha256sum "$WHEELHOUSE"/*.whl > "$WHEELHOUSE/SHA256SUMS"
```

Copy the wheelhouse to the offline host. From the project checkout, install with:

```bash
WHEELHOUSE="$HOME/celab-wheelhouse"
VENV="$HOME/.local/share/camera-exploration-lab/venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --no-index --find-links "$WHEELHOUSE" "setuptools>=68"
"$VENV/bin/python" -m pip install --no-index --find-links "$WHEELHOUSE" \
  --no-build-isolation -e ".[desktop]"
```

Keep the wheelhouse and its checksum file with the setup notes. Rebuild it only when intentionally updating the dependency set.

## KDE launcher

For a fresh installation, create a per-user KDE application entry. Choose a new database path for a new history, or set DATABASE to an existing local history database if you are continuing earlier experiments. Keep that file and its backups in place; do not copy them into the checkout. The verified Bazzite launcher already passes the existing database path.

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
APP_DIR="$HOME/.local/share/applications"
DATABASE="${XDG_DATA_HOME:-$HOME/.local/share}/camera-exploration-lab/history.sqlite3"
mkdir -p "$APP_DIR" "$(dirname "$DATABASE")"
cat > "$APP_DIR/camera-exploration-lab.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Camera Exploration Lab
Comment=Discover cameras and record capture experiments
Exec="$VENV/bin/celab" --database "$DATABASE" gui
Path=$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot
Terminal=false
Categories=Science;Utility;
EOF
chmod 644 "$APP_DIR/camera-exploration-lab.desktop"
```

If you already have a working launcher, leave its `--database` argument unchanged. For a manual launch of the same history, use `celab --database "$DATABASE" gui` with that existing database path. The app reads host V4L2 devices directly; use `celab scan-v4l2` from the virtual environment to inspect discovered endpoints.

## Local data and privacy

By default, the SQLite history is stored under `$XDG_DATA_HOME/camera-exploration-lab/history.sqlite3`, or `~/.local/share/camera-exploration-lab/history.sqlite3` when `XDG_DATA_HOME` is unset. Logs are stored under `$XDG_STATE_HOME/camera-exploration-lab/` or `~/.local/state/camera-exploration-lab/`.

The database may contain camera serial numbers, local source paths, experiment names, and notes. Keep databases, backups, CSV/JSON exports, captured images, and other experiment outputs in ignored local data directories. Preserve an existing research database at its current location and pass that same path with `--database`; do not move it into the checkout. Review and redact any database or log before sharing it. The application does not upload data.

To import a CSV file or directory into a selected local database:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
"$VENV/bin/celab" --database "$HOME/.local/share/camera-exploration-lab/history.sqlite3" \
  import-csv /path/to/your/csv-folder
```

The importer reads the source files without moving or editing them. Reimporting the same unchanged file is idempotent.

## Search History and inspect experiments

Launch Camera Exploration Lab through the KDE launcher, or use the existing virtual environment and the same database argument already configured in the launcher. History search covers experiment names, camera, source, notes, capture mode, requested FPS, date, and status where available. Camera-family and source filters can be combined; Clear filters resets them. Sorting and filtering do not edit stored records. Select a row and choose Analyze selected, or open Analysis and select a run directly. Choose a second run in Session B to see compatible elapsed-time curves, interval-distribution comparison, and the metric table.

For host visual review, check the timeline units and displayed interval count against the selected run, compare capture and preview rates separately, and confirm that requested and driver-reported modes remain distinct. A raw interval plot may be unavailable for summary-only imports; those records show stored fields without reconstructing samples. History and Analysis use read-only connections for browsing and leave the existing history file in place. See [Related Projects](docs/RELATED_PROJECTS.md) for complementary tools and scope boundaries.

## Multi-Camera live preview (CEL-013 Pass 1)

The Multi-Camera tab supports two independent V4L2 live streams. It enumerates capture formats, sizes, and advertised frame intervals using read-only V4L2 ioctls; only reported options appear. The driver readback is shown separately from the request, and delivered capture FPS can be lower than the requested value. Each stream has its own start/stop controls, capture worker, latest-frame slot, preview rate, frame count, failures, and elapsed host time. Start Both waits for both streams to confirm independently and reports partial startup if only one succeeds.

The two streams share one user action but are not hardware-synchronized. Host frame arrival does not establish sensor exposure timing or absolute latency. Preview rendering runs at the GUI refresh cadence and can skip captured frames. Pass 1 provides live preview only; use Single Camera Workbench for recording. The grouped schema, coordinator, and grouped History/Analysis are demonstrated only through the explicitly acknowledged fake-camera harness below. Normal startup remains schema v2, and the production GUI has no grouped Record Both control or production schema-v3 activation. The owner-validated dual preview does not validate grouped recording. Grouped video recording is unavailable.

## Tests

Tests use synthetic timings, generated CSVs, temporary SQLite databases, and mocked cameras. They do not require a connected camera:

```bash
cd "$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
VENV="$HOME/.local/share/camera-exploration-lab/venv"
PYTHONPATH=src QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -m unittest discover -v -s tests
```

## Inspect a synthetic grouped experiment

The schema-v3 grouped History/Analysis viewer is currently available only inside the explicitly acknowledged fake-camera harness. It does not change normal History or Analysis, which remain on schema v2. From the repository root on Bazzite:

```bash
QT_QPA_PLATFORM=xcb \
  "$HOME/.local/share/camera-exploration-lab/venv/bin/python" \
  tools/run_synthetic_recording_ui.py \
  --synthetic-only --acknowledge-synthetic-only
```

Check the in-window synthetic-only acknowledgement, start the fake streams, record or finish a group, choose **Inspect Synthetic Experiment**, then choose **Open in Grouped Analysis**. The harness uses only its own temporary database, refuses database-path arguments, and removes that database on clean shutdown. **Keep synthetic capture running after recording finishes** is unchecked by default: natural fixed-duration completion commits the recording and then stops both fake sources. Checking it leaves synthetic capture running; manual early **Stop Recording** also leaves capture running. These synthetic flows do not validate real grouped recording. See [Grouped History and Analysis](docs/GROUPED_HISTORY_ANALYSIS.md) for measurement definitions and limitations.

Schema-v3 compatibility and backup/migration rehearsal remain development-only. Normal `celab gui` continues to use schema v2 and rejects v3; no production migration or grouped recording activation is available. For an isolated synthetic migration and read-compatibility rehearsal, use the no-path commands in [Schema-v3 Readiness](docs/SCHEMA_V3_READINESS.md). They create, verify, and remove synthetic databases under temporary directories and do not select the configured application history file.

Physical verification must run in a Bazzite KDE host session, since a container or sandbox may not have access to `/dev/video*`:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
"$VENV/bin/celab" scan-v4l2
LAUNCHER="$HOME/.local/share/applications/camera-exploration-lab.desktop"
DATABASE="$(sed -n 's/.*--database "\([^"]*\)".*/\1/p' "$LAUNCHER")"
test -n "$DATABASE"
"$VENV/bin/celab" --database "$DATABASE" gui
```

In the Live Workbench, select a camera and mode, start preview, and check the driver-reported settings. Camera discovery checks each node's effective V4L2 device capabilities, so metadata-only endpoints are omitted while genuine virtual capture devices remain eligible. Start a 10, 30, 60, or 120 second experiment and confirm that the preview continues while recording. Then inspect the result and any imported sessions in History. Do not treat read duration or frame interval as sensor latency.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for scope, test, and data-handling guidance. The project is distributed under the [MIT License](LICENSE), with SumChenn-Dev as the copyright holder.
