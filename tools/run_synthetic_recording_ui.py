#!/usr/bin/env python3
"""Launch the explicitly acknowledged fake-camera recording UI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from camera_exploration_lab.ui.synthetic_recording_harness import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
