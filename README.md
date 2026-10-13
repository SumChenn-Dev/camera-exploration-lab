# Camera Exploration Lab

Camera Exploration Lab is an offline-first Linux desktop workbench for discovering cameras and running reproducible capture experiments. It is built with Python, PySide6, OpenCV/V4L2, and SQLite.

## Current scope

Phase 2A provides:

- Read-only V4L2 discovery with `/dev/v4l/by-id` identity preferred when available.
- A preview-first Live Workbench for V4L2 cameras, with the preview occupying about two thirds of the primary area and persistent capture/preview diagnostics beside it.
- Camera selection and tested mode profiles for the Sony PlayStation Eye and AVerMedia Live Streamer CAM 313.
- Independent camera capture and GUI preview loops. Capture can run faster than the screen refresh without building a frame queue.
- Fixed duration and manual experiment recording, with requested settings, driver readback, notes, timestamps, summary statistics, and raw timing samples stored in SQLite.
- A History tab for local experiment sessions and imported CSV measurements.

Capture FPS, preview FPS, and future processing FPS are separate measurements. Frame arrival timestamps and intervals are host-side observations; they do not measure sensor-to-display latency. Raw video is not retained by the recorder.

Detailed analysis graphs, exports, extensive camera controls, and Canon CHDKPTP support are future work. The core application is for camera research. Specialized computer-vision experiments will use optional extension packages with their own dependencies and processing metrics. Wardrobe inventory is a separate application and is outside this project.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the component boundaries and [docs/EXPLORATION_NOTEBOOK.md](docs/EXPLORATION_NOTEBOOK.md) for ongoing ideas, findings, and design decisions.
The [development log](docs/DEVELOPMENT_LOG.md) summarizes completed milestones. The local [publication audit](docs/PUBLICATION_AUDIT.md) documents the read-only privacy and release-tree check.

## Install on Bazzite

Use a virtual environment in the current user's data area. This leaves the immutable host image and other containers unchanged. Run these commands from the project checkout:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e ".[desktop]"
"$VENV/bin/celab" gui
```

The optional `desktop` extra provides PySide6, PyQtGraph, NumPy, and `opencv-python-headless`. Camera discovery and the data model use the Python standard library and SQLite. No `sudo pip`, host package installation, or container configuration is needed.

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

After installation, create a per-user KDE application entry. This writes only to the current user's application directory:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$APP_DIR"
cat > "$APP_DIR/camera-exploration-lab.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Camera Exploration Lab
Comment=Discover cameras and record capture experiments
Exec="$VENV/bin/celab" gui
Terminal=false
Categories=Science;Utility;
EOF
chmod 644 "$APP_DIR/camera-exploration-lab.desktop"
```

The app reads host V4L2 devices directly. Use `celab scan-v4l2` from the virtual environment to inspect discovered endpoints before opening the GUI.

## Local data and privacy

By default, the SQLite history is stored under `$XDG_DATA_HOME/camera-exploration-lab/history.sqlite3`, or `~/.local/share/camera-exploration-lab/history.sqlite3` when `XDG_DATA_HOME` is unset. Logs are stored under `$XDG_STATE_HOME/camera-exploration-lab/` or `~/.local/state/camera-exploration-lab/`.

The database may contain camera serial numbers, local source paths, experiment names, and notes. Keep databases, backups, CSV/JSON exports, captured images, and other experiment outputs in ignored local data directories. Review and redact any database or log before sharing it. The application does not upload data.

To import a CSV file or directory into a selected local database:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
"$VENV/bin/celab" --database "$HOME/.local/share/camera-exploration-lab/history.sqlite3" \
  import-csv /path/to/your/csv-folder
```

The importer reads the source files without moving or editing them. Reimporting the same unchanged file is idempotent.

## Tests

Tests use synthetic timings, generated CSVs, temporary SQLite databases, and mocked cameras. They do not require a connected camera:

```bash
cd /path/to/Camera-Exploration-Lab
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python3 -m unittest discover -v -s tests
```

Physical verification must run in a Bazzite KDE host session, since a container or sandbox may not have access to `/dev/video*`:

```bash
VENV="$HOME/.local/share/camera-exploration-lab/venv"
"$VENV/bin/celab" scan-v4l2
"$VENV/bin/celab" gui
```

In the Live Workbench, select a camera and mode, start preview, and check the driver-reported settings. Start a 10, 30, 60, or 120 second experiment and confirm that the preview continues while recording. Then inspect the result and any imported sessions in History. Do not treat read duration or frame interval as sensor latency.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for scope, test, and data-handling guidance. The proposed license is [MIT](LICENSE), prepared for project-owner review before publication.
