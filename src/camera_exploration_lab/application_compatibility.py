"""Read-only schema compatibility and explicit experiment selection models.

This module is an isolated readiness adapter. The normal CLI/MainWindow does
not call it, and its default feature profile keeps grouped UI/recording hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Mapping
from typing import TypeAlias

from .analysis import ExperimentAnalysis, load_analysis
from .db import Database
from .grouped_analysis import GroupedExperimentAnalysis, load_grouped_analysis
from .grouped_recording import GroupedExperimentRecord, GroupedExperimentRepository
from .history import HistoryRow, make_history_rows
from .schema_migrations import (
    GROUPED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SYNTHETIC_DATABASE_APPLICATION_ID,
    SchemaMigrationError,
    validate_schema_structure,
)
from .recording_ownership import (
    RecoveryClassification,
    RecoveryLockState,
)


class ApplicationOperation(StrEnum):
    LEGACY_HISTORY_READ = "legacy_history_read"
    LEGACY_ANALYSIS_READ = "legacy_analysis_read"
    SINGLE_CAMERA_RECORD = "single_camera_record"
    GROUPED_HISTORY_READ = "grouped_history_read"
    GROUPED_ANALYSIS_READ = "grouped_analysis_read"
    GROUPED_RECORD = "grouped_record"
    SCHEMA_MIGRATION = "schema_migration"


class CompatibilityErrorCode(StrEnum):
    DATABASE_NOT_FOUND = "database_not_found"
    DATABASE_OPEN_FAILED = "database_open_failed"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    MALFORMED_SCHEMA = "malformed_schema"
    UNSUPPORTED_APPLICATION_ID = "unsupported_application_id"
    OPERATION_DISABLED = "operation_disabled"
    MIGRATION_REQUIRED = "migration_required"
    UNRESOLVED_RECORDING = "unresolved_recording"
    RECORDING_STATE_UNAVAILABLE = "recording_state_unavailable"


class CompatibilityAccessError(RuntimeError):
    """A requested read/write operation is not available for this database."""

    def __init__(self, failure: "CompatibilityFailure") -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class CompatibilityFailure:
    code: CompatibilityErrorCode
    message: str
    schema_version: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeFeatures:
    """Features this runtime has actually enabled for a compatibility check."""

    v3_database_access: bool = False
    legacy_history_reader: bool = True
    legacy_analysis_reader: bool = True
    single_camera_v2_writer: bool = True
    grouped_history_reader: bool = False
    grouped_analysis_reader: bool = False
    grouped_recording_writer: bool = False
    recovery_inspector: bool = True


PRODUCTION_RUNTIME_FEATURES = RuntimeFeatures()
SYNTHETIC_READ_COMPATIBILITY_FEATURES = RuntimeFeatures(
    v3_database_access=True,
    grouped_history_reader=True,
    grouped_analysis_reader=True,
)


@dataclass(frozen=True, slots=True)
class ApplicationCapabilities:
    schema_version: int
    legacy_history_available: bool
    legacy_analysis_available: bool
    single_camera_recording_available: bool
    grouped_history_available: bool
    grouped_analysis_available: bool
    grouped_recording_available: bool
    recovery_inspection_available: bool
    migration_required: bool
    read_only: bool
    recording_blocked_by_unresolved_lease: bool | None
    operation_allowed: bool
    operation_block: CompatibilityFailure | None


@dataclass(frozen=True, slots=True)
class DatabaseCompatibility:
    supported: bool
    schema_version: int | None
    application_id: int | None
    synthetic_fixture: bool
    capabilities: ApplicationCapabilities | None
    failure: CompatibilityFailure | None


_READ_OPERATIONS = {
    ApplicationOperation.LEGACY_HISTORY_READ,
    ApplicationOperation.LEGACY_ANALYSIS_READ,
    ApplicationOperation.GROUPED_HISTORY_READ,
    ApplicationOperation.GROUPED_ANALYSIS_READ,
}
_RECORD_OPERATIONS = {
    ApplicationOperation.SINGLE_CAMERA_RECORD,
    ApplicationOperation.GROUPED_RECORD,
}
_V3_OPERATIONS = {
    ApplicationOperation.GROUPED_HISTORY_READ,
    ApplicationOperation.GROUPED_ANALYSIS_READ,
    ApplicationOperation.GROUPED_RECORD,
}


def inspect_database_compatibility(
    database: Database,
    operation: ApplicationOperation = ApplicationOperation.LEGACY_HISTORY_READ,
    *,
    features: RuntimeFeatures = PRODUCTION_RUNTIME_FEATURES,
) -> DatabaseCompatibility:
    """Validate v2/v3 structure read-only and report effective capabilities.

    The function never calls ``Database.initialize`` or a migration. A WRITE
    operation is only evaluated as a capability request; it does not acquire a
    recording lease or mutate the database.
    """
    version: int | None = None
    application_id: int | None = None
    try:
        with database.connect_readonly() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id not in {0, SYNTHETIC_DATABASE_APPLICATION_ID}:
                return _failed(
                    CompatibilityErrorCode.UNSUPPORTED_APPLICATION_ID,
                    "database application identity is not recognized",
                    version,
                    application_id,
                )
            validate_schema_structure(connection, expected_version=version)
    except FileNotFoundError:
        return _failed(
            CompatibilityErrorCode.DATABASE_NOT_FOUND,
            "database file does not exist",
            version,
            application_id,
        )
    except SchemaMigrationError as exc:
        code = (
            CompatibilityErrorCode.UNSUPPORTED_SCHEMA
            if "unsupported database schema version" in str(exc)
            else CompatibilityErrorCode.MALFORMED_SCHEMA
        )
        return _failed(code, str(exc), version, application_id)
    except Exception as exc:
        # Avoid returning SQLite exception strings, which may include paths.
        return _failed(
            CompatibilityErrorCode.DATABASE_OPEN_FAILED,
            f"database compatibility inspection failed ({type(exc).__name__})",
            version,
            application_id,
        )

    assert version is not None
    if (
        version == GROUPED_SCHEMA_VERSION
        and features.v3_database_access
        and application_id != SYNTHETIC_DATABASE_APPLICATION_ID
    ):
        return _failed(
            CompatibilityErrorCode.UNSUPPORTED_APPLICATION_ID,
            "schema-v3 readiness access requires a marked synthetic fixture",
            version,
            application_id,
        )
    if version == GROUPED_SCHEMA_VERSION and not features.v3_database_access:
        block = CompatibilityFailure(
            CompatibilityErrorCode.OPERATION_DISABLED,
            "schema-v3 application access is not enabled in this runtime profile",
            version,
        )
        capabilities = ApplicationCapabilities(
            schema_version=version,
            legacy_history_available=False,
            legacy_analysis_available=False,
            single_camera_recording_available=False,
            grouped_history_available=False,
            grouped_analysis_available=False,
            grouped_recording_available=False,
            recovery_inspection_available=False,
            migration_required=False,
            read_only=True,
            recording_blocked_by_unresolved_lease=None,
            operation_allowed=False,
            operation_block=block,
        )
        return DatabaseCompatibility(
            supported=True,
            schema_version=version,
            application_id=application_id,
            synthetic_fixture=application_id == SYNTHETIC_DATABASE_APPLICATION_ID,
            capabilities=capabilities,
            failure=None,
        )
    valid_legacy = version in {SCHEMA_VERSION, GROUPED_SCHEMA_VERSION}
    legacy_history = valid_legacy and features.legacy_history_reader
    legacy_analysis = valid_legacy and features.legacy_analysis_reader
    single_writer = version == SCHEMA_VERSION and features.single_camera_v2_writer
    grouped_history = version == GROUPED_SCHEMA_VERSION and features.grouped_history_reader
    grouped_analysis = version == GROUPED_SCHEMA_VERSION and features.grouped_analysis_reader
    recovery_available = valid_legacy and features.recovery_inspector
    migration_required = version == SCHEMA_VERSION and operation in _V3_OPERATIONS

    blocked: bool | None = None
    recovery_failure: CompatibilityFailure | None = None
    if operation in _RECORD_OPERATIONS:
        if not recovery_available:
            blocked = True
            recovery_failure = CompatibilityFailure(
                CompatibilityErrorCode.RECORDING_STATE_UNAVAILABLE,
                "recording state cannot be checked safely",
                version,
            )
        else:
            try:
                inspection = database.inspect_recording_recovery()
                blocked = inspection.lock_state is not RecoveryLockState.AVAILABLE or any(
                    finding.lease_present
                    or finding.classification in {
                        RecoveryClassification.ACTIVE_OWNER,
                        RecoveryClassification.INDETERMINATE_AFTER_CRASH,
                    }
                    for finding in inspection.findings
                )
            except Exception:
                blocked = True
                recovery_failure = CompatibilityFailure(
                    CompatibilityErrorCode.RECORDING_STATE_UNAVAILABLE,
                    "recording state could not be inspected safely",
                    version,
                )

    if operation is ApplicationOperation.LEGACY_HISTORY_READ:
        allowed = legacy_history
        block = None if allowed else _disabled(operation, version)
    elif operation is ApplicationOperation.LEGACY_ANALYSIS_READ:
        allowed = legacy_analysis
        block = None if allowed else _disabled(operation, version)
    elif operation is ApplicationOperation.SINGLE_CAMERA_RECORD:
        allowed = single_writer and not blocked
        block = recovery_failure or (None if allowed else _disabled(operation, version))
        if blocked and recovery_failure is None:
            block = CompatibilityFailure(
                CompatibilityErrorCode.UNRESOLVED_RECORDING,
                "recording is blocked by an active owner or unresolved experiment",
                version,
            )
    elif operation is ApplicationOperation.GROUPED_HISTORY_READ:
        allowed = grouped_history
        block = None if allowed else _operation_block(operation, version)
    elif operation is ApplicationOperation.GROUPED_ANALYSIS_READ:
        allowed = grouped_analysis
        block = None if allowed else _operation_block(operation, version)
    elif operation is ApplicationOperation.GROUPED_RECORD:
        allowed = version == GROUPED_SCHEMA_VERSION and features.grouped_recording_writer and not blocked
        block = recovery_failure or (None if allowed else _operation_block(operation, version))
        if blocked and recovery_failure is None:
            block = CompatibilityFailure(
                CompatibilityErrorCode.UNRESOLVED_RECORDING,
                "recording is blocked by an active owner or unresolved experiment",
                version,
            )
    else:
        allowed = False
        block = CompatibilityFailure(
            CompatibilityErrorCode.OPERATION_DISABLED,
            "production schema migration is disabled in this readiness pass",
            version,
        )

    read_only = operation in _READ_OPERATIONS or not allowed
    capabilities = ApplicationCapabilities(
        schema_version=version,
        legacy_history_available=legacy_history,
        legacy_analysis_available=legacy_analysis,
        single_camera_recording_available=single_writer and not bool(blocked),
        grouped_history_available=grouped_history,
        grouped_analysis_available=grouped_analysis,
        # Pass 2E.1 ships no production grouped writer, so it stays false in
        # the current and synthetic reader profiles.
        grouped_recording_available=(
            version == GROUPED_SCHEMA_VERSION
            and features.grouped_recording_writer
            and not bool(blocked)
        ),
        recovery_inspection_available=recovery_available,
        migration_required=migration_required,
        read_only=read_only,
        recording_blocked_by_unresolved_lease=blocked,
        operation_allowed=allowed,
        operation_block=block,
    )
    return DatabaseCompatibility(
        supported=True,
        schema_version=version,
        application_id=application_id,
        synthetic_fixture=application_id == SYNTHETIC_DATABASE_APPLICATION_ID,
        capabilities=capabilities,
        failure=None,
    )


def require_operation(
    compatibility: DatabaseCompatibility,
) -> ApplicationCapabilities:
    if compatibility.failure is not None:
        raise CompatibilityAccessError(compatibility.failure)
    capabilities = compatibility.capabilities
    if capabilities is None or not capabilities.operation_allowed:
        failure = (
            capabilities.operation_block if capabilities is not None
            else CompatibilityFailure(
                CompatibilityErrorCode.MALFORMED_SCHEMA,
                "database compatibility could not be established",
                compatibility.schema_version,
            )
        )
        raise CompatibilityAccessError(failure)
    return capabilities


class ExperimentKind(StrEnum):
    SINGLE_CAMERA = "single_camera"
    GROUPED = "grouped"


@dataclass(frozen=True, slots=True)
class AnalysisSelection:
    kind: ExperimentKind
    session_id: str
    benchmark_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompatibleHistoryItem:
    selection: AnalysisSelection
    label: str
    source: str
    status: str
    started_at: str | None
    camera_labels: tuple[str, ...]
    legacy_row: HistoryRow | None = None
    grouped_record: GroupedExperimentRecord | None = None

    @property
    def searchable_text(self) -> str:
        parts = [self.label, self.source, self.status, self.started_at or "", *self.camera_labels]
        if self.legacy_row is not None:
            parts.append(self.legacy_row.searchable_text)
        if self.grouped_record is not None:
            parts.append(self.grouped_record.notes or "")
            parts.extend(
                stream.benchmark_label for stream in self.grouped_record.streams
            )
        return " ".join(parts).casefold()


CompatibleAnalysis: TypeAlias = ExperimentAnalysis | GroupedExperimentAnalysis


def list_compatible_history(
    database: Database,
    *,
    features: RuntimeFeatures = PRODUCTION_RUNTIME_FEATURES,
) -> tuple[CompatibleHistoryItem, ...]:
    """Return legacy run rows plus one row per explicit grouped parent."""
    compatibility = inspect_database_compatibility(
        database,
        ApplicationOperation.LEGACY_HISTORY_READ,
        features=features,
    )
    require_operation(compatibility)
    assert compatibility.schema_version is not None
    repository = GroupedExperimentRepository(database)
    with database.connect_readonly() as connection:
        entries = database.history_entries_snapshot(connection)
        group_ids: tuple[str, ...] = ()
        group_records: list[GroupedExperimentRecord] = []
        if compatibility.schema_version == GROUPED_SCHEMA_VERSION:
            group_ids = tuple(
                str(row[0]) for row in connection.execute(
                    "SELECT session_id FROM group_recordings ORDER BY session_id"
                )
            )
            if group_ids and not features.grouped_history_reader:
                raise CompatibilityAccessError(CompatibilityFailure(
                    CompatibilityErrorCode.OPERATION_DISABLED,
                    "grouped History is disabled for this runtime profile",
                    compatibility.schema_version,
                ))
            for session_id in group_ids:
                group = repository.read_group_snapshot(connection, session_id)
                if group is not None:
                    group_records.append(group)

    grouped_ids = set(group_ids)
    items: list[CompatibleHistoryItem] = []
    for entry in entries:
        if str(entry.get("session_id") or "") in grouped_ids:
            continue
        row = make_history_rows((entry,))[0]
        items.append(CompatibleHistoryItem(
            selection=AnalysisSelection(
                ExperimentKind.SINGLE_CAMERA,
                row.session_id,
                row.benchmark_id,
            ),
            label=row.cells[0],
            source=row.source_value,
            status=row.cells[3],
            started_at=row.sort_values[4] or None,
            camera_labels=(row.cells[1],),
            legacy_row=row,
        ))
    for group in group_records:
        camera_labels = tuple(
            _camera_label(stream.identity_snapshot)
            for stream in group.streams
        )
        items.append(CompatibleHistoryItem(
            selection=AnalysisSelection(ExperimentKind.GROUPED, group.session_id),
            label=group.label,
            source=group.source,
            status=group.status,
            started_at=group.started_at or group.record_requested_at,
            camera_labels=camera_labels,
            grouped_record=group,
        ))
    items.sort(key=lambda item: (item.started_at or "", item.selection.session_id), reverse=True)
    return tuple(items)


def filter_compatible_history(
    items: tuple[CompatibleHistoryItem, ...],
    *,
    source: str | None = None,
    search: str = "",
) -> tuple[CompatibleHistoryItem, ...]:
    needle = search.strip().casefold()
    return tuple(
        item for item in items
        if (source is None or item.source == source)
        and (not needle or needle in item.searchable_text)
    )


def list_compatible_analysis_selections(
    database: Database,
    *,
    features: RuntimeFeatures = PRODUCTION_RUNTIME_FEATURES,
) -> tuple[AnalysisSelection, ...]:
    """Return exact targets without exposing grouped child runs as experiments."""
    return tuple(item.selection for item in list_compatible_history(database, features=features))


def load_compatible_analysis(
    database: Database,
    selection: AnalysisSelection,
    *,
    features: RuntimeFeatures = PRODUCTION_RUNTIME_FEATURES,
) -> CompatibleAnalysis:
    if selection.kind is ExperimentKind.GROUPED:
        compatibility = inspect_database_compatibility(
            database, ApplicationOperation.GROUPED_ANALYSIS_READ, features=features
        )
        require_operation(compatibility)
        if selection.benchmark_id is not None:
            raise ValueError("a grouped Analysis selection cannot target one child benchmark directly")
        return load_grouped_analysis(database, selection.session_id)
    compatibility = inspect_database_compatibility(
        database, ApplicationOperation.LEGACY_ANALYSIS_READ, features=features
    )
    require_operation(compatibility)
    return load_analysis(database, selection.benchmark_id, selection.session_id)


def _failed(
    code: CompatibilityErrorCode,
    message: str,
    schema_version: int | None,
    application_id: int | None,
) -> DatabaseCompatibility:
    return DatabaseCompatibility(
        supported=False,
        schema_version=schema_version,
        application_id=application_id,
        synthetic_fixture=application_id == SYNTHETIC_DATABASE_APPLICATION_ID,
        capabilities=None,
        failure=CompatibilityFailure(code, message, schema_version),
    )


def _disabled(
    operation: ApplicationOperation,
    version: int,
) -> CompatibilityFailure:
    return _operation_block(operation, version)


def _operation_block(
    operation: ApplicationOperation,
    version: int,
) -> CompatibilityFailure:
    if operation in _V3_OPERATIONS and version < GROUPED_SCHEMA_VERSION:
        return CompatibilityFailure(
            CompatibilityErrorCode.MIGRATION_REQUIRED,
            "this operation requires an explicitly migrated schema-v3 database",
            version,
        )
    return CompatibilityFailure(
        CompatibilityErrorCode.OPERATION_DISABLED,
        f"{operation.value} is not enabled in this runtime profile",
        version,
    )


def _camera_label(snapshot: object) -> str:
    if not isinstance(snapshot, Mapping):
        return "Camera identity unavailable"
    manufacturer = str(snapshot.get("manufacturer") or "").strip()
    model = str(snapshot.get("model") or "").strip()
    return " ".join(part for part in (manufacturer, model) if part) or "Camera identity unavailable"
