#!/usr/bin/env python3
"""Read-only audit for a proposed Camera Exploration Lab public snapshot."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DATA_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".csv", ".json", ".jsonl", ".log",
    ".bak", ".backup", ".wal", ".shm", ".jpg", ".jpeg", ".png",
    ".gif", ".webp", ".tif", ".tiff", ".bmp", ".mp4", ".mov", ".avi",
    ".mkv", ".webm",
}
SENSITIVE_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials"}

EMAIL_RE = re.compile(
    r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+\b",
    re.IGNORECASE,
)
HOME_PATH_RE = re.compile(r"/(?:home|var/home)/([A-Za-z0-9._-]+)(?=/|\b)")
RUN_USER_PATH_RE = re.compile(r"/run/user/\d+(?:/|\b)")
TILDE_USER_PATH_RE = re.compile(r"~/(?![.${])[A-Za-z][A-Za-z0-9._-]*(?:/|\b)")
MAC_RE = re.compile(r"\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b", re.IGNORECASE)
PRIVATE_IPV4_RE = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
HOSTNAME_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:hostname|host[_ -]?name|machine[_ -]?name)\b"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9.-]{2,})[\"']?"
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
TOKEN_RE = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"client[_-]?secret)\b\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{8,})"
)
SERIAL_VALUE_RE = re.compile(
    r"(?i)\b(?:serial(?:[_ -]?(?:number|no|id))?|device[_ -]?serial)\b"
    r"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9][A-Za-z0-9._:-]{3,})[\"']"
)
USB_TOPOLOGY_RE = re.compile(r"\b\d+-\d+(?:\.\d+)*:\d+\.\d+\b")
NUMBERED_VIDEO_NODE_RE = re.compile(r"/dev/video\d+\b")


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    path: str
    line: int | None = None
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    size: int
    sha256: str
    binary: bool


def _safe_fixture(path: str, value: str | None = None) -> bool:
    if not (path.startswith("tests/") or ":tests/" in path):
        return False
    if value is None:
        return True
    normalized = value.upper()
    return normalized.startswith(("MOCK", "SYNTHETIC", "EXAMPLE", "TEST-")) or normalized == "SHARED-SERIAL"


def scan_text(path: str, text: str, sensitive_terms: Iterable[str] = ()) -> list[Finding]:
    """Return likely privacy/security findings without echoing matched content."""
    findings: list[Finding] = []
    terms = tuple(term.casefold() for term in sensitive_terms if term)
    for line_number, line in enumerate(text.splitlines(), start=1):
        checks: tuple[tuple[str, re.Pattern[str]], ...] = (
            ("email address", EMAIL_RE),
            ("personal home path", HOME_PATH_RE),
            ("runtime user path", RUN_USER_PATH_RE),
            ("user-relative path", TILDE_USER_PATH_RE),
            ("MAC address", MAC_RE),
            ("private network address", PRIVATE_IPV4_RE),
            ("machine hostname", HOSTNAME_ASSIGNMENT_RE),
            ("UUID identifier", UUID_RE),
            ("private key material", PRIVATE_KEY_RE),
            ("credential token", TOKEN_RE),
            ("bearer credential", BEARER_RE),
        )
        for category, pattern in checks:
            if pattern.search(line):
                findings.append(Finding(category, path, line_number))
        if any(term in line.casefold() for term in terms):
            findings.append(Finding("user-supplied sensitive term", path, line_number))

        if SECRET_ASSIGNMENT_RE.search(line):
            findings.append(Finding("credential assignment", path, line_number))

        serial_match = SERIAL_VALUE_RE.search(line)
        if serial_match:
            value = serial_match.group(1).rstrip(".,;)]}")
            synthetic = _safe_fixture(path, value)
            findings.append(
                Finding(
                    "synthetic serial fixture" if synthetic else "serial-like identifier",
                    path,
                    line_number,
                    blocking=not synthetic,
                )
            )

        if USB_TOPOLOGY_RE.search(line):
            synthetic = _safe_fixture(path)
            findings.append(
                Finding(
                    "synthetic USB topology fixture" if synthetic else "USB port topology",
                    path,
                    line_number,
                    blocking=not synthetic,
                )
            )

        if NUMBERED_VIDEO_NODE_RE.search(line):
            synthetic = _safe_fixture(path)
            findings.append(
                Finding(
                    "synthetic numbered video node" if synthetic else "numbered video node",
                    path,
                    line_number,
                    blocking=not synthetic,
                )
            )
    return findings


def _run_git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def _repo_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve()
    return root


def _tracked_paths(repo: Path) -> list[str]:
    output = _run_git(repo, "ls-files", "--cached", "-z")
    return sorted(item for item in output.split("\0") if item)


def _paths(repo: Path, *args: str) -> list[str]:
    output = _run_git(repo, *args, "-z")
    return sorted(item for item in output.split("\0") if item)


def _file_record(root: Path, relative: str) -> FileRecord:
    path = root / relative
    if path.is_symlink():
        raise ValueError(f"symbolic link is not allowed in snapshot: {relative}")
    data = path.read_bytes()
    try:
        data.decode("utf-8")
        binary = b"\0" in data
    except UnicodeDecodeError:
        binary = True
    return FileRecord(relative, len(data), hashlib.sha256(data).hexdigest(), binary)


def _scan_file(
    root: Path, relative: str, sensitive_terms: Iterable[str] = ()
) -> tuple[list[Finding], bool]:
    path = root / relative
    try:
        data = path.read_bytes()
    except OSError:
        return [Finding("unreadable file", relative)], False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding("binary file", relative)], True
    if b"\0" in data:
        return [Finding("binary file", relative)], True
    return scan_text(relative, text, sensitive_terms), False


def _scan_paths(
    root: Path, paths: Iterable[str], sensitive_terms: Iterable[str] = ()
) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    binary_paths: set[str] = set()
    for relative in sorted(set(paths)):
        path = Path(relative)
        if any(part in {".git", ".venv", "venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.name.lower() in SENSITIVE_NAMES:
            findings.append(Finding("sensitive filename", relative))
        if path.suffix.lower() in DATA_SUFFIXES:
            findings.append(Finding("experiment or media file", relative))
        file_findings, binary = _scan_file(root, relative, sensitive_terms)
        findings.extend(file_findings)
        if binary:
            binary_paths.add(relative)
    return findings, binary_paths


def _scan_history(
    repo: Path,
    sensitive_terms: Iterable[str] = (),
    approved_email: str | None = None,
) -> tuple[list[Finding], list[str], int]:
    findings: list[Finding] = []
    commits = _run_git(
        repo,
        "log",
        "--all",
        "--format=%H%x09%an%x09%ae%x09%cn%x09%ce%x09%s",
    ).splitlines()
    identities: set[str] = set()
    for entry in commits:
        fields = entry.split("\t", 5)
        if len(fields) == 6:
            commit, author, author_email, committer, committer_email, subject = fields
            identities.add(f"author={author} <{author_email}>; committer={committer} <{committer_email}>")
            findings.extend(scan_text(f"commit:{commit[:12]}:subject", subject, sensitive_terms))
            findings.extend(scan_text(f"commit:{commit[:12]}:author", author, sensitive_terms))
            findings.extend(scan_text(f"commit:{commit[:12]}:committer", committer, sensitive_terms))
            for label, value in (("author", author), ("author email", author_email), ("committer", committer), ("committer email", committer_email)):
                if label.endswith("email") and "@" in value:
                    # Identity addresses are reported separately; generic local.invalid is not a personal address.
                    expected = approved_email and value.casefold() == approved_email.casefold()
                    if not expected and not value.lower().endswith("@local.invalid"):
                        findings.append(Finding(f"{label} in Git metadata", f"commit:{commit[:12]}"))
                elif label in {"author", "committer"}:
                    findings.extend(scan_text(f"commit:{commit[:12]}:{label}", value))

    object_lines = _run_git(repo, "rev-list", "--objects", "--all").splitlines()
    blob_ids: dict[str, str] = {}
    for entry in object_lines:
        fields = entry.split(" ", 1)
        if len(fields) == 2:
            blob_ids.setdefault(fields[0], fields[1])
    if blob_ids:
        type_output = _run_git(
            repo,
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            input_text="\n".join(blob_ids),
        )
        for item in type_output.splitlines():
            object_id, object_type = item.split(" ", 1)
            if object_type != "blob":
                continue
            relative = blob_ids[object_id]
            content = _run_git_bytes(repo, "cat-file", "blob", object_id)
            try:
                findings.extend(scan_text(f"history:{relative}", content.decode("utf-8"), sensitive_terms))
            except UnicodeDecodeError:
                findings.append(Finding("binary Git history blob", f"history:{relative}"))
    return findings, sorted(identities), len(commits)


def _format_findings(title: str, findings: list[Finding], *, excluded: bool = False) -> int:
    if not findings:
        print(f"{title}: none")
        return 0
    print(f"{title}: {len(findings)} finding(s)")
    for item in findings:
        suffix = f":{item.line}" if item.line is not None else ""
        severity = "INFO" if excluded or not item.blocking else "BLOCK"
        print(f"  {severity} {item.category} — {item.path}{suffix}")
    return sum(1 for item in findings if item.blocking)


def audit(source: Path, snapshot: Path, args: argparse.Namespace) -> int:
    source_root = _repo_root(source)
    snapshot_root = _repo_root(snapshot)
    if source_root == snapshot_root:
        raise ValueError("source and snapshot must be separate Git repositories")
    if source_root in snapshot_root.parents or snapshot_root in source_root.parents:
        raise ValueError("source and snapshot repositories must not contain one another")

    blockers = 0
    print("Camera Exploration Lab local publication audit (read only)")
    print(f"Source repository: {source_root}")
    print(f"Snapshot repository: {snapshot_root}")

    source_branch = _run_git(source_root, "branch", "--show-current")
    source_head = _run_git(source_root, "rev-parse", "HEAD")
    source_status = _run_git(source_root, "status", "--short", "--branch")
    source_tracked_ignored = _paths(source_root, "ls-files", "--cached", "--ignored", "--exclude-standard")
    source_untracked = _paths(source_root, "ls-files", "--others", "--exclude-standard")
    source_ignored = _paths(source_root, "ls-files", "--others", "--ignored", "--exclude-standard")
    print("\nSource Git state")
    print(source_status or "  clean")
    print(f"  branch={source_branch or '[detached]'} head={source_head}")
    print(f"  tracked-but-ignored={len(source_tracked_ignored)} nonignored-untracked={len(source_untracked)} ignored-untracked={len(source_ignored)}")
    print("  source remotes:")
    source_remotes = _run_git(source_root, "remote", "-v")
    print("    " + (source_remotes.replace("\n", "\n    ") if source_remotes else "none"))
    source_history_findings, source_identities, source_commit_count = _scan_history(
        source_root, args.sensitive_term, args.expected_email
    )
    source_commits = _run_git(source_root, "log", "--all", "--format=%H %s")
    source_refs = _run_git(
        source_root,
        "for-each-ref",
        "--format=%(refname:short) %(objectname:short)",
        "refs/heads",
        "refs/tags",
    )
    print(f"  reachable source commits={source_commit_count}")
    print("  source commits:")
    for item in source_commits.splitlines():
        print(f"    {item}")
    print("  source branches and tags:")
    print("    " + (source_refs.replace("\n", "\n    ") if source_refs else "none"))
    print("  source Git identities:")
    for identity in source_identities:
        print(f"    {identity}")
    source_scan_paths = _run_git(
        source_root, "ls-files", "--cached", "--others", "--exclude-standard"
    ).splitlines()
    source_worktree_findings, _ = _scan_paths(
        source_root, source_scan_paths, args.sensitive_term
    )
    source_ignored_data = [
        path for path in source_ignored if Path(path).suffix.lower() in DATA_SUFFIXES
    ]
    print(f"  source worktree scan paths={len(source_scan_paths)}")
    print(f"  ignored source files matching experiment/media extensions={len(source_ignored_data)}")
    _format_findings(
        "  source worktree findings (not copied automatically)",
        source_worktree_findings,
        excluded=True,
    )
    _format_findings(
        "  source history findings (excluded from the independent snapshot)",
        source_history_findings,
        excluded=True,
    )
    if source_branch != args.source_branch:
        print(f"BLOCK source branch mismatch: expected {args.source_branch!r}")
        blockers += 1
    if not source_head.startswith(args.source_head):
        print(f"BLOCK source HEAD mismatch: expected prefix {args.source_head!r}")
        blockers += 1
    if source_tracked_ignored:
        print("BLOCK source has tracked files ignored by current rules:")
        for path in source_tracked_ignored:
            print(f"  {path}")
        blockers += len(source_tracked_ignored)

    snapshot_branch = _run_git(snapshot_root, "branch", "--show-current")
    snapshot_head = _run_git(snapshot_root, "rev-parse", "HEAD")
    snapshot_remotes = _run_git(snapshot_root, "remote", "-v")
    snapshot_commits = _run_git(snapshot_root, "rev-list", "--all", "--count")
    snapshot_parents = _run_git(snapshot_root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    snapshot_commit_list = _run_git(snapshot_root, "log", "--all", "--format=%H %s")
    snapshot_all_refs = _run_git(snapshot_root, "for-each-ref", "--format=%(refname)")
    snapshot_refs = _run_git(
        snapshot_root,
        "for-each-ref",
        "--format=%(refname:short) %(objectname:short)",
        "refs/heads",
        "refs/tags",
    )
    snapshot_tracked = _tracked_paths(snapshot_root)
    snapshot_untracked = _paths(snapshot_root, "ls-files", "--others", "--exclude-standard")
    snapshot_ignored = _paths(snapshot_root, "ls-files", "--others", "--ignored", "--exclude-standard")
    snapshot_tracked_ignored = _paths(snapshot_root, "ls-files", "--cached", "--ignored", "--exclude-standard")
    snapshot_modified = sorted(set(
        _run_git(snapshot_root, "diff", "--name-only").splitlines()
        + _run_git(snapshot_root, "diff", "--cached", "--name-only").splitlines()
    ))

    print("\nPublic snapshot Git state")
    print(_run_git(snapshot_root, "status", "--short", "--branch") or "  clean")
    print(f"  branch={snapshot_branch or '[detached]'} head={snapshot_head}")
    print(f"  reachable commits={snapshot_commits}; root_commit={'yes' if len(snapshot_parents) == 1 else 'no'}")
    print("  snapshot commits:")
    for item in snapshot_commit_list.splitlines():
        print(f"    {item}")
    print("  snapshot branches and tags:")
    print("    " + (snapshot_refs.replace("\n", "\n    ") if snapshot_refs else "none"))
    print(f"  tracked={len(snapshot_tracked)} nonignored-untracked={len(snapshot_untracked)} ignored-untracked={len(snapshot_ignored)} tracked-but-ignored={len(snapshot_tracked_ignored)}")
    print("  configured remotes:")
    print("    " + (snapshot_remotes.replace("\n", "\n    ") if snapshot_remotes else "none"))
    print("  Git identities:")
    snapshot_history_findings, snapshot_identities, snapshot_commit_count = _scan_history(
        snapshot_root, args.sensitive_term, args.expected_email
    )
    for identity in snapshot_identities:
        print(f"    {identity}")
    if not snapshot_identities:
        print("    none")

    if snapshot_branch != args.snapshot_branch:
        print(f"BLOCK snapshot branch mismatch: expected {args.snapshot_branch!r}")
        blockers += 1
    if not snapshot_head.startswith(args.snapshot_head):
        print(f"BLOCK snapshot HEAD mismatch: expected prefix {args.snapshot_head!r}")
        blockers += 1
    if int(snapshot_commits) != 1 or snapshot_commit_count != 1 or len(snapshot_parents) != 1:
        print("BLOCK snapshot must have exactly one root commit and no inherited history")
        blockers += 1
    if snapshot_all_refs.splitlines() != [f"refs/heads/{args.snapshot_branch}"]:
        print("BLOCK snapshot must have only the intended branch and no tags or extra refs")
        blockers += 1
    if snapshot_remotes:
        print("BLOCK snapshot has a configured remote; review it before any network action")
        blockers += 1
    if snapshot_untracked:
        print("BLOCK snapshot contains nonignored untracked files:")
        for path in snapshot_untracked:
            print(f"  {path}")
        blockers += len(snapshot_untracked)
    if snapshot_modified:
        print("BLOCK tracked snapshot files differ from the expected commit:")
        for path in snapshot_modified:
            print(f"  {path}")
        blockers += len(snapshot_modified)
    if snapshot_tracked_ignored:
        print("BLOCK snapshot has tracked files ignored by its rules:")
        for path in snapshot_tracked_ignored:
            print(f"  {path}")
        blockers += len(snapshot_tracked_ignored)

    identity_ok = False
    if len(snapshot_identities) == 1:
        identity = snapshot_identities[0]
        expected_author = f"author={args.expected_author} <{args.expected_email}>"
        expected_committer = f"committer={args.expected_author} <{args.expected_email}>"
        identity_ok = expected_author in identity and expected_committer in identity
    if not identity_ok:
        print("BLOCK snapshot author and committer must match the approved public identity")
        blockers += 1

    print("\nPublic file manifest (tracked files only)")
    records: list[FileRecord] = []
    snapshot_findings: list[Finding] = []
    binary_paths: set[str] = set()
    for relative in snapshot_tracked:
        try:
            record = _file_record(snapshot_root, relative)
            records.append(record)
            print(f"  {record.sha256}  {record.size:>8} bytes  {relative}")
            if record.binary:
                binary_paths.add(relative)
            if record.size > 1_000_000:
                snapshot_findings.append(Finding("large file (>1 MB)", relative, blocking=record.size > 5_000_000))
        except (OSError, ValueError) as exc:
            print(f"  BLOCK {exc}")
            blockers += 1

    worktree_content_paths = sorted(set(snapshot_tracked) | set(snapshot_untracked))
    content_findings, content_binaries = _scan_paths(
        snapshot_root, worktree_content_paths, args.sensitive_term
    )
    snapshot_findings.extend(content_findings)
    binary_paths.update(content_binaries)
    blockers += _format_findings("  public snapshot findings", snapshot_findings)
    _format_findings("  snapshot reachable history findings", snapshot_history_findings)

    if snapshot_ignored:
        ignored_data = [path for path in snapshot_ignored if Path(path).suffix.lower() in DATA_SUFFIXES]
        print(f"  ignored files present={len(snapshot_ignored)}; ignored experiment/media files={len(ignored_data)}")
    else:
        print("  ignored files present=0")

    if blockers:
        print(f"\nAUDIT FAILED: {blockers} release-blocking issue(s)")
        return 1
    print("\nAUDIT PASSED: no configured release-blocking issues found")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="original local development Git repository")
    parser.add_argument("--snapshot", required=True, type=Path, help="separate proposed public snapshot Git repository")
    parser.add_argument("--source-branch", required=True, help="expected source branch name")
    parser.add_argument("--source-head", required=True, help="expected source HEAD hash or prefix")
    parser.add_argument("--snapshot-branch", required=True, help="expected public snapshot branch name")
    parser.add_argument("--snapshot-head", required=True, help="expected public snapshot HEAD hash or prefix")
    parser.add_argument("--expected-author", required=True, help="approved public Git author and committer name")
    parser.add_argument("--expected-email", required=True, help="approved public Git author and committer email")
    parser.add_argument(
        "--sensitive-term",
        action="append",
        default=[],
        help="additional local identifier to search for; may be repeated; matched text is never printed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return audit(args.source, args.snapshot, args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"AUDIT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
