from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.analysis import ExperimentAnalysis
from camera_exploration_lab.application_compatibility import (
    SYNTHETIC_READ_COMPATIBILITY_FEATURES,
    AnalysisSelection,
    ApplicationOperation,
    CompatibilityAccessError,
    CompatibilityErrorCode,
    ExperimentKind,
    PRODUCTION_RUNTIME_FEATURES,
    RuntimeFeatures,
    filter_compatible_history,
    inspect_database_compatibility,
    list_compatible_history,
    load_compatible_analysis,
    require_operation,
)
from camera_exploration_lab.db import Database
from camera_exploration_lab.grouped_analysis import GroupedExperimentAnalysis
from camera_exploration_lab.grouped_recording import (
    GroupStatus,
    GroupedExperimentRepository,
    GroupedExperimentSpec,
    StreamStatus,
)
from camera_exploration_lab.schema_migrations import (
    GROUPED_SCHEMA_VERSION,
    SYNTHETIC_DATABASE_APPLICATION_ID,
    migrate_synthetic_database_to_v3,
)
from grouped_test_support import create_synthetic_database
from test_grouped_recording import stream_specs


class ApplicationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixtures = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_database(self, name: str, *, migrate: bool = False) -> Database:
        database, fixture = create_synthetic_database(self.root / name, seed_legacy_data=True)
        self.fixtures[name] = fixture
        if migrate:
            migrate_synthetic_database_to_v3(database)
        return database

    @staticmethod
    def content_digest(database: Database) -> str:
        digest = hashlib.sha256()
        with database.connect_readonly() as connection:
            for table in (
                "cameras", "sessions", "benchmark_runs", "setting_snapshots",
                "import_sources", "raw_records", "measurements", "benchmark_summaries",
            ):
                columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                if table == "measurements":
                    columns = [column for column in columns if column not in {
                        "elapsed_ns", "source_timestamp_ns", "source_sequence", "source_clock"
                    }]
                quoted = ",".join(f'"{column}"' for column in columns)
                for row in connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid'):
                    digest.update(repr(tuple(row)).encode("utf-8"))
                    digest.update(b"\n")
        return digest.hexdigest()

    def test_v2_detection_preserves_legacy_capabilities_and_requires_explicit_migration(self) -> None:
        database = self.make_database("legacy-v2.sqlite3")

        report = inspect_database_compatibility(database)
        grouped_request = inspect_database_compatibility(
            database, ApplicationOperation.GROUPED_ANALYSIS_READ,
            features=SYNTHETIC_READ_COMPATIBILITY_FEATURES,
        )

        self.assertTrue(report.supported)
        self.assertEqual(report.schema_version, 2)
        self.assertTrue(report.capabilities.legacy_history_available)
        self.assertTrue(report.capabilities.legacy_analysis_available)
        self.assertTrue(report.capabilities.single_camera_recording_available)
        self.assertFalse(report.capabilities.grouped_recording_available)
        self.assertTrue(report.capabilities.read_only)
        self.assertTrue(grouped_request.capabilities.migration_required)
        self.assertFalse(grouped_request.capabilities.operation_allowed)
        self.assertEqual(
            grouped_request.capabilities.operation_block.code,
            CompatibilityErrorCode.MIGRATION_REQUIRED,
        )
        with self.assertRaises(CompatibilityAccessError):
            require_operation(grouped_request)

    def test_v3_production_profile_keeps_group_features_disabled(self) -> None:
        database = self.make_database("v3-disabled.sqlite3", migrate=True)

        compatibility = inspect_database_compatibility(
            database, ApplicationOperation.LEGACY_HISTORY_READ,
            features=PRODUCTION_RUNTIME_FEATURES,
        )
        group_write = inspect_database_compatibility(
            database, ApplicationOperation.GROUPED_RECORD,
            features=SYNTHETIC_READ_COMPATIBILITY_FEATURES,
        )

        self.assertTrue(compatibility.supported)
        self.assertEqual(compatibility.schema_version, GROUPED_SCHEMA_VERSION)
        self.assertFalse(compatibility.capabilities.legacy_history_available)
        self.assertFalse(compatibility.capabilities.grouped_history_available)
        self.assertFalse(compatibility.capabilities.grouped_analysis_available)
        self.assertFalse(compatibility.capabilities.grouped_recording_available)
        self.assertFalse(compatibility.capabilities.operation_allowed)
        self.assertEqual(
            compatibility.capabilities.operation_block.code,
            CompatibilityErrorCode.OPERATION_DISABLED,
        )
        with self.assertRaises(CompatibilityAccessError):
            list_compatible_history(database, features=PRODUCTION_RUNTIME_FEATURES)
        self.assertFalse(group_write.capabilities.operation_allowed)
        self.assertFalse(group_write.capabilities.grouped_recording_available)

    def test_v3_readiness_profile_requires_synthetic_marker(self) -> None:
        database = self.make_database("v3-unmarked.sqlite3", migrate=True)
        with database.connect() as connection:
            connection.execute("PRAGMA application_id=0")
        report = inspect_database_compatibility(
            database,
            ApplicationOperation.GROUPED_HISTORY_READ,
            features=SYNTHETIC_READ_COMPATIBILITY_FEATURES,
        )
        self.assertFalse(report.supported)
        self.assertEqual(
            report.failure.code,
            CompatibilityErrorCode.UNSUPPORTED_APPLICATION_ID,
        )

    def test_unsupported_future_and_malformed_schemas_return_structured_errors(self) -> None:
        future = self.make_database("future.sqlite3")
        with future.connect() as connection:
            connection.execute("PRAGMA user_version=44")
        future_result = inspect_database_compatibility(future)
        self.assertFalse(future_result.supported)
        self.assertEqual(future_result.failure.code, CompatibilityErrorCode.UNSUPPORTED_SCHEMA)

        malformed = self.make_database("malformed.sqlite3")
        with malformed.connect() as connection:
            connection.execute("DROP TABLE benchmark_summaries")
        malformed_result = inspect_database_compatibility(malformed)
        self.assertFalse(malformed_result.supported)
        self.assertEqual(malformed_result.failure.code, CompatibilityErrorCode.MALFORMED_SCHEMA)

        malformed_v3 = self.make_database("malformed-v3.sqlite3", migrate=True)
        with malformed_v3.connect() as connection:
            connection.execute("DROP INDEX idx_benchmark_streams_camera_session")
        malformed_v3_result = inspect_database_compatibility(malformed_v3)
        self.assertFalse(malformed_v3_result.supported)
        self.assertEqual(
            malformed_v3_result.failure.code,
            CompatibilityErrorCode.MALFORMED_SCHEMA,
        )

    def test_non_cel_application_id_is_rejected_without_mutation(self) -> None:
        database = self.make_database("foreign-id.sqlite3")
        with database.connect() as connection:
            connection.execute("PRAGMA application_id=111")
        before = self.content_digest(database)

        result = inspect_database_compatibility(database)

        self.assertFalse(result.supported)
        self.assertEqual(result.failure.code, CompatibilityErrorCode.UNSUPPORTED_APPLICATION_ID)
        self.assertEqual(self.content_digest(database), before)

    def test_v3_startup_refuses_without_changing_legacy_rows_or_schema(self) -> None:
        database = self.make_database("no-auto-v3.sqlite3", migrate=True)
        before_digest = self.content_digest(database)
        before_schema = database.path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            database.initialize()

        self.assertEqual(self.content_digest(database), before_digest)
        self.assertEqual(database.path.read_bytes(), before_schema)

    def test_v2_startup_remains_v2_and_does_not_create_group_tables(self) -> None:
        database = self.make_database("ordinary-v2.sqlite3")
        database.initialize()
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_recordings'"
            ).fetchone())

    def test_v2_write_capability_fails_closed_while_another_owner_holds_database(self) -> None:
        database = self.make_database("owner-conflict.sqlite3")
        owner = database.acquire_recording_ownership(
            owner_kind="synthetic-compatibility-test", check_recovery=False
        )
        try:
            report = inspect_database_compatibility(
                database, ApplicationOperation.SINGLE_CAMERA_RECORD
            )
            self.assertFalse(report.capabilities.single_camera_recording_available)
            self.assertFalse(report.capabilities.operation_allowed)
            self.assertTrue(report.capabilities.recording_blocked_by_unresolved_lease)
            self.assertEqual(
                report.capabilities.operation_block.code,
                CompatibilityErrorCode.UNRESOLVED_RECORDING,
            )
        finally:
            owner.release(owner.owner_token)

    def test_schema_version_alone_never_enables_grouped_recording(self) -> None:
        database = self.make_database("no-group-writer.sqlite3", migrate=True)
        profile = RuntimeFeatures(
            v3_database_access=True,
            legacy_history_reader=True,
            legacy_analysis_reader=True,
            single_camera_v2_writer=True,
            grouped_history_reader=True,
            grouped_analysis_reader=True,
            grouped_recording_writer=False,
            recovery_inspector=True,
        )
        report = inspect_database_compatibility(
            database, ApplicationOperation.GROUPED_RECORD, features=profile
        )
        self.assertFalse(report.capabilities.grouped_recording_available)
        self.assertFalse(report.capabilities.operation_allowed)
        self.assertEqual(
            report.capabilities.operation_block.code,
            CompatibilityErrorCode.OPERATION_DISABLED,
        )

    def test_v3_history_combines_legacy_and_grouped_parents_once(self) -> None:
        database = self.make_database("mixed-v3.sqlite3", migrate=True)
        fixture = self.fixtures["mixed-v3.sqlite3"]
        repository = GroupedExperimentRepository(database)
        group = repository.create_group(GroupedExperimentSpec(
            label="Fictional grouped run",
            notes="synthetic only",
            record_requested_at="2026-01-01T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
            clock_epoch_id="fictional-clock",
            streams=stream_specs(),
        ))
        for stream in group.streams:
            repository.set_stream_status(
                group.session_id, stream.slot, StreamStatus.RECORDING,
                observed_at="2026-01-01T00:00:01+00:00", elapsed_ns=0,
            )
        repository.set_group_status(
            group.session_id, GroupStatus.RECORDING,
            observed_at="2026-01-01T00:00:01+00:00", elapsed_ns=0,
        )
        before_digest = self.content_digest(database)

        items = list_compatible_history(
            database, features=SYNTHETIC_READ_COMPATIBILITY_FEATURES
        )

        grouped = [item for item in items if item.selection.kind is ExperimentKind.GROUPED]
        legacy = [item for item in items if item.selection.kind is ExperimentKind.SINGLE_CAMERA]
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].selection.session_id, group.session_id)
        self.assertEqual(len(grouped[0].grouped_record.streams), 2)
        self.assertEqual(len(legacy), 2)
        self.assertEqual(
            {item.selection.benchmark_id for item in legacy},
            set(fixture.benchmark_ids),
        )
        self.assertEqual(len(filter_compatible_history(items, source="imported")), 2)
        self.assertEqual(len(filter_compatible_history(items, search="fictional grouped")), 1)
        self.assertEqual(self.content_digest(database), before_digest)

    def test_exact_analysis_selection_uses_legacy_or_grouped_reader(self) -> None:
        database = self.make_database("analysis-dispatch-v3.sqlite3", migrate=True)
        items = list_compatible_history(
            database, features=SYNTHETIC_READ_COMPATIBILITY_FEATURES
        )
        legacy = next(item for item in items if item.selection.kind is ExperimentKind.SINGLE_CAMERA)
        analysis = load_compatible_analysis(
            database, legacy.selection, features=SYNTHETIC_READ_COMPATIBILITY_FEATURES
        )
        self.assertIsInstance(analysis, ExperimentAnalysis)
        self.assertEqual(analysis.option.benchmark_id, legacy.selection.benchmark_id)

        repository = GroupedExperimentRepository(database)
        group = repository.create_group(GroupedExperimentSpec(
            label="Fictional analysis pair",
            record_requested_at="2026-01-01T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
            clock_epoch_id="fictional-analysis-clock",
            streams=stream_specs(),
        ))
        selection = AnalysisSelection(ExperimentKind.GROUPED, group.session_id)
        group_analysis = load_compatible_analysis(
            database, selection, features=SYNTHETIC_READ_COMPATIBILITY_FEATURES
        )
        self.assertIsInstance(group_analysis, GroupedExperimentAnalysis)
        self.assertEqual(group_analysis.group.session_id, group.session_id)

    def test_read_compatibility_operations_do_not_mutate_v3_content(self) -> None:
        database = self.make_database("read-only-v3.sqlite3", migrate=True)
        before_digest = self.content_digest(database)
        result = inspect_database_compatibility(
            database, ApplicationOperation.GROUPED_HISTORY_READ,
            features=SYNTHETIC_READ_COMPATIBILITY_FEATURES,
        )
        require_operation(result)
        list_compatible_history(database, features=SYNTHETIC_READ_COMPATIBILITY_FEATURES)
        self.assertEqual(self.content_digest(database), before_digest)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
