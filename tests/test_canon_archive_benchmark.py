from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/canon/archive_benchmark.py"
SPEC = importlib.util.spec_from_file_location("canon_archive_benchmark", MODULE_PATH)
ARCHIVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARCHIVER
SPEC.loader.exec_module(ARCHIVER)


class CanonArchiveBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cel-canon-archive-")
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)
        self.repo = self.temp_root / "repo"
        self.repo.mkdir()
        self._init_repo(self.repo)

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _init_repo(self, repo: Path) -> None:
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(repo)],
            check=True,
            capture_output=True,
        )
        self._git(repo, "config", "user.name", "Synthetic Test")
        self._git(repo, "config", "user.email", "synthetic" + "@" + "example.invalid")
        (repo / ".gitignore").write_text("*.csv\n*.jsonl\n", encoding="utf-8")
        (repo / "README.md").write_text("synthetic\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "README.md")
        self._git(repo, "commit", "--quiet", "-m", "synthetic fixture")

    def _source(
        self,
        content: str = "timestamp,value,sample_index\n2026-01-01T00:00:00Z,1,80\n2026-01-01T00:00:01Z,2,81\n",
    ) -> Path:
        path = self.temp_root / "input.csv"
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def test_sanitizes_timestamp_and_reindexes_without_changing_source(self) -> None:
        source = self._source()
        original = source.read_bytes()
        result = ARCHIVER.archive_benchmark(source, self.repo, "test run")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.destination.name, "01-test-run.csv")
        with result.destination.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(list(rows[0]), ["sample_index", "value"])
        self.assertEqual([row["sample_index"] for row in rows], ["1", "2"])
        self.assertEqual([row["value"] for row in rows], ["1", "2"])
        manifest = [json.loads(line) for line in result.manifest.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["rows"], 2)
        self.assertEqual(manifest[0]["public_sha256"], ARCHIVER.sha256(result.destination))
        self.assertNotIn(str(source), result.manifest.read_text(encoding="utf-8"))

    def test_uses_available_number_without_overwriting_existing_files(self) -> None:
        source = self._source()
        results = self.repo / "docs/research/canon-sx40-live-view/results"
        results.mkdir(parents=True)
        first = results / "01-bench.csv"
        third = results / "03-bench.csv"
        first.write_text("prior one\n", encoding="utf-8")
        third.write_text("prior three\n", encoding="utf-8")
        result = ARCHIVER.archive_benchmark(source, self.repo, "bench")
        self.assertEqual(result.destination.name, "02-bench.csv")
        self.assertEqual(first.read_text(encoding="utf-8"), "prior one\n")
        self.assertEqual(third.read_text(encoding="utf-8"), "prior three\n")

    def test_resolves_git_worktree_paths(self) -> None:
        worktree = self.temp_root / "worktree"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--quiet", "-b", "archive-test", str(worktree)],
            check=True,
            capture_output=True,
        )
        self.assertTrue((worktree / ".git").is_file())
        result = ARCHIVER.archive_benchmark(self._source(), worktree, "worktree")
        self.assertEqual(result.repo_root, worktree.resolve())
        self.assertTrue(result.destination.is_relative_to(worktree.resolve()))
        self.assertFalse((self.repo / "docs/research/canon-sx40-live-view/results").exists())

    def test_default_destination_is_the_worktree_containing_the_tool(self) -> None:
        self.assertEqual(ARCHIVER.resolve_repo_root(), ROOT.resolve())

    def test_missing_source_fails_without_disclosing_path_or_creating_output(self) -> None:
        missing = self.temp_root / "private-name-not-for-output.csv"
        with self.assertRaises(ARCHIVER.ArchiveError) as caught:
            ARCHIVER.archive_benchmark(missing, self.repo)
        self.assertNotIn(str(self.temp_root), str(caught.exception))
        self.assertFalse((self.repo / "docs/research/canon-sx40-live-view").exists())

    def test_rejects_common_sensitive_headers_and_values(self) -> None:
        synthetic_email = "user" + "@" + "example.invalid"
        synthetic_home_path = str(Path("/", "var", "home", "synthetic-user", "private.csv"))
        samples = (
            "timestamp,serial_number\nnow,123456\n",
            f"timestamp,comment\nnow,{synthetic_email}\n",
            f"timestamp,comment\nnow,{synthetic_home_path}\n",
        )
        for content in samples:
            with self.subTest(content_kind="sensitive synthetic fixture"):
                source = self._source(content)
                original = source.read_bytes()
                with self.assertRaises(ARCHIVER.ArchiveError):
                    ARCHIVER.archive_benchmark(source, self.repo, "safe")
                self.assertEqual(source.read_bytes(), original)
                results = self.repo / "docs/research/canon-sx40-live-view/results"
                self.assertFalse(results.exists() and any(results.glob("*.csv")))

    def test_sanitizes_label_before_constructing_destination(self) -> None:
        result = ARCHIVER.archive_benchmark(self._source(), self.repo, "../outside")
        self.assertEqual(
            result.destination.parent,
            (self.repo / "docs/research/canon-sx40-live-view/results").resolve(),
        )
        self.assertEqual(result.destination.name, "01-outside.csv")

    def test_rejects_malformed_rows_without_publishing_partial_output(self) -> None:
        source = self._source("name,value\none,1,extra\n")
        with self.assertRaises(ARCHIVER.ArchiveError):
            ARCHIVER.archive_benchmark(source, self.repo, "bad")
        results = self.repo / "docs/research/canon-sx40-live-view/results"
        self.assertFalse(results.exists() and any(results.glob("*.csv")))

    def test_ignored_source_and_archive_are_not_staged_automatically(self) -> None:
        source = self.repo / "private-input.csv"
        source.write_text("timestamp,value\nnow,1\n", encoding="utf-8")
        ignored = subprocess.run(
            ["git", "-C", str(self.repo), "check-ignore", "-q", str(source)],
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        result = ARCHIVER.archive_benchmark(source, self.repo, "ignored")
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(result.destination.exists())
        self.assertTrue(result.manifest.exists())


if __name__ == "__main__":
    unittest.main()
