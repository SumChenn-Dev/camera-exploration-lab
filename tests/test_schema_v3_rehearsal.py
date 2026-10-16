from __future__ import annotations

import tempfile
import unittest
import sqlite3
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from camera_exploration_lab.recording_ownership import RecordingOwnershipBusyError
from camera_exploration_lab.schema_migrations import (
    SYNTHETIC_DATABASE_APPLICATION_ID,
    SchemaMigrationError,
    migrate_synthetic_database_to_v3,
)
from tools.schema_v3_rehearsal import (
    SYNTHETIC_MARKER,
    _backup_consistently,
    _check_integrity,
    _connect_readonly,
    _seed_v2_fixture,
    _validate_synthetic_v2,
    main as rehearsal_main,
    run_rehearsal,
)


class SchemaV3RehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / SYNTHETIC_MARKER).write_text("synthetic-only\n", encoding="ascii")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_rehearsal_preserves_legacy_data_and_checks_rollback_restore(self) -> None:
        report = run_rehearsal()
        self.assertEqual(report.fixture_schema, 2)
        self.assertEqual(report.migrated_schema, 3)
        self.assertEqual(report.legacy_rows_before, report.legacy_rows_after)
        self.assertEqual(report.legacy_digest_before, report.legacy_digest_after)
        self.assertTrue(report.analysis_unchanged)
        self.assertEqual(report.integrity_check, "ok")
        self.assertEqual(report.foreign_key_violations, 0)
        self.assertTrue(report.failure_rollback_verified)
        self.assertTrue(report.postcheck_failure_rollback_verified)
        self.assertTrue(report.restore_verified)
        self.assertGreater(report.backup_bytes, 0)

    def test_cli_requires_acknowledgement_and_rejects_arbitrary_database_paths(self) -> None:
        for arguments in (
            ["--synthetic-only"],
            ["--synthetic-only", "--acknowledge-synthetic-only", "/some/existing.sqlite3"],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    rehearsal_main(arguments)
            self.assertEqual(caught.exception.code, 2)

    def test_backup_refuses_path_outside_synthetic_directory(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        with self.assertRaisesRegex(RuntimeError, "outside its private synthetic directory"):
            _validate_synthetic_v2(source.path, self.root / "elsewhere")

    def test_backup_rejects_existing_target_without_modifying_it(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        target = self.root / "already-exists.sqlite3"
        target.write_bytes(b"keep this synthetic test artifact")
        before = target.read_bytes()

        with self.assertRaises(FileExistsError):
            _backup_consistently(source, target, self.root)

        self.assertEqual(target.read_bytes(), before)

    def test_backup_refuses_an_active_recording_owner(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        owner = source.acquire_recording_ownership(
            owner_kind="synthetic_test_owner", check_recovery=False
        )
        target = self.root / "blocked-backup.sqlite3"
        try:
            with self.assertRaises(RecordingOwnershipBusyError):
                _backup_consistently(source, target, self.root)
            self.assertFalse(target.exists())
        finally:
            owner.release(owner.owner_token)

    def test_migration_fails_closed_on_sqlite_exclusive_lock(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "locked-source.sqlite3")
        before = source.path.read_bytes()
        blocker = sqlite3.connect(source.path)
        blocker.execute("BEGIN EXCLUSIVE")
        original_connect = sqlite3.connect

        def short_timeout_connect(*args, **kwargs):
            kwargs.setdefault("timeout", 0.01)
            return original_connect(*args, **kwargs)

        try:
            with patch("camera_exploration_lab.db.sqlite3.connect", side_effect=short_timeout_connect):
                with self.assertRaisesRegex(SchemaMigrationError, "transaction was rolled back"):
                    migrate_synthetic_database_to_v3(source)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(source.path.read_bytes(), before)
        _validate_synthetic_v2(source.path, self.root)

    def test_online_backup_captures_committed_wal_contents(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "wal-source.sqlite3")
        writer = __import__("sqlite3").connect(source.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE sessions SET notes='committed through synthetic WAL' "
                           "WHERE id='synthetic-import-session'")
            writer.commit()
            wal_path = Path(str(source.path) + "-wal")
            self.assertTrue(wal_path.exists())
            target = self.root / "wal-backup.sqlite3"
            _backup_consistently(source, target, self.root)
            backup = _connect_readonly(target)
            try:
                notes = backup.execute(
                    "SELECT notes FROM sessions WHERE id='synthetic-import-session'"
                ).fetchone()[0]
                self.assertEqual(notes, "committed through synthetic WAL")
            finally:
                backup.close()
        finally:
            writer.close()

    def test_interrupted_backup_removes_only_its_partial_target(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        target = self.root / "interrupted-backup.sqlite3"
        before = source.path.read_bytes()
        calls = 0

        def interrupt(_status: int, _remaining: int, _total: int) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("synthetic backup interruption")

        with self.assertRaisesRegex(RuntimeError, "backup interruption"):
            _backup_consistently(source, target, self.root, progress=interrupt)

        self.assertGreaterEqual(calls, 1)
        self.assertFalse(target.exists())
        self.assertEqual(source.path.read_bytes(), before)

    def test_storage_failure_before_target_creation_leaves_existing_source_untouched(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        target = self.root / "no-space-backup.sqlite3"
        source_before = source.path.read_bytes()
        with patch("tools.schema_v3_rehearsal._exclusive_empty_file", side_effect=OSError("synthetic disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                _backup_consistently(source, target, self.root)
        self.assertFalse(target.exists())
        self.assertEqual(source.path.read_bytes(), source_before)

    def test_backup_verifier_rejects_corrupt_or_unsupported_copy(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        target = self.root / "bad-backup.sqlite3"
        # Construct a deliberately marked file with an unsupported version;
        # verification must reject it before it can be used as a restore source.
        with __import__("sqlite3").connect(target) as connection:
            connection.execute(f"PRAGMA application_id={SYNTHETIC_DATABASE_APPLICATION_ID}")
            connection.execute("PRAGMA user_version=55")
        with self.assertRaisesRegex(RuntimeError, "not a marked schema-v2"):
            _validate_synthetic_v2(target, self.root)

    def test_failed_backup_verification_removes_only_fresh_copy(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        source_before = source.path.read_bytes()
        target = self.root / "verification-failure.sqlite3"
        with patch(
            "tools.schema_v3_rehearsal._validate_synthetic_v2",
            side_effect=(None, RuntimeError("synthetic backup verification failure")),
        ):
            with self.assertRaisesRegex(RuntimeError, "verification failure"):
                _backup_consistently(source, target, self.root)
        self.assertFalse(target.exists())
        self.assertEqual(source.path.read_bytes(), source_before)

    def test_read_only_integrity_verification_enables_foreign_keys(self) -> None:
        source, _, _ = _seed_v2_fixture(self.root / "source.sqlite3")
        connection = _connect_readonly(source.path)
        try:
            integrity, count = _check_integrity(connection)
            self.assertEqual(integrity, "ok")
            self.assertEqual(count, 0)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
