from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.db import Database, SCHEMA_VERSION
from camera_exploration_lab.analysis import load_analysis
from camera_exploration_lab.schema_migrations import (
    GROUPED_SCHEMA_VERSION,
    SYNTHETIC_DATABASE_APPLICATION_ID,
    SchemaMigrationError,
    migrate_synthetic_database_to_v3,
)
from grouped_test_support import create_synthetic_database, legacy_snapshot, schema_fingerprint


class GroupedSchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def database(self, name: str, *, version: int = 2, seed: bool = True):
        return create_synthetic_database(
            self.root / name, version=version, seed_legacy_data=seed
        )

    def assert_integrity(self, database: Database) -> None:
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_explicit_v2_migration_preserves_all_legacy_rows_and_old_readers(self) -> None:
        database, fixture = self.database("v2.sqlite3", version=2)
        before = legacy_snapshot(database)

        report = migrate_synthetic_database_to_v3(database)

        self.assertEqual(report.source_version, 2)
        self.assertEqual(report.target_version, GROUPED_SCHEMA_VERSION)
        self.assertFalse(report.already_current)
        self.assertEqual(legacy_snapshot(database), before)
        entries = database.history_entries()
        self.assertTrue(all(row["session_id"] == fixture.session_id for row in entries))
        self.assertEqual(
            {row["benchmark_id"] for row in entries}, set(fixture.benchmark_ids)
        )
        detail = database.history_detail(fixture.benchmark_ids[0], fixture.session_id)
        self.assertEqual(detail["metrics"][0]["sample_count"], 2)
        with database.connect_readonly() as connection:
            provenance = connection.execute(
                """SELECT elapsed_seconds, interval_ns, elapsed_ns,
                          source_timestamp_ns, source_sequence, source_clock
                   FROM measurements WHERE benchmark_id=? ORDER BY sample_index LIMIT 1""",
                (fixture.benchmark_ids[0],),
            ).fetchone()
        self.assertEqual(provenance["elapsed_seconds"], 1.0)
        self.assertEqual(provenance["interval_ns"], 16_666_667)
        self.assertIsNone(provenance["elapsed_ns"])
        self.assertIsNone(provenance["source_timestamp_ns"])
        self.assertIsNone(provenance["source_sequence"])
        self.assertIsNone(provenance["source_clock"])
        analysis = load_analysis(database, fixture.benchmark_ids[0], fixture.session_id)
        self.assertEqual(analysis.interval_stats.count, 2)
        self.assertEqual(analysis.interval_x_label, "Recorder elapsed time (s)")
        self.assert_integrity(database)
        self.assertEqual(database.count("measurements"), 3)

    def test_explicit_v1_migration_is_atomic_and_preserves_legacy_rows(self) -> None:
        database, fixture = self.database("v1.sqlite3", version=1)
        before = legacy_snapshot(database)

        report = migrate_synthetic_database_to_v3(database)

        self.assertEqual(report.source_version, 1)
        self.assertEqual(report.target_version, 3)
        self.assertIn("v1_to_v2:create_benchmark_summaries", report.applied_steps)
        after = legacy_snapshot(database)
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertNotIn("benchmark_summaries", before)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM benchmark_summaries"
            ).fetchone()[0], 0)
        self.assertEqual(database.history_detail(
            fixture.benchmark_ids[0], fixture.session_id
        )["metrics"][0]["sample_count"], 2)
        self.assert_integrity(database)

    def test_empty_synthetic_database_can_be_bootstrapped_and_migrated(self) -> None:
        database, _ = self.database("empty.sqlite3", version=0, seed=False)

        report = migrate_synthetic_database_to_v3(database)

        self.assertEqual(report.source_version, 0)
        self.assertEqual(report.target_version, 3)
        self.assertTrue(report.applied_steps[0].startswith("bootstrap_v2_statement_"))
        self.assert_integrity(database)
        self.assertEqual(database.count("sessions"), 0)

    def test_migration_is_idempotent_and_application_startup_stays_on_v2(self) -> None:
        database, _ = self.database("idempotent.sqlite3")
        self.assertEqual(SCHEMA_VERSION, 2)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

        first = migrate_synthetic_database_to_v3(database)
        second = migrate_synthetic_database_to_v3(database)

        self.assertFalse(first.already_current)
        self.assertTrue(second.already_current)
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            database.initialize()

        startup_database, _ = self.database("normal-startup.sqlite3")
        startup_database.initialize()
        with startup_database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_recordings'"
            ).fetchone())

    def test_migration_rejects_unmarked_databases_without_changes(self) -> None:
        database, _ = self.database("unmarked.sqlite3")
        with database.connect() as connection:
            connection.execute("PRAGMA application_id = 0")
        before = schema_fingerprint(database)

        with self.assertRaisesRegex(SchemaMigrationError, "not marked as a synthetic"):
            migrate_synthetic_database_to_v3(database)

        self.assertEqual(schema_fingerprint(database), before)

    def test_future_schema_version_is_rejected(self) -> None:
        database, _ = self.database("future.sqlite3")
        with database.connect() as connection:
            connection.execute("PRAGMA user_version = 4")
        before = schema_fingerprint(database)

        with self.assertRaisesRegex(SchemaMigrationError, "unsupported database schema version"):
            migrate_synthetic_database_to_v3(database)

        self.assertEqual(schema_fingerprint(database), before)

    def test_malformed_v1_and_v2_schemas_are_rejected_without_repair(self) -> None:
        v1, _ = self.database("malformed-v1.sqlite3", version=1)
        with v1.connect() as connection:
            connection.execute("DROP TABLE measurements")
        before_v1 = schema_fingerprint(v1)
        with self.assertRaisesRegex(SchemaMigrationError, "required table is missing"):
            migrate_synthetic_database_to_v3(v1)
        self.assertEqual(schema_fingerprint(v1), before_v1)

        v2, _ = self.database("malformed-v2.sqlite3", version=2)
        with v2.connect() as connection:
            connection.execute("DROP INDEX idx_measurements_benchmark_interval")
        before_v2 = schema_fingerprint(v2)
        with self.assertRaisesRegex(SchemaMigrationError, "expected indexes are missing"):
            migrate_synthetic_database_to_v3(v2)
        self.assertEqual(schema_fingerprint(v2), before_v2)

    def test_schema_v2_with_partial_v3_objects_is_rejected_without_repair(self) -> None:
        database, _ = self.database("partial-v3.sqlite3")
        with database.connect() as connection:
            connection.execute(
                """CREATE TABLE group_recordings (
                       session_id TEXT PRIMARY KEY,
                       status TEXT NOT NULL
                   )"""
            )
        before = schema_fingerprint(database)
        with self.assertRaisesRegex(SchemaMigrationError, "partial version 3 objects"):
            migrate_synthetic_database_to_v3(database)
        self.assertEqual(schema_fingerprint(database), before)

    def test_already_v3_schema_is_validated_before_idempotent_return(self) -> None:
        database, _ = self.database("broken-v3.sqlite3")
        migrate_synthetic_database_to_v3(database)
        with database.connect() as connection:
            connection.execute("DROP INDEX idx_benchmark_streams_camera_session")
        before = schema_fingerprint(database)
        with self.assertRaisesRegex(SchemaMigrationError, "expected indexes are missing"):
            migrate_synthetic_database_to_v3(database)
        self.assertEqual(schema_fingerprint(database), before)

    def test_v3_validation_rejects_nonunique_run_index_and_incompatible_trigger(self) -> None:
        broken_index, _ = self.database("nonunique-run-index.sqlite3")
        migrate_synthetic_database_to_v3(broken_index)
        with broken_index.connect() as connection:
            connection.execute("DROP INDEX idx_benchmark_runs_id_session_unique")
            connection.execute(
                "CREATE INDEX idx_benchmark_runs_id_session_unique ON benchmark_runs(id, session_id)"
            )
        before_index = schema_fingerprint(broken_index)
        with self.assertRaisesRegex(SchemaMigrationError, "index is not unique"):
            migrate_synthetic_database_to_v3(broken_index)
        self.assertEqual(schema_fingerprint(broken_index), before_index)

        broken_trigger, _ = self.database("broken-group-trigger.sqlite3")
        migrate_synthetic_database_to_v3(broken_trigger)
        with broken_trigger.connect() as connection:
            connection.execute("DROP TRIGGER group_recordings_require_two_streams")
            connection.execute(
                """CREATE TRIGGER group_recordings_require_two_streams
                   BEFORE UPDATE OF status ON group_recordings
                   WHEN 0 BEGIN SELECT RAISE(ABORT, 'synthetic incorrect trigger'); END"""
            )
        before_trigger = schema_fingerprint(broken_trigger)
        with self.assertRaisesRegex(SchemaMigrationError, "trigger is incompatible"):
            migrate_synthetic_database_to_v3(broken_trigger)
        self.assertEqual(schema_fingerprint(broken_trigger), before_trigger)

    def test_corrupt_synthetic_sqlite_file_fails_without_exposing_path(self) -> None:
        path = self.root / "corrupt.sqlite3"
        path.write_bytes(b"this is synthetic non-sqlite data")
        with self.assertRaises(SchemaMigrationError) as caught:
            migrate_synthetic_database_to_v3(Database(path))
        self.assertNotIn(str(path), str(caught.exception))

    def test_failure_after_each_v2_to_v3_ddl_step_rolls_back(self) -> None:
        probe, _ = self.database("probe.sqlite3", seed=False)
        executed: list[str] = []
        migrate_synthetic_database_to_v3(probe, _after_step=executed.append)
        ddl_steps = [
            name for name in executed
            if name.startswith("v2_to_v3:")
        ]
        self.assertGreaterEqual(len(ddl_steps), 10)

        for index, failing_step in enumerate(ddl_steps):
            with self.subTest(step=failing_step):
                database, _ = self.database(f"rollback-{index}.sqlite3")
                before_schema = schema_fingerprint(database)
                before_rows = legacy_snapshot(database)

                def fail_after(step: str) -> None:
                    if step == failing_step:
                        raise RuntimeError("synthetic injected failure")

                with self.assertRaisesRegex(RuntimeError, "synthetic injected failure"):
                    migrate_synthetic_database_to_v3(database, _after_step=fail_after)

                self.assertEqual(schema_fingerprint(database), before_schema)
                self.assertEqual(legacy_snapshot(database), before_rows)
                self.assert_integrity(database)

    def test_failure_in_v3_rolls_back_the_prior_v1_to_v2_step_too(self) -> None:
        database, _ = self.database("rollback-v1.sqlite3", version=1)
        before_schema = schema_fingerprint(database)
        before_rows = legacy_snapshot(database)

        def fail_after(step: str) -> None:
            if step == "v2_to_v3:create_group_recordings":
                raise RuntimeError("synthetic injected failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic injected failure"):
            migrate_synthetic_database_to_v3(database, _after_step=fail_after)

        self.assertEqual(schema_fingerprint(database), before_schema)
        self.assertEqual(legacy_snapshot(database), before_rows)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assert_integrity(database)

    def test_failure_after_every_ddl_step_rolls_back_v1_chain(self) -> None:
        probe, _ = self.database("probe-v1.sqlite3", version=1, seed=False)
        executed: list[str] = []
        migrate_synthetic_database_to_v3(probe, _after_step=executed.append)
        steps = [name for name in executed if name.startswith(("v1_to_v2:", "v2_to_v3:"))]
        self.assertGreaterEqual(len(steps), 15)
        for index, failing_step in enumerate(steps):
            with self.subTest(step=failing_step):
                database, _ = self.database(f"v1-chain-rollback-{index}.sqlite3", version=1)
                before_schema = schema_fingerprint(database)
                before_rows = legacy_snapshot(database)

                def fail_after(step: str) -> None:
                    if step == failing_step:
                        raise RuntimeError("synthetic v1-chain failure")

                with self.assertRaisesRegex(RuntimeError, "synthetic v1-chain failure"):
                    migrate_synthetic_database_to_v3(database, _after_step=fail_after)
                self.assertEqual(schema_fingerprint(database), before_schema)
                self.assertEqual(legacy_snapshot(database), before_rows)
                self.assert_integrity(database)

    def test_empty_bootstrap_failure_rolls_back_all_v2_and_v3_ddl(self) -> None:
        database, _ = self.database("rollback-empty.sqlite3", version=0, seed=False)
        before = schema_fingerprint(database)

        def fail_after(step: str) -> None:
            if step == "bootstrap_v2_statement_3":
                raise RuntimeError("synthetic bootstrap failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic bootstrap failure"):
            migrate_synthetic_database_to_v3(database, _after_step=fail_after)

        self.assertEqual(schema_fingerprint(database), before)
        with database.connect_readonly() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assert_integrity(database)

    def test_foreign_keys_are_enabled_by_database_connections(self) -> None:
        database, _ = self.database("fk.sqlite3")
        migrate_synthetic_database_to_v3(database)
        with database.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO experiment_events
                       (session_id, benchmark_id, event_type, observed_at)
                       VALUES ('missing-session','missing-run','test','synthetic')"""
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
