#!/usr/bin/env python3
"""Archive an explicitly selected Canon benchmark CSV into a CEL worktree.

The source is read-only. The public copy removes timestamp columns, replaces
any existing sample_index with a stable row index, and writes a path-free
manifest. This helper never stages or commits files.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


PATTERNS = {
    "canon-sweep-": "initial-fps-sweep",
    "canon-skip-compare-": "dropframe-ab",
    "canon-live-profile-": "live-pipeline-profile",
    "canon-device-poll-ab-": "device-polling-ab",
    "canon-smart-fps-": "smart-polling-fps-sweep",
    "canon-content-freshness-": "content-freshness",
}
SENSITIVE_HEADER_PARTS = {
    "email", "hostname", "host", "username", "user", "path", "filepath",
    "serial", "serialnumber", "uuid", "mac", "macaddress", "deviceid", "cameraid",
}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
HOME_PREFIXES = ("/" + "home/", "/var/" + "home/", "/" + "Users/")
HOME_PATH_RE = re.compile(r"(?:" + "|".join(re.escape(prefix) for prefix in HOME_PREFIXES) + r")[^\s,;]+|~[/\\][^\s,;]+")
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s,;]+)")


class ArchiveError(ValueError):
    """A safe, user-facing archive validation or write error."""


@dataclass(frozen=True)
class ArchiveResult:
    repo_root: Path
    destination: Path
    manifest: Path
    rows: int


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_label(name: str) -> str:
    lowered = Path(name).name.lower()
    for prefix, label in PATTERNS.items():
        if lowered.startswith(prefix):
            return label
    # Do not publish arbitrary source filenames as labels.
    return "benchmark"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64].strip("-")
    if not slug:
        raise ArchiveError("The public label must contain letters or numbers.")
    return slug


def resolve_repo_root(path: Path | None = None) -> Path:
    """Resolve a repository or worktree path through Git, including .git files."""
    candidate = path or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ArchiveError("Git is required to identify the destination worktree.") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ArchiveError("The destination must be inside a Git worktree.")
    return Path(result.stdout.strip()).resolve()


def _normalized_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _is_timestamp_header(header: str) -> bool:
    normalized = _normalized_header(header)
    return "timestamp" in normalized or normalized in {
        "datetime", "date_time", "wallclock", "wall_clock",
        "wallclock_time", "wall_clock_time",
    }


def _validate_headers(headers: list[str]) -> None:
    if not headers or any(not header or not header.strip() for header in headers):
        raise ArchiveError("The CSV must have non-empty column names.")
    normalized = [_normalized_header(header) for header in headers]
    if len(normalized) != len(set(normalized)):
        raise ArchiveError("The CSV has duplicate or ambiguous column names.")
    for header in normalized:
        if set(header.split("_")) & SENSITIVE_HEADER_PARTS:
            raise ArchiveError("The CSV has a column that may contain identifying information.")


def _validate_value(value: str) -> None:
    if EMAIL_RE.search(value) or HOME_PATH_RE.search(value) or WINDOWS_PATH_RE.search(value):
        raise ArchiveError("The CSV contains a value that may identify a person or local filesystem.")


def _write_manifest_entry(manifest_stream: TextIO, entry: dict[str, object]) -> None:
    original_size = os.fstat(manifest_stream.fileno()).st_size
    manifest_stream.seek(0, os.SEEK_END)
    try:
        manifest_stream.write(json.dumps(entry, sort_keys=True) + "\n")
        manifest_stream.flush()
        os.fsync(manifest_stream.fileno())
    except OSError as exc:
        try:
            manifest_stream.seek(original_size)
            manifest_stream.truncate()
            manifest_stream.flush()
        except OSError:
            pass
        raise ArchiveError("Unable to update the archive manifest.") from exc


def archive_benchmark(
    source: Path | str,
    repo: Path | str | None = None,
    label: str | None = None,
) -> ArchiveResult:
    """Create a sanitized, non-overwriting CSV copy in a Git worktree."""
    try:
        src = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArchiveError("The source CSV does not exist or cannot be resolved.") from exc
    if not src.is_file():
        raise ArchiveError("The source must be a regular CSV file.")

    repo_root = resolve_repo_root(Path(repo).expanduser() if repo is not None else None)
    public_label = _slug(label if label is not None else infer_label(src.name))
    results_dir = repo_root / "docs/research/canon-sx40-live-view/results"
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        results_dir = results_dir.resolve()
    except OSError as exc:
        raise ArchiveError("Unable to prepare the research results directory.") from exc
    if not results_dir.is_relative_to(repo_root):
        raise ArchiveError("The results directory must remain inside the selected worktree.")
    if src.is_relative_to(results_dir):
        raise ArchiveError("An existing public result cannot be used as a new source.")

    manifest = results_dir.parent / "archive-manifest.jsonl"
    if manifest.is_symlink() or not manifest.resolve().parent.is_relative_to(repo_root):
        raise ArchiveError("The archive manifest must remain inside the selected worktree.")

    try:
        initial_source_hash = sha256(src)
    except OSError as exc:
        raise ArchiveError("The source CSV could not be read.") from exc
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".canon-archive-", suffix=".csv", dir=results_dir)
    except OSError as exc:
        raise ArchiveError("Unable to prepare a temporary archive output.") from exc
    temp_path = Path(temp_name)
    destination: Path | None = None
    row_count = 0
    transformations: list[str] = []
    try:
        try:
            input_stream = src.open("r", newline="", encoding="utf-8-sig")
        except OSError:
            os.close(fd)
            raise
        with input_stream, os.fdopen(fd, "w", newline="", encoding="utf-8") as output_stream:
            reader = csv.DictReader(input_stream, strict=True)
            headers = reader.fieldnames
            if not headers:
                raise ArchiveError("The source CSV has no header.")
            _validate_headers(headers)
            timestamp_headers = [h for h in headers if _is_timestamp_header(h)]
            source_index = any(_normalized_header(h) == "sample_index" for h in headers)
            output_headers = ["sample_index"] + [
                h for h in headers
                if not _is_timestamp_header(h) and _normalized_header(h) != "sample_index"
            ]
            if timestamp_headers:
                transformations.append("removed timestamp columns")
            transformations.append("replaced source sample_index" if source_index else "added sample_index")

            writer = csv.DictWriter(
                output_stream,
                fieldnames=output_headers,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ArchiveError("The source CSV has rows with missing or extra fields.")
                row_count += 1
                clean: dict[str, str] = {}
                for header in headers:
                    if _is_timestamp_header(header) or _normalized_header(header) == "sample_index":
                        continue
                    value = row[header]
                    _validate_value(value)
                    clean[header] = value
                writer.writerow({"sample_index": row_count, **clean})
            output_stream.flush()
            os.fsync(output_stream.fileno())

        if row_count == 0:
            raise ArchiveError("The source CSV has no data rows.")
        source_hash = sha256(src)
        if source_hash != initial_source_hash:
            raise ArchiveError("The source CSV changed while it was being archived; retry with a stable file.")
        public_hash = sha256(temp_path)
        entry: dict[str, object] = {
            "public_file": "",
            "label": public_label,
            "rows": row_count,
            "source_sha256": source_hash,
            "public_sha256": public_hash,
            "privacy_transform": transformations + [
                "rejected common identifying columns and values",
                "no source path recorded",
            ],
        }

        with manifest.open("a+", encoding="utf-8") as manifest_stream:
            fcntl.flock(manifest_stream.fileno(), fcntl.LOCK_EX)
            try:
                sequence = 1
                while True:
                    candidate = results_dir / f"{sequence:02d}-{public_label}.csv"
                    try:
                        # Linking the completed temp file is atomic and never replaces.
                        os.link(temp_path, candidate)
                        destination = candidate
                        break
                    except FileExistsError:
                        sequence += 1
                entry["public_file"] = destination.name
                try:
                    _write_manifest_entry(manifest_stream, entry)
                except Exception:
                    destination.unlink(missing_ok=True)
                    destination = None
                    raise
            finally:
                fcntl.flock(manifest_stream.fileno(), fcntl.LOCK_UN)

        temp_path.unlink(missing_ok=True)
        assert destination is not None
        return ArchiveResult(repo_root, destination, manifest, row_count)
    except ArchiveError:
        temp_path.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    except (OSError, csv.Error, UnicodeError) as exc:
        temp_path.unlink(missing_ok=True)
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise ArchiveError("Unable to archive this CSV safely.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="explicit source CSV; the original file is never modified")
    parser.add_argument("--repo", help="destination repository/worktree (default: worktree containing this tool)")
    parser.add_argument("--label", help="public descriptive label")
    args = parser.parse_args()
    try:
        result = archive_benchmark(args.csv, args.repo, args.label)
    except ArchiveError as exc:
        parser.error(str(exc))
    print(f"Archived sanitized CSV: {result.destination.relative_to(result.repo_root)}")
    print("Original source left unchanged; no Git staging or commit was performed.")


if __name__ == "__main__":
    main()
