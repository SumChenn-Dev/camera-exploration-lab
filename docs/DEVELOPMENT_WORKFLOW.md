# Konsole development and release workflow

This document covers local testing, privacy review, change review, and deliberate publication from the public checkout. Audit and review commands are read-only: they do not fetch, commit, push, change remotes, or write application data.

## Locate the public checkout

Set this to the public Git checkout on the Bazzite host:

```bash
CHECKOUT="$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
VENV="$HOME/.local/share/camera-exploration-lab/venv"
cd "$CHECKOUT"
```

Keep the original private checkout and its Git history intact.

## Daily test and review loop

Run the full camera-free suite in the existing environment:

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen \
  "$VENV/bin/python" -m unittest discover -v -s tests
```

Review repository changes and remote refs without updating them:

```bash
git status --short --branch
git diff --check
git diff --stat
git diff
git diff --cached --check
git diff --cached
git diff origin/main...HEAD
git ls-remote origin refs/heads/main
```

The last command reads GitHub's current `main` ref. Compare it with `git rev-parse origin/main`; if they differ, stop and review remote changes before updating local refs. These commands do not fetch or alter local refs.

Run the read-only audit against the public checkout:

```bash
"$VENV/bin/python" tools/review_release.py \
  --repo "$CHECKOUT" \
  --base origin/main \
  --sensitive-term '<local-only username, host, or project label>'
```

Omit `--sensitive-term` when there are no additional local identifiers to check. Repeat it for each known sensitive term. The audit prints the intended branch, base and commit IDs, remotes, commit identities, tracked and ignored file counts, and a manifest with each proposed file's size and SHA-256. It scans reachable Git history and current tracked or nonignored files. It does not print ignored filenames or file contents.

Before a push, first commit only reviewed files. Then run the audit again with `--release-ready`:

```bash
"$VENV/bin/python" tools/review_release.py \
  --repo "$CHECKOUT" \
  --base origin/main \
  --release-ready
```

That mode requires a clean worktree, no commits behind the selected base, and at least one local commit ahead of it. The audit does not contact GitHub, so compare the live `main` hash using `git ls-remote` before relying on a local base ref. The privacy audit is a local release requirement; CI does not receive or upload experiment databases, logs, images, or other local data.

Automated scanning is a filter, not a privacy guarantee. Read every changed file, every outgoing commit, its author and committer, and the complete proposed diff. Check for local paths, serials, database files, backups, CSV/JSON experiment outputs, images, video, credentials, and research notes. Do not add local data to Git to make an audit pass.

## Local commit identity

Do not change global Git configuration. Use the public GitHub identity for each public commit, or set identity only in this repository after reviewing its local config. A one-command identity override is:

```bash
git -c user.name="$(git log -1 --format=%an)" \
  -c user.email="$(git log -1 --format=%ae)" \
  commit -m "Describe the reviewed change"
```

Stage explicit source and documentation paths, inspect the staged diff, and create a local commit only after review. Do not use `git add -A` when local experiment data or generated files may be present.

## Publishing

No audit command in this project pushes. Publishing requires explicit owner approval of the exact outgoing commit hash and destination. After that approval, recheck `git status --short --branch`, the release-ready audit, and the current GitHub `main` hash. The publication command is:

```bash
git push origin main
```

Never force-push or rewrite the original private development history. If the remote has advanced, stop and review or integrate its changes before publishing.

## CEL-013 Pass 1: physical dual-camera preview

Run this check in a normal Bazzite KDE user session with the Sony PlayStation Eye and AVerMedia CAM 313 connected. Automated tests do not access the physical devices and this workflow does not change camera controls.

```bash
CHECKOUT="$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
VENV="$HOME/.local/share/camera-exploration-lab/venv"
cd "$CHECKOUT"
"$VENV/bin/celab" scan-v4l2
```

Launch from the existing KDE Camera Exploration Lab entry, or keep using its current database argument in Konsole:

```bash
LAUNCHER="$HOME/.local/share/applications/camera-exploration-lab.desktop"
DATABASE="$(sed -n 's/.*--database "\([^"]*\)".*/\1/p' "$LAUNCHER")"
test -n "$DATABASE"
"$VENV/bin/celab" --database "$DATABASE" gui
```

Open **Multi-Camera**. Select the PlayStation Eye and CAM 313 in separate slots. The offered capture modes come from read-only format/size/frame-interval enumeration of each endpoint; choose a requested rate and check separate driver readback. Start A and B independently, stop one while confirming the other remains active, then use Start Both and Stop Both. Compare per-slot Capture FPS with Preview FPS; the GUI deliberately skips stale display frames. A partial-start message should retain the healthy stream. Reconnect testing requires a deliberate refresh and manual restart. A successful joint preview confirms concurrent host delivery only; it does not establish camera synchronization or sensor latency.

Single-camera recording remains in Live Workbench. The Multi-Camera tab has no Record Both control in Pass 1. Grouped experiment storage, paired host-time analysis, and interruption recovery belong to CEL-013 Pass 2 and are not implemented here.

## Verified Bazzite runtime and existing data

The Bazzite host transition is complete. The existing virtual environment's editable install resolves to the canonical public checkout, and the KDE launcher opens the app with the pre-existing local SQLite database. Both webcams were discovered and previewed; History still shows the earlier PS3 Eye 187 FPS experiment. The original checkout and database remain preserved.

For normal development, use the existing environment and checkout paths above. Check the editable source and dependencies without reinstalling packages:

```bash
"$VENV/bin/python" -m pip check
"$VENV/bin/python" - <<'PY'
from importlib import metadata
print(metadata.distribution("camera-exploration-lab").read_text("direct_url.json"))
PY
grep '^Exec=' "$HOME/.local/share/applications/camera-exploration-lab.desktop"
```

Keep the launcher's current `--database` argument. To launch the same History database manually, read the value after `--database` from that local `Exec=` line and set it as `DATABASE` in Konsole:

```bash
DATABASE="/path/to/the/existing/history.sqlite3"
"$VENV/bin/celab" --database "$DATABASE" gui
```

Do not copy the database or backups into the public checkout or Git. The application defaults to a per-user data directory only when no database argument is provided. Fresh-host installation is documented in the README.
