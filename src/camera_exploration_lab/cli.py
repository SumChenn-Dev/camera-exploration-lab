"""Small command line entry points for Phase 0 operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import Database
from .discovery import V4L2Discovery
from .importer import CSVHistoryImporter
from .logging_config import configure_logging


def _csv_sources(values: list[str]) -> list[Path]:
    sources: set[Path] = set()
    for value in values:
        path = Path(value).expanduser()
        if path.is_dir():
            sources.update(item.resolve() for item in path.rglob("*.csv") if item.is_file())
        elif path.is_file() and path.suffix.casefold() == ".csv":
            sources.add(path.resolve())
        else:
            raise ValueError(f"not a CSV file or directory: {path}")
    return sorted(sources)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="celab", description="Camera Exploration Lab tools")
    parser.add_argument("--database", type=Path, help="SQLite file (default: XDG data directory)")
    parser.add_argument("--log-file", type=Path, help="application log path (default: XDG state directory)")
    parser.add_argument("--no-file-log", action="store_true", help="write logs to the terminal only")
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser("import-csv", help="import CSV files or all CSVs below directories")
    import_command.add_argument("paths", nargs="+", help="CSV file or directory; directories are scanned recursively")

    commands.add_parser("database-info", help="show database version and record counts")
    commands.add_parser("scan-v4l2", help="list V4L2 nodes using read-only sysfs discovery")
    commands.add_parser("gui", help="open the preview-first desktop workbench")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(args.log_file, file_logging=not args.no_file_log)

    if args.command == "scan-v4l2":
        candidates = V4L2Discovery().discover()
        if not candidates:
            print("No V4L2 video nodes found.")
        for candidate in candidates:
            identity = candidate.identity
            print(
                f"{candidate.display_name}: {identity.stable_identifier} "
                f"(open node: {identity.device_path}; node present: {candidate.node_present}; "
                f"identity: {identity.identity_source})"
            )
        return 0

    if args.command == "gui":
        try:
            from .ui.live_workbench import run
        except ImportError as exc:
            print(
                f"FAIL: the desktop dependencies are unavailable: {exc}. "
                "Install the optional desktop extra in a per-user virtual environment.",
                file=sys.stderr,
            )
            return 2
        database = Database(args.database)
        database.initialize()
        return run(database)

    database = Database(args.database)
    database.initialize()

    if args.command == "import-csv":
        try:
            sources = _csv_sources(args.paths)
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        if not sources:
            print("FAIL: no CSV files found", file=sys.stderr)
            return 2
        importer = CSVHistoryImporter(database)
        failed = False
        for index, source in enumerate(sources, start=1):
            print(f"[{index}/{len(sources)}] IMPORT {source}", flush=True)
            try:
                result = importer.import_file(source)
            except Exception as exc:  # Continue so one malformed history file doesn't hide others.
                failed = True
                print(f"[{index}/{len(sources)}] FAIL {type(exc).__name__}: {exc}", flush=True)
                continue
            if result.already_imported:
                print(f"[{index}/{len(sources)}] PASS already imported; no duplicate rows created", flush=True)
            else:
                print(
                    f"[{index}/{len(sources)}] PASS {result.row_count} rows, "
                    f"{result.measurement_count} measurements",
                    flush=True,
                )
        return 1 if failed else 0

    if args.command == "database-info":
        with database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        print(f"Database: {database.path}")
        print(f"Schema version: {version}")
        for table in (
            "cameras", "sessions", "benchmark_runs", "benchmark_summaries",
            "measurements", "raw_records", "import_sources",
        ):
            print(f"{table}: {database.count(table)}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
