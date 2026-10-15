"""Explicit, synthetic-only schema migrations.

The normal application startup path remains on schema version 2. This module
exists for Pass 2A synthetic migration/repository tests; production database
activation requires a later, separately reviewed migration entry point.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from .db import Database, SCHEMA, SCHEMA_VERSION

GROUPED_SCHEMA_VERSION = 3
# Test fixtures must mark a database with this application_id before migration.
# A normal CEL history file is not marked and is rejected by this entry point.
SYNTHETIC_DATABASE_APPLICATION_ID = 0x43454C54  # "CELT"


class SchemaMigrationError(RuntimeError):
    """A schema could not be safely recognized or migrated."""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    source_version: int
    target_version: int
    applied_steps: tuple[str, ...]
    already_current: bool = False


_V1_TO_V2_STEPS: tuple[tuple[str, str], ...] = (
    (
        "create_benchmark_summaries",
        """CREATE TABLE benchmark_summaries (
            benchmark_id TEXT PRIMARY KEY REFERENCES benchmark_runs(id) ON DELETE CASCADE,
            frame_count INTEGER NOT NULL DEFAULT 0,
            read_failures INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL NOT NULL DEFAULT 0,
            capture_fps REAL,
            preview_fps REAL,
            processing_fps REAL,
            interval_count INTEGER NOT NULL DEFAULT 0,
            median_interval_ms REAL,
            p95_interval_ms REAL,
            p99_interval_ms REAL,
            summary_json TEXT NOT NULL DEFAULT '{}'
        )""",
    ),
    (
        "create_idx_measurements_benchmark_metric_sample",
        """CREATE INDEX idx_measurements_benchmark_metric_sample
           ON measurements(benchmark_id, metric, stream_scope, sample_index)""",
    ),
    (
        "create_idx_measurements_benchmark_interval",
        """CREATE INDEX idx_measurements_benchmark_interval
           ON measurements(benchmark_id, metric, interval_ns)
           WHERE interval_ns IS NOT NULL""",
    ),
)

_V3_DDL_STEPS: tuple[tuple[str, str], ...] = (
    (
        "create_benchmark_run_session_unique_index",
        """CREATE UNIQUE INDEX idx_benchmark_runs_id_session_unique
           ON benchmark_runs(id, session_id)""",
    ),
    (
        "create_group_recordings",
        """CREATE TABLE group_recordings (
            session_id TEXT PRIMARY KEY
                REFERENCES sessions(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN
                ('pending','recording','degraded','completed','partial','interrupted','failed')),
            duration_target_seconds REAL CHECK
                (duration_target_seconds IS NULL OR duration_target_seconds > 0),
            record_requested_at TEXT,
            recording_started_at TEXT,
            stop_requested_at TEXT,
            finalized_at TEXT,
            clock_epoch_id TEXT NOT NULL,
            timing_method_version TEXT NOT NULL,
            owner_pid INTEGER,
            owner_process_start_ticks TEXT,
            data_complete INTEGER NOT NULL DEFAULT 1 CHECK (data_complete IN (0,1)),
            integrity_flags_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )""",
    ),
    (
        "create_benchmark_streams",
        """CREATE TABLE benchmark_streams (
            session_id TEXT NOT NULL
                REFERENCES group_recordings(session_id) ON DELETE CASCADE,
            slot TEXT NOT NULL CHECK (slot IN ('A','B')),
            benchmark_id TEXT NOT NULL,
            camera_id TEXT REFERENCES cameras(id) ON DELETE SET NULL,
            endpoint_key TEXT NOT NULL CHECK (length(endpoint_key) > 0),
            identity_snapshot_json TEXT NOT NULL,
            timing_method TEXT NOT NULL,
            timestamp_source TEXT NOT NULL,
            clock_type TEXT NOT NULL,
            collection_point TEXT NOT NULL,
            timing_method_version TEXT NOT NULL,
            stream_status TEXT NOT NULL CHECK
                (stream_status IN ('pending','recording','completed','interrupted','failed')),
            first_host_arrival_elapsed_ns INTEGER CHECK
                (first_host_arrival_elapsed_ns IS NULL OR first_host_arrival_elapsed_ns >= 0),
            last_host_arrival_elapsed_ns INTEGER CHECK
                (last_host_arrival_elapsed_ns IS NULL OR last_host_arrival_elapsed_ns >= 0),
            first_persisted_elapsed_ns INTEGER CHECK
                (first_persisted_elapsed_ns IS NULL OR first_persisted_elapsed_ns >= 0),
            last_persisted_elapsed_ns INTEGER CHECK
                (last_persisted_elapsed_ns IS NULL OR last_persisted_elapsed_ns >= 0),
            last_persisted_sample_index INTEGER CHECK
                (last_persisted_sample_index IS NULL OR last_persisted_sample_index >= 0),
            capture_frame_count INTEGER NOT NULL DEFAULT 0 CHECK (capture_frame_count >= 0),
            read_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (read_failure_count >= 0),
            invalid_frame_count INTEGER NOT NULL DEFAULT 0 CHECK (invalid_frame_count >= 0),
            timing_queue_loss_count INTEGER NOT NULL DEFAULT 0 CHECK (timing_queue_loss_count >= 0),
            persistence_loss_count INTEGER NOT NULL DEFAULT 0 CHECK (persistence_loss_count >= 0),
            sequence_gap_count INTEGER NOT NULL DEFAULT 0 CHECK (sequence_gap_count >= 0),
            preview_skip_count INTEGER CHECK (preview_skip_count IS NULL OR preview_skip_count >= 0),
            data_complete INTEGER NOT NULL DEFAULT 1 CHECK (data_complete IN (0,1)),
            integrity_flags_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, slot),
            UNIQUE (benchmark_id),
            UNIQUE (session_id, benchmark_id),
            UNIQUE (session_id, endpoint_key),
            FOREIGN KEY (benchmark_id, session_id)
                REFERENCES benchmark_runs(id, session_id) ON DELETE CASCADE
        )""",
    ),
    (
        "create_experiment_events",
        """CREATE TABLE experiment_events (
            event_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL
                REFERENCES group_recordings(session_id) ON DELETE CASCADE,
            benchmark_id TEXT,
            event_type TEXT NOT NULL,
            elapsed_ns INTEGER CHECK (elapsed_ns IS NULL OR elapsed_ns >= 0),
            observed_at TEXT NOT NULL,
            reason_code TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (benchmark_id, session_id)
                REFERENCES benchmark_runs(id, session_id) ON DELETE CASCADE
        )""",
    ),
    (
        "create_recording_leases",
        """CREATE TABLE recording_leases (
            lease_key INTEGER PRIMARY KEY CHECK (lease_key = 1),
            session_id TEXT NOT NULL
                REFERENCES group_recordings(session_id) ON DELETE CASCADE,
            owner_token TEXT NOT NULL UNIQUE,
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_process_start_ticks TEXT,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT
        )""",
    ),
    (
        "create_group_status_index",
        """CREATE INDEX idx_group_recordings_status
           ON group_recordings(status, session_id)""",
    ),
    (
        "create_stream_camera_index",
        """CREATE INDEX idx_benchmark_streams_camera_session
           ON benchmark_streams(camera_id, session_id)""",
    ),
    (
        "create_event_elapsed_index",
        """CREATE INDEX idx_experiment_events_session_elapsed
           ON experiment_events(session_id, elapsed_ns, event_id)""",
    ),
    (
        "create_group_ready_trigger",
        """CREATE TRIGGER group_recordings_require_two_streams
           BEFORE UPDATE OF status ON group_recordings
           WHEN NEW.status IN ('recording','degraded','completed','partial','interrupted')
             AND (SELECT COUNT(*) FROM benchmark_streams
                  WHERE session_id=NEW.session_id) != 2
           BEGIN
               SELECT RAISE(ABORT, 'group requires exactly two stream participants');
           END""",
    ),
    (
        "add_measurements_elapsed_ns",
        """ALTER TABLE measurements ADD COLUMN elapsed_ns INTEGER
           CHECK (elapsed_ns IS NULL OR elapsed_ns >= 0)""",
    ),
    (
        "add_measurements_source_timestamp_ns",
        """ALTER TABLE measurements ADD COLUMN source_timestamp_ns INTEGER
           CHECK (source_timestamp_ns IS NULL OR source_timestamp_ns >= 0)""",
    ),
    (
        "add_measurements_source_sequence",
        """ALTER TABLE measurements ADD COLUMN source_sequence INTEGER
           CHECK (source_sequence IS NULL OR source_sequence >= 0)""",
    ),
    (
        "add_measurements_source_clock",
        """ALTER TABLE measurements ADD COLUMN source_clock TEXT""",
    ),
)

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "cameras": {
        "id", "stable_identifier", "backend", "device_path", "manufacturer", "model",
        "serial_number", "vendor_id", "product_id", "identity_source", "first_seen_at",
        "last_seen_at", "metadata_json",
    },
    "sessions": {
        "id", "camera_id", "label", "source", "started_at", "ended_at", "created_at",
        "notes", "metadata_json",
    },
    "benchmark_runs": {
        "id", "session_id", "label", "started_at", "ended_at", "status", "configuration_json",
    },
    "setting_snapshots": {
        "id", "session_id", "benchmark_id", "raw_record_id", "setting_key",
        "requested_value_json", "reported_value_json", "verified_value_json",
        "verification_status", "requested_at", "checked_at", "verification_method", "raw_json",
    },
    "import_sources": {
        "id", "source_path", "source_basename", "sha256", "source_size_bytes", "imported_at",
        "session_id", "row_count", "measurement_count", "format",
    },
    "raw_records": {
        "id", "source_id", "session_id", "benchmark_id", "row_number", "timestamp_raw",
        "observed_at", "elapsed_seconds", "raw_json",
    },
    "measurements": {
        "id", "benchmark_id", "raw_record_id", "metric", "value", "unit", "stream_scope",
        "observed_at", "elapsed_seconds", "sample_index", "interval_ns", "method",
    },
}
_SUMMARY_COLUMNS = {
    "benchmark_id", "frame_count", "read_failures", "duration_seconds", "capture_fps",
    "preview_fps", "processing_fps", "interval_count", "median_interval_ms",
    "p95_interval_ms", "p99_interval_ms", "summary_json",
}
_V3_COLUMNS: dict[str, set[str]] = {
    "group_recordings": {
        "session_id", "status", "duration_target_seconds", "record_requested_at",
        "recording_started_at", "stop_requested_at", "finalized_at", "clock_epoch_id",
        "timing_method_version", "owner_pid", "owner_process_start_ticks", "data_complete",
        "integrity_flags_json", "metadata_json",
    },
    "benchmark_streams": {
        "session_id", "slot", "benchmark_id", "camera_id", "endpoint_key",
        "identity_snapshot_json", "timing_method", "timestamp_source", "clock_type",
        "collection_point", "timing_method_version", "stream_status",
        "first_host_arrival_elapsed_ns", "last_host_arrival_elapsed_ns",
        "first_persisted_elapsed_ns", "last_persisted_elapsed_ns",
        "last_persisted_sample_index",
        "capture_frame_count", "read_failure_count", "invalid_frame_count",
        "timing_queue_loss_count", "persistence_loss_count", "sequence_gap_count",
        "preview_skip_count",
        "data_complete", "integrity_flags_json",
    },
    "experiment_events": {
        "event_id", "session_id", "benchmark_id", "event_type", "elapsed_ns",
        "observed_at", "reason_code", "detail_json",
    },
    "recording_leases": {
        "lease_key", "session_id", "owner_token", "owner_pid",
        "owner_process_start_ticks", "acquired_at", "heartbeat_at",
    },
}
_MEASUREMENT_V3_COLUMNS = {
    "elapsed_ns", "source_timestamp_ns", "source_sequence", "source_clock",
}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _indexes(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _index_columns(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in connection.execute(f'PRAGMA index_info("{index_name}")')
    )


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(
        str(row["name"])
        for row in sorted((row for row in rows if int(row["pk"])), key=lambda row: int(row["pk"]))
    )


def _foreign_key_groups(
    connection: sqlite3.Connection,
    table: str,
) -> list[tuple[str, str, tuple[tuple[str, str], ...]]]:
    rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["id"]), []).append(row)
    result = []
    for values in grouped.values():
        result.append((
            str(values[0]["table"]),
            str(values[0]["on_delete"]).upper(),
            tuple(sorted((str(row["from"]), str(row["to"])) for row in values)),
        ))
    return result


def _require_foreign_key(
    connection: sqlite3.Connection,
    table: str,
    target: str,
    on_delete: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    expected = (target, on_delete.upper(), tuple(sorted(columns)))
    if expected not in _foreign_key_groups(connection, table):
        raise SchemaMigrationError(f"unrecognized schema: foreign key is missing from {table}")


def _require_columns(
    connection: sqlite3.Connection,
    required: dict[str, set[str]],
    *,
    version_label: str,
) -> None:
    tables = _table_names(connection)
    missing_tables = sorted(set(required) - tables)
    if missing_tables:
        raise SchemaMigrationError(
            f"unrecognized {version_label} schema: required table is missing"
        )
    for table, columns in required.items():
        if not columns <= _columns(connection, table):
            raise SchemaMigrationError(
                f"unrecognized {version_label} schema: required columns are missing from {table}"
            )


def _validate_v1(connection: sqlite3.Connection) -> None:
    _require_columns(connection, _REQUIRED_COLUMNS, version_label="version 1")
    if "benchmark_summaries" in _table_names(connection):
        raise SchemaMigrationError("version 1 schema contains an unexpected summary table")
    indexes = _indexes(connection)
    if {
        "idx_measurements_benchmark_metric_sample",
        "idx_measurements_benchmark_interval",
    } & indexes:
        raise SchemaMigrationError("version 1 schema is partially upgraded")


def _validate_v2(connection: sqlite3.Connection) -> None:
    _require_columns(connection, _REQUIRED_COLUMNS, version_label="version 2")
    if "benchmark_summaries" not in _table_names(connection):
        raise SchemaMigrationError("unrecognized version 2 schema: benchmark_summaries is missing")
    if not _SUMMARY_COLUMNS <= _columns(connection, "benchmark_summaries"):
        raise SchemaMigrationError("unrecognized version 2 schema: benchmark_summaries is incomplete")
    indexes = _indexes(connection)
    if not {
        "idx_measurements_benchmark_metric_sample",
        "idx_measurements_benchmark_interval",
    } <= indexes:
        raise SchemaMigrationError("unrecognized version 2 schema: expected indexes are missing")
    if _index_columns(connection, "idx_measurements_benchmark_metric_sample") != (
        "benchmark_id", "metric", "stream_scope", "sample_index"
    ):
        raise SchemaMigrationError("unrecognized version 2 schema: measurement index is incompatible")
    if _index_columns(connection, "idx_measurements_benchmark_interval") != (
        "benchmark_id", "metric", "interval_ns"
    ):
        raise SchemaMigrationError("unrecognized version 2 schema: interval index is incompatible")


def _validate_v3(connection: sqlite3.Connection) -> None:
    _validate_v2(connection)
    _require_columns(connection, _V3_COLUMNS, version_label="version 3")
    if not _MEASUREMENT_V3_COLUMNS <= _columns(connection, "measurements"):
        raise SchemaMigrationError("unrecognized version 3 schema: timing provenance fields are incomplete")
    required_indexes = {
        "idx_benchmark_runs_id_session_unique",
        "idx_group_recordings_status",
        "idx_benchmark_streams_camera_session",
        "idx_experiment_events_session_elapsed",
    }
    if not required_indexes <= _indexes(connection):
        raise SchemaMigrationError("unrecognized version 3 schema: expected indexes are missing")
    expected_indexes = {
        "idx_benchmark_runs_id_session_unique": ("id", "session_id"),
        "idx_group_recordings_status": ("status", "session_id"),
        "idx_benchmark_streams_camera_session": ("camera_id", "session_id"),
        "idx_experiment_events_session_elapsed": ("session_id", "elapsed_ns", "event_id"),
    }
    for name, columns in expected_indexes.items():
        if _index_columns(connection, name) != columns:
            raise SchemaMigrationError(f"unrecognized version 3 schema: index {name} is incompatible")
    unique_benchmark_indexes = {
        str(row["name"])
        for row in connection.execute("PRAGMA index_list(benchmark_runs)")
        if bool(row["unique"])
    }
    if "idx_benchmark_runs_id_session_unique" not in unique_benchmark_indexes:
        raise SchemaMigrationError("unrecognized version 3 schema: benchmark run identity index is not unique")
    for column in ("elapsed_ns", "source_timestamp_ns", "source_sequence"):
        info = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(measurements)")
        }
        if str(info[column]["type"]).upper() != "INTEGER":
            raise SchemaMigrationError("unrecognized version 3 schema: timing integer affinity is incompatible")
    text_columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(measurements)")}
    if str(text_columns["source_clock"]["type"]).upper() != "TEXT":
        raise SchemaMigrationError("unrecognized version 3 schema: source_clock affinity is incompatible")
    _require_foreign_key(
        connection, "group_recordings", "sessions", "CASCADE", (("session_id", "id"),)
    )
    _require_foreign_key(
        connection, "benchmark_streams", "group_recordings", "CASCADE",
        (("session_id", "session_id"),),
    )
    _require_foreign_key(
        connection, "benchmark_streams", "benchmark_runs", "CASCADE",
        (("benchmark_id", "id"), ("session_id", "session_id")),
    )
    _require_foreign_key(
        connection, "benchmark_streams", "cameras", "SET NULL", (("camera_id", "id"),)
    )
    _require_foreign_key(
        connection, "experiment_events", "group_recordings", "CASCADE",
        (("session_id", "session_id"),),
    )
    _require_foreign_key(
        connection, "experiment_events", "benchmark_runs", "CASCADE",
        (("benchmark_id", "id"), ("session_id", "session_id")),
    )
    _require_foreign_key(
        connection, "recording_leases", "group_recordings", "CASCADE",
        (("session_id", "session_id"),),
    )

    unique_stream_keys = set()
    for index in connection.execute("PRAGMA index_list(benchmark_streams)"):
        if bool(index["unique"]):
            unique_stream_keys.add(_index_columns(connection, str(index["name"])))
    if not {
        ("session_id", "slot"),
        ("benchmark_id",),
        ("session_id", "benchmark_id"),
        ("session_id", "endpoint_key"),
    } <= unique_stream_keys:
        raise SchemaMigrationError("unrecognized version 3 schema: stream uniqueness constraints are missing")
    expected_primary_keys = {
        "group_recordings": ("session_id",),
        "benchmark_streams": ("session_id", "slot"),
        "experiment_events": ("event_id",),
        "recording_leases": ("lease_key",),
    }
    for table, columns in expected_primary_keys.items():
        if _primary_key_columns(connection, table) != columns:
            raise SchemaMigrationError(f"unrecognized version 3 schema: primary key is incompatible in {table}")
    unique_lease_columns = {
        _index_columns(connection, str(row["name"]))
        for row in connection.execute("PRAGMA index_list(recording_leases)")
        if bool(row["unique"])
    }
    if ("owner_token",) not in unique_lease_columns:
        raise SchemaMigrationError("unrecognized version 3 schema: lease owner token is not unique")
    triggers = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    if "group_recordings_require_two_streams" not in triggers:
        raise SchemaMigrationError("unrecognized version 3 schema: group invariant trigger is missing")
    trigger_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='group_recordings_require_two_streams'"
    ).fetchone()
    normalized_trigger = "".join(str(trigger_sql_row["sql"]).lower().split())
    required_trigger_parts = (
        "beforeupdateofstatusongroup_recordings",
        "new.statusin('recording','degraded','completed','partial','interrupted')",
        "(selectcount(*)frombenchmark_streamswheresession_id=new.session_id)!=2",
    )
    if any(part not in normalized_trigger for part in required_trigger_parts):
        raise SchemaMigrationError("unrecognized version 3 schema: group participant trigger is incompatible")


def _execute_script_atomically(connection: sqlite3.Connection, script: str) -> tuple[str, ...]:
    """Execute complete SQL statements individually without executescript's commit behavior."""
    statements: list[str] = []
    pending = ""
    for line in script.splitlines():
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
                statements.append(statement.splitlines()[0].strip())
            pending = ""
    if pending.strip():
        raise SchemaMigrationError("schema definition contains an incomplete SQL statement")
    return tuple(statements)


