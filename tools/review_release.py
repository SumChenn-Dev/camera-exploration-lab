#!/usr/bin/env python3
"""Read-only review of a published checkout and its proposed public changes."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from publication_audit import (
    Finding,
    _file_record,
    _is_data_path,
    _format_findings,
    _paths,
    _repo_root,
    _run_git,
    _scan_history,
    _scan_paths,
)

DEFAULT_REMOTE = "https://github.com/SumChenn-Dev/camera-exploration-lab.git"
DEFAULT_PUBLIC_EMAIL = "297786765+SumChenn-Dev" + "@users.noreply.github.com"


def _safe_remote(value: str) -> str:
    scheme, separator, remainder = value.partition("://")
    if not separator:
        return value
    authority, slash, suffix = remainder.partition("/")
    if "@" in authority:
        authority = "[credentials-redacted]@" + authority.rsplit("@", 1)[1]
    return scheme + separator + authority + (slash + suffix if slash else "")


def _git_result(repo: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def review(
    repo: Path,
    *,
    base: str = "origin/main",
    expected_remote: str = DEFAULT_REMOTE,
    expected_email: str = DEFAULT_PUBLIC_EMAIL,
    sensitive_terms: Iterable[str] = (),
    release_ready: bool = False,
) -> int:
    root = _repo_root(repo)
    blockers = 0
    branch = _run_git(root, "branch", "--show-current")
    head = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--short", "--branch", "--untracked-files=all")
    dirty = bool(_run_git(root, "status", "--porcelain", "--untracked-files=all"))

    print("Public checkout review (read-only)")
    print("  root=<local checkout>")
    print(f"  branch={branch or '[detached]'}")
    print(f"  HEAD={head}")
    print(f"  status={status or 'clean'}")
    if branch != "main":
        print("BLOCK: current branch is not main")
        blockers += 1

    remotes = _run_git(root, "remote")
    remote_names = remotes.splitlines()
    remote_code, remote_url, remote_error = _git_result(root, "remote", "get-url", "origin")
    push_code, push_url, push_error = _git_result(root, "remote", "get-url", "--push", "origin")
    upstream_code, upstream, upstream_error = _git_result(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    print(f"  remotes={', '.join(remote_names) if remote_names else 'none'}")
    if remote_code:
        print(f"BLOCK: origin fetch URL is unavailable ({remote_error})")
        blockers += 1
    else:
        print(f"  origin.fetch={_safe_remote(remote_url)}")
        if remote_url != expected_remote:
            print("BLOCK: origin fetch URL differs from the expected public repository")
            blockers += 1
    if push_code:
        print(f"BLOCK: origin push URL is unavailable ({push_error})")
        blockers += 1
    else:
        print(f"  origin.push={_safe_remote(push_url)}")
        if push_url != expected_remote:
            print("BLOCK: origin push URL differs from the expected public repository")
            blockers += 1
    if remote_names != ["origin"]:
        print("BLOCK: expected exactly one configured remote named origin")
        blockers += 1
    if upstream_code:
        print(f"BLOCK: main has no configured upstream ({upstream_error})")
        blockers += 1
    else:
        print(f"  upstream={upstream}")
        if upstream != "origin/main":
            print("BLOCK: main does not track origin/main")
            blockers += 1
    refs = _run_git(
        root,
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        "refs/heads",
        "refs/tags",
    )
    print("  local branches and tags:")
    for ref in refs.splitlines():
        print(f"    {ref}")
    if not refs:
        print("    none")

    base_commit: str | None = None
    ahead = 0
    behind = 0
    base_code, base_commit_output, base_error = _git_result(
        root, "rev-parse", "--verify", f"{base}^{{commit}}"
    )
    if base_code:
        print(f"BLOCK: selected base {base!r} is unavailable ({base_error})")
        blockers += 1
    else:
        base_commit = base_commit_output
        divergence = _run_git(root, "rev-list", "--left-right", "--count", f"{base}...HEAD")
        behind, ahead = (int(value) for value in divergence.split())
        print(f"  base={base} ({base_commit})")
        print(f"  commits behind={behind}; ahead={ahead}")
        if behind:
            print("BLOCK: local main is behind the selected publication base")
            blockers += 1

    if base_commit:
        outgoing = _run_git(
            root,
            "log",
            "--format=%h %an <%ae> %s",
            f"{base}..HEAD",
        )
        print("  commits proposed beyond base:")
        for entry in outgoing.splitlines():
            print(f"    {entry}")
        if not outgoing:
            print("    none")

    tracked = _paths(root, "ls-files", "--cached")
    untracked = _paths(root, "ls-files", "--others", "--exclude-standard")
    ignored = _paths(root, "ls-files", "--others", "--ignored", "--exclude-standard")
    tracked_ignored = _paths(root, "ls-files", "--cached", "--ignored", "--exclude-standard")
    visible = sorted(set(tracked) | set(untracked))
    present = [path for path in visible if (root / path).exists() or (root / path).is_symlink()]
    ignored_data_count = sum(_is_data_path(path) for path in ignored)

    print(
        f"  tracked={len(tracked)} nonignored-untracked={len(untracked)} "
        f"ignored-untracked={len(ignored)} ignored-data-candidates={ignored_data_count}"
    )
    if tracked_ignored:
        print("BLOCK: tracked paths match ignore rules:")
        for path in tracked_ignored:
            print(f"  {path}")
        blockers += len(tracked_ignored)

    print("  proposed file manifest (tracked and nonignored untracked files):")
    size_findings: list[Finding] = []
    for relative in present:
        try:
            record = _file_record(root, relative)
            print(f"    {record.sha256} {record.size:>9} bytes {relative}")
            if record.size > 1_000_000:
                size_findings.append(
                    Finding(
                        "large file (>1 MB)",
                        relative,
                        blocking=record.size > 5_000_000,
                    )
                )
        except (OSError, ValueError) as exc:
            print(f"BLOCK: {exc}")
            blockers += 1

    file_findings, _ = _scan_paths(root, present, sensitive_terms)
    blockers += _format_findings("  current file privacy and packaging findings", file_findings)
    blockers += _format_findings("  file size findings", size_findings)

    history_findings, identities, commit_count = _scan_history(
        root, sensitive_terms, expected_email
    )
    print(f"  reachable commits={commit_count}")
    print("  reachable author and committer identities:")
    for identity in identities:
        print(f"    {identity}")
    if not identities:
        print("    none")
    blockers += _format_findings("  reachable history privacy findings", history_findings)

    check_commands = [
        ("working-tree whitespace", ["diff", "--check"]),
        ("staged whitespace", ["diff", "--cached", "--check"]),
    ]
    if base_commit:
        check_commands.append(("outgoing whitespace", ["diff", "--check", f"{base}...HEAD"]))
    for label, args in check_commands:
        code, _stdout, error = _git_result(root, *args)
        if code:
            print(f"BLOCK: {label} check failed: {error}")
            blockers += 1

    if release_ready:
        if dirty:
            print("BLOCK: worktree has uncommitted or untracked nonignored changes")
            blockers += 1
        if ahead < 1:
            print("BLOCK: there are no local commits ahead of the selected base")
            blockers += 1

    if blockers:
        print(f"AUDIT BLOCKED: {blockers} release-blocking finding(s)")
        return 1
    print("AUDIT PASSED: no configured release blockers found")
    if dirty:
        print("Review the uncommitted diff and rerun with --release-ready after committing.")
    elif ahead == 0:
        print("No new commit is pending publication.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="published Git checkout")
    parser.add_argument("--base", default="origin/main", help="locally available publication base ref")
    parser.add_argument("--expected-remote", default=DEFAULT_REMOTE, help="expected origin URL")
    parser.add_argument("--expected-email", default=DEFAULT_PUBLIC_EMAIL, help="approved public commit email")
    parser.add_argument("--sensitive-term", action="append", default=[], help="extra local-only term to scan")
    parser.add_argument(
        "--release-ready",
        action="store_true",
        help="also require a clean worktree and at least one commit ahead of base",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return review(
            args.repo,
            base=args.base,
            expected_remote=args.expected_remote,
            expected_email=args.expected_email,
            sensitive_terms=args.sensitive_term,
            release_ready=args.release_ready,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"AUDIT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
