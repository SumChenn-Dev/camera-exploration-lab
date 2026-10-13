"""Application logging setup with a bounded rotating log file."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def default_log_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "camera-exploration-lab" / "app.log"


def configure_logging(
    log_path: str | Path | None = None,
    *,
    level: int = logging.INFO,
    console: bool = True,
    file_logging: bool = True,
) -> logging.Logger:
    logger = logging.getLogger("camera_exploration_lab")
    logger.setLevel(level)
    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    if file_logging:
        target = Path(log_path) if log_path is not None else default_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
