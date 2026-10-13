from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_writes_a_rotating_file_and_replaces_old_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "logs" / "celab.log"
            logger = configure_logging(log_path, console=False)
            logger.info("synthetic log event")
            first_handlers = list(logger.handlers)
            configure_logging(log_path, console=False)

            self.assertEqual(len(first_handlers), 1)
            self.assertEqual(len(logger.handlers), 1)
            self.assertIn("synthetic log event", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
