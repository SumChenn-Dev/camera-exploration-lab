from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from review_release import _safe_remote, review

TEST_EMAIL = "reviewer" + "@example.invalid"
EXPECTED_REMOTE = "https://github.com/SumChenn-Dev/camera-exploration-lab.git"


class ReleaseReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "work"
        self._git("init", "-b", "main", str(self.repo))
        self._git("-C", str(self.repo), "config", "user.name", "SumChenn-Dev")
        self._git("-C", str(self.repo), "config", "user.email", TEST_EMAIL)
        (self.repo / "README.md").write_text("Public project fixture.\n", encoding="utf-8")
        self._git("-C", str(self.repo), "add", "README.md")
        self._git("-C", str(self.repo), "commit", "-m", "Initial public fixture")
        self._git("-C", str(self.repo), "remote", "add", "origin", EXPECTED_REMOTE)
        head = self._git("-C", str(self.repo), "rev-parse", "HEAD")
        self._git("-C", str(self.repo), "update-ref", "refs/remotes/origin/main", head)
        self._git("-C", str(self.repo), "branch", "--set-upstream-to=origin/main", "main")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def _review(self, *, release_ready: bool = False) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = review(
                self.repo,
                expected_remote=EXPECTED_REMOTE,
                expected_email=TEST_EMAIL,
                release_ready=release_ready,
            )
        return result, output.getvalue()

    def test_read_only_review_of_clean_checkout(self) -> None:
        head_before = self._git("-C", str(self.repo), "rev-parse", "HEAD")
        status_before = self._git("-C", str(self.repo), "status", "--porcelain")
        result, output = self._review()
        self.assertEqual(result, 0, output)
        self.assertIn("AUDIT PASSED", output)
        self.assertIn("root=<local checkout>", output)
        self.assertNotIn(str(self.repo), output)
        self.assertEqual(self._git("-C", str(self.repo), "rev-parse", "HEAD"), head_before)
        self.assertEqual(self._git("-C", str(self.repo), "status", "--porcelain"), status_before)

    def test_uncommitted_private_path_and_csv_are_blocked(self) -> None:
        (self.repo / "notes.csv").write_text("/" + "home/private/measurements.csv\n", encoding="utf-8")
        result, output = self._review()
        self.assertEqual(result, 1)
        self.assertIn("personal home path", output)
        self.assertIn("experiment or media file", output)

    def test_sqlite_wal_sidecar_is_blocked_when_proposed(self) -> None:
        (self.repo / "history.sqlite3-wal").write_bytes(b"\x00synthetic wal")
        result, output = self._review()
        self.assertEqual(result, 1)
        self.assertIn("experiment or media file", output)
        self.assertIn("binary file", output)

    def test_ignored_sqlite_wal_sidecar_is_counted_without_listing_name(self) -> None:
        (self.repo / ".gitignore").write_text("*.sqlite3-wal\n", encoding="utf-8")
        self._git("-C", str(self.repo), "add", ".gitignore")
        self._git("-C", str(self.repo), "commit", "-m", "Ignore local WAL fixture")
        (self.repo / "private-history.sqlite3-wal").write_bytes(b"\x00synthetic wal")
        result, output = self._review()
        self.assertEqual(result, 0, output)
        self.assertIn("ignored-data-candidates=1", output)
        self.assertNotIn("private-history.sqlite3-wal", output)

    def test_remote_credentials_are_redacted(self) -> None:
        remote = "https://" + "user:private" + "@example.invalid/repo.git"
        self.assertEqual(
            _safe_remote(remote),
            "https://[credentials-redacted]@example.invalid/repo.git",
        )

    def test_release_ready_accepts_clean_commit_ahead_of_base(self) -> None:
        (self.repo / "CHANGELOG.md").write_text("Reviewed local change.\n", encoding="utf-8")
        self._git("-C", str(self.repo), "add", "CHANGELOG.md")
        self._git("-C", str(self.repo), "commit", "-m", "Document reviewed change")
        result, output = self._review(release_ready=True)
        self.assertEqual(result, 0, output)
        self.assertIn("commits behind=0; ahead=1", output)
        self.assertIn("AUDIT PASSED", output)


if __name__ == "__main__":
    unittest.main()
