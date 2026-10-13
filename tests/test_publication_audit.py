import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.publication_audit import audit, scan_text


PUBLIC_AUTHOR = "Example Author"
PUBLIC_EMAIL = "123456+example" + "@users.noreply.github.com"


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def make_repository(root: Path, files: dict[str, str], commits: int = 1) -> str:
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for index in range(commits):
        marker = root / "milestones.txt"
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"milestone {index + 1}\n")
        git(root, "add", "--all")
        git(
            root,
            "-c", f"user.name={PUBLIC_AUTHOR}",
            "-c", f"user.email={PUBLIC_EMAIL}",
            "commit", "-m", f"synthetic milestone {index + 1}",
        )
    return git(root, "rev-parse", "HEAD")


class PublicationAuditTests(unittest.TestCase):
    def test_sensitive_values_are_detected_without_echoing_them(self):
        text = "\n".join(
            [
                "contact: " + "private.person" + "@" + "example.invalid",
                "source: /" + "home/" + "alice/private/project",
                "device: " + ":".join(("00", "11", "22", "33", "44", "55")),
                "pass" + "word = " + "VerySecret" + "Value123",
                "network: " + "192.168" + ".1.5",
                "host" + "name: " + "camera-" + "test-node",
            ]
        )
        findings = scan_text("README.md", text)
        categories = {finding.category for finding in findings}
        self.assertIn("email address", categories)
        self.assertIn("personal home path", categories)
        self.assertIn("MAC address", categories)
        self.assertIn("credential assignment", categories)
        self.assertIn("private network address", categories)
        self.assertIn("machine hostname", categories)
        self.assertTrue(all(finding.blocking for finding in findings))

    def test_synthetic_test_identifiers_are_reported_as_nonblocking(self):
        findings = scan_text(
            "tests/test_discovery.py",
            '"serial": "MOCK-001"\npath = "/dev/video9"\nport = "1-7:1.0"',
        )
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(not finding.blocking for finding in findings))

    def test_clean_independent_snapshot_passes_without_mutating_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_head = make_repository(base / "source", {"README.md": "local source\n"}, commits=3)
            snapshot_head = make_repository(base / "snapshot", {"README.md": "public source\n"})
            args = SimpleNamespace(
                source_branch="main",
                source_head=source_head,
                snapshot_branch="main",
                snapshot_head=snapshot_head,
                expected_author=PUBLIC_AUTHOR,
                expected_email=PUBLIC_EMAIL,
                sensitive_term=[],
            )
            source_status_before = git(base / "source", "status", "--short", "--branch")
            snapshot_status_before = git(base / "snapshot", "status", "--short", "--branch")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = audit(base / "source", base / "snapshot", args)
            self.assertEqual(status, 0, output.getvalue())
            self.assertIn("AUDIT PASSED", output.getvalue())
            self.assertIn("reachable source commits=3", output.getvalue())
            self.assertIn("reachable commits=1; root_commit=yes", output.getvalue())
            self.assertEqual(source_status_before, git(base / "source", "status", "--short", "--branch"))
            self.assertEqual(snapshot_status_before, git(base / "snapshot", "status", "--short", "--branch"))

    def test_remote_on_public_snapshot_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_head = make_repository(base / "source", {"README.md": "local source\n"})
            snapshot_head = make_repository(base / "snapshot", {"README.md": "public source\n"})
            git(base / "snapshot", "remote", "add", "origin", "https://example.invalid/project.git")
            args = SimpleNamespace(
                source_branch="main",
                source_head=source_head,
                snapshot_branch="main",
                snapshot_head=snapshot_head,
                expected_author=PUBLIC_AUTHOR,
                expected_email=PUBLIC_EMAIL,
                sensitive_term=[],
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = audit(base / "source", base / "snapshot", args)
            self.assertEqual(status, 1)
            self.assertIn("snapshot has a configured remote", output.getvalue())

    def test_modified_tracked_snapshot_file_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_head = make_repository(base / "source", {"README.md": "local source\n"})
            snapshot_head = make_repository(base / "snapshot", {"README.md": "public source\n"})
            (base / "snapshot" / "README.md").write_text("edited after review\n", encoding="utf-8")
            args = SimpleNamespace(
                source_branch="main",
                source_head=source_head,
                snapshot_branch="main",
                snapshot_head=snapshot_head,
                expected_author=PUBLIC_AUTHOR,
                expected_email=PUBLIC_EMAIL,
                sensitive_term=[],
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = audit(base / "source", base / "snapshot", args)
            self.assertEqual(status, 1)
            self.assertIn("tracked snapshot files differ from the expected commit", output.getvalue())


if __name__ == "__main__":
    unittest.main()