def _check_integrity(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SchemaMigrationError("foreign-key validation failed during schema migration")
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SchemaMigrationError("SQLite integrity validation failed during schema migration")


def _reject_partial_v3(connection: sqlite3.Connection) -> None:
    partial_tables = {
        "group_recordings", "benchmark_streams", "experiment_events", "recording_leases"
    } & _table_names(connection)
    partial_columns = _MEASUREMENT_V3_COLUMNS & _columns(connection, "measurements")
    partial_indexes = {
        "idx_benchmark_runs_id_session_unique", "idx_group_recordings_status",
        "idx_benchmark_streams_camera_session", "idx_experiment_events_session_elapsed",
    } & _indexes(connection)
    partial_trigger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='group_recordings_require_two_streams'"
    ).fetchone()
    if partial_tables or partial_columns or partial_indexes or partial_trigger:
        raise SchemaMigrationError("version 2 schema contains partial version 3 objects")


def migrate_synthetic_database_to_v3(
    database: Database,
    *,
    _after_step: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Explicitly migrate a marked synthetic test database to grouped schema v3.

    This function is deliberately not called by Database.initialize(), GUI
    startup, the CLI, or any production application path. Synthetic fixtures
    must carry SYNTHETIC_DATABASE_APPLICATION_ID. The private callback exists
    only to inject transactional failures in migration tests.
    """
    if database._memory_uri is not None or str(database.path) == ":memory:":
        raise SchemaMigrationError("synthetic migration requires a marked on-disk test database")
    if not database.path.is_file():
        raise SchemaMigrationError("synthetic migration requires an existing test database file")

    applied: list[str] = []

    def checkpoint(step: str) -> None:
        applied.append(step)
        if _after_step is not None:
            _after_step(step)

    try:
        with database.connect() as connection:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id != SYNTHETIC_DATABASE_APPLICATION_ID:
                raise SchemaMigrationError("database is not marked as a synthetic migration fixture")
            source_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if source_version < 0 or source_version > GROUPED_SCHEMA_VERSION:
                raise SchemaMigrationError(
                    f"unsupported database schema version {source_version}"
                )
            if source_version == GROUPED_SCHEMA_VERSION:
                _validate_v3(connection)
                _check_integrity(connection)
                return MigrationReport(
                    source_version, GROUPED_SCHEMA_VERSION, (), already_current=True
                )

            connection.execute("BEGIN IMMEDIATE")
            if source_version == 0:
                if _table_names(connection):
                    raise SchemaMigrationError("unversioned database is not empty")
                statements = _execute_script_atomically(connection, SCHEMA)
                for index, _statement in enumerate(statements, start=1):
                    checkpoint(f"bootstrap_v2_statement_{index}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                checkpoint("set_user_version_2")
                _validate_v2(connection)
                source_for_report = 0
            elif source_version == 1:
                _validate_v1(connection)
                for name, statement in _V1_TO_V2_STEPS:
                    connection.execute(statement)
                    checkpoint(f"v1_to_v2:{name}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                checkpoint("v1_to_v2:set_user_version_2")
                _validate_v2(connection)
                source_for_report = 1
            elif source_version == SCHEMA_VERSION:
                _validate_v2(connection)
                _reject_partial_v3(connection)
                source_for_report = SCHEMA_VERSION
            else:
                raise SchemaMigrationError(
                    f"unsupported source database schema version {source_version}"
                )

            for name, statement in _V3_DDL_STEPS:
                connection.execute(statement)
                checkpoint(f"v2_to_v3:{name}")
            connection.execute(f"PRAGMA user_version = {GROUPED_SCHEMA_VERSION}")
            checkpoint("v2_to_v3:set_user_version_3")
            _validate_v3(connection)
            _check_integrity(connection)
            return MigrationReport(
                source_for_report, GROUPED_SCHEMA_VERSION, tuple(applied), already_current=False
            )
    except SchemaMigrationError:
        raise
    except sqlite3.Error as exc:
        # Do not expose the SQLite filename or private filesystem details.
        raise SchemaMigrationError("schema migration failed; the transaction was rolled back") from exc
