"""Cross-process ownership and read-only recovery inspection for recordings.

The lock is a cooperative Linux ``flock`` on the SQLite database inode. It is
intentionally separate from SQLite's own transaction locks and does not alter
the schema. Current CEL recording paths use it for the full recording lifetime.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .db import Database


class RecordingOwnershipError(RuntimeError):
    """Base error for cooperative recording ownership."""


class RecordingOwnershipBusyError(RecordingOwnershipError):
    """Another process or recorder owns the database inode."""


class RecordingRecoveryRequiredError(RecordingOwnershipError):
    """Unresolved durable recording state blocks a new recording owner."""


class RecordingOwnershipLostError(RecordingOwnershipError):
    """The owner handle is closed or no longer identifies this database."""


class RecoveryInspectionError(RuntimeError):
    """Read-only recovery inspection could not safely read the database."""


class RecoveryClassification(StrEnum):
    ACTIVE_OWNER = "active_owner"
    PENDING_UNSTARTED = "pending_unstarted"
    COMPLETED = "completed"
    DELIBERATELY_INTERRUPTED = "deliberately_interrupted"
    PARTIAL = "partial"
    FAILED = "failed"
    INDETERMINATE_AFTER_CRASH = "indeterminate_after_crash"
    INCOMPLETE_PERSISTENCE_LOSS = "incomplete_persistence_loss"
    INCOMPLETE_DATASET = "incomplete_dataset"


class RecoveryLockState(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class RecordingOwnership:
    """A process-held database lock with a unique owner token.

    Unresolved handles stay registered after writer failure. They are released
    only by the owner after successful finalization, or by kernel process exit.
    """

    owner_token: str
    owner_kind: str
    database_path: Path
    device: int
    inode: int
    fd: int
    owner_pid: int
    _released: bool = False
    _release_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def released(self) -> bool:
        return self._released

    def assert_active(self, database: Database, owner_token: str | None = None) -> None:
        if owner_token is not None and owner_token != self.owner_token:
            raise RecordingOwnershipLostError("recording owner token does not match")
        if self.owner_pid != os.getpid():
            raise RecordingOwnershipLostError("recording ownership belongs to another process")
        if self._released or _ACTIVE_OWNERS.get(self.owner_token) is not self:
            raise RecordingOwnershipLostError("recording ownership is no longer active")
        try:
            current = database.path.stat()
            held = os.fstat(self.fd)
        except OSError as exc:
            raise RecordingOwnershipLostError("recording database identity is unavailable") from exc
        if (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise RecordingOwnershipLostError("recording database path now refers to another file")
        if (held.st_dev, held.st_ino) != (self.device, self.inode):
            raise RecordingOwnershipLostError("recording lock no longer refers to the database")

    def release(self, owner_token: str) -> None:
        """Release only this owner's lock; callers must do so after commit."""
        if owner_token != self.owner_token:
            raise RecordingOwnershipLostError("recording owner token does not match")
        if self.owner_pid != os.getpid():
            raise RecordingOwnershipLostError("recording ownership belongs to another process")
        with self._release_lock:
            if self._released:
                raise RecordingOwnershipLostError("recording ownership was already released")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError as exc:
                # Retain the registry entry and fail closed. The caller must
                # report uncertain ownership cleanup rather than retry recording.
                raise RecordingOwnershipError("recording ownership release failed") from exc
            self._released = True
            _ACTIVE_OWNERS.pop(self.owner_token, None)


_ACTIVE_OWNERS: dict[str, RecordingOwnership] = {}
_REGISTRY_LOCK = threading.RLock()


def _drop_inherited_owner_handles() -> None:
    """A forked child must neither reuse nor accidentally extend a parent lease."""
    global _REGISTRY_LOCK
    for owner in tuple(_ACTIVE_OWNERS.values()):
        try:
            # Do not issue LOCK_UN: the parent shares this open file description.
            os.close(owner.fd)
        except OSError:
            pass
        owner._released = True
    _ACTIVE_OWNERS.clear()
    _REGISTRY_LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_drop_inherited_owner_handles)


def acquire_recording_ownership(
    database: Database,
    *,
    owner_kind: str,
    owner_token: str | None = None,
    check_recovery: bool = True,
) -> RecordingOwnership:
    """Acquire a nonblocking inode lock before opening a recording transaction.

    This function does not create databases. A writable, initialized file-backed
    CEL database is required. SQLite's existing v3 lease is checked after the
    OS lock is held; it is never stolen or deleted here.
    """
    if not owner_kind.strip():
        raise ValueError("owner_kind is required")
    path = database.path
    if str(path) == ":memory:" or not path.is_file():
        raise RecordingOwnershipError("recording ownership requires an existing file-backed database")
    token = owner_token or str(uuid4())
    if not token.strip():
        raise ValueError("owner_token is required")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise RecordingOwnershipError("recording database cannot be locked") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecordingOwnershipBusyError("another recording owner holds this database") from exc
        stat = os.fstat(fd)
        path_stat = path.stat()
        if (stat.st_dev, stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise RecordingOwnershipError("recording database changed during ownership acquisition")
        lease = RecordingOwnership(
            owner_token=token,
            owner_kind=owner_kind,
            database_path=path,
            device=stat.st_dev,
            inode=stat.st_ino,
            fd=fd,
            owner_pid=os.getpid(),
        )
        registered = False
        with _REGISTRY_LOCK:
            if token in _ACTIVE_OWNERS:
                raise RecordingOwnershipError("recording owner token is already active")
            _ACTIVE_OWNERS[token] = lease
            registered = True
        if check_recovery:
            try:
                _check_unresolved_recordings(database)
            except BaseException:
                lease.release(token)
                raise
        return lease
    except BaseException:
        # Close a newly opened descriptor if ownership was not registered. A
        # duplicate token can be rejected after flock succeeds on a different
        # database inode, so the presence of a local lease object alone does
        # not mean that its descriptor is safely owned by the registry.
        if not locals().get("registered", False):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def require_recording_ownership(
    database: Database,
    owner_token: str,
) -> RecordingOwnership:
    with _REGISTRY_LOCK:
        lease = _ACTIVE_OWNERS.get(owner_token)
    if lease is None:
        raise RecordingOwnershipLostError("recording owner is not active in this process")
    lease.assert_active(database, owner_token)
    return lease


def _check_unresolved_recordings(
    database: Database,
) -> None:
    try:
        with database.connect_readonly() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {2, 3}:
                raise RecordingRecoveryRequiredError("database schema version is unsupported for recording")
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "recording_leases" in tables:
                lease = connection.execute(
                    "SELECT session_id FROM recording_leases WHERE lease_key=1"
                ).fetchone()
                if lease is not None:
                    raise RecordingRecoveryRequiredError(
                        "database has an unresolved grouped recording lease; inspect it before recording"
                    )
            if "group_recordings" in tables:
                rows = connection.execute(
                    "SELECT session_id, status FROM group_recordings "
                    "WHERE status IN ('recording','degraded')"
                ).fetchall()
                if rows:
                    raise RecordingRecoveryRequiredError(
                        "database has a nonterminal grouped experiment requiring inspection"
                    )
            if {"sessions", "benchmark_runs"} <= tables:
                row = connection.execute(
                    """SELECT s.id FROM sessions s JOIN benchmark_runs b ON b.session_id=s.id
                       WHERE s.source='live_experiment' AND b.status='running' LIMIT 1"""
                ).fetchone()
                if row is not None:
                    raise RecordingRecoveryRequiredError(
                        "database has an unresolved single-camera recording; recover it before recording"
                    )
    except RecordingRecoveryRequiredError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise RecordingRecoveryRequiredError(
            "recording state could not be validated safely"
        ) from exc


def _process_identity_alive(pid: object, expected_start: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    if not isinstance(expected_start, str) or not expected_start:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        actual_start = stat[stat.rfind(")") + 2 :].split()[19]
        return actual_start == expected_start
    except (OSError, IndexError):
        return False


@dataclass(frozen=True, slots=True)
class RecoveryStream:
    slot: str | None
    benchmark_id: str
    status: str
    measurement_count: int
    last_elapsed_ns: int | None
    last_elapsed_seconds: float | None
    capture_frame_count: int | None = None
    read_failure_count: int | None = None
    timing_queue_loss_count: int | None = None
    persistence_loss_count: int | None = None
    sequence_gap_count: int | None = None
    data_complete: bool | None = None

    @property
    def persistence_loss_observed(self) -> bool:
        return any((
            (self.timing_queue_loss_count or 0) > 0,
            (self.persistence_loss_count or 0) > 0,
            (self.sequence_gap_count or 0) > 0,
        ))


@dataclass(frozen=True, slots=True)
class RecoveryFinding:
    session_id: str
    kind: str
    status: str
    classification: RecoveryClassification
    streams: tuple[RecoveryStream, ...]
    os_lock_state: RecoveryLockState
    lease_present: bool
    owner_pid: int | None
    owner_process_alive: bool
    last_event_type: str | None = None
    last_event_at: str | None = None
    missing_tail_size_known: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    schema_version: int
    lock_state: RecoveryLockState
    findings: tuple[RecoveryFinding, ...]


def inspect_recording_recovery(database: Database) -> RecoveryInspection:
    """Take a read-only snapshot; never clear leases or update experiment rows."""
    fd, lock_state = _probe_database_lock(database.path)
    try:
        with database.connect_readonly() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            findings: list[RecoveryFinding] = []
            if {"sessions", "benchmark_runs", "measurements"} <= tables:
                findings.extend(_inspect_single_runs(connection, tables, lock_state))
            if {"group_recordings", "benchmark_streams", "experiment_events"} <= tables:
                findings.extend(_inspect_groups(connection, tables, lock_state))
            return RecoveryInspection(version, lock_state, tuple(findings))
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise RecoveryInspectionError("recording recovery inspection failed") from exc
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _probe_database_lock(path: Path) -> tuple[int | None, RecoveryLockState]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None, RecoveryLockState.UNKNOWN
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, RecoveryLockState.HELD
    except OSError:
        os.close(fd)
        return None, RecoveryLockState.UNKNOWN
    return fd, RecoveryLockState.AVAILABLE


def _classify(
    status: str,
    *,
    lock_state: RecoveryLockState,
    lease_present: bool,
    owner_alive: bool,
    streams: tuple[RecoveryStream, ...],
    dataset_incomplete: bool = False,
) -> RecoveryClassification:
    persistence_loss = any(stream.persistence_loss_observed for stream in streams)
    incomplete = any(stream.data_complete is False for stream in streams)
    if persistence_loss:
        return RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS
    if incomplete or dataset_incomplete:
        return RecoveryClassification.INCOMPLETE_DATASET
    # A durable lease alongside a terminal row is contradictory. Do not
    # silently accept the status as authoritative or clear the lease.
    if lease_present and status in {"completed", "partial", "interrupted", "failed"}:
        return RecoveryClassification.INDETERMINATE_AFTER_CRASH
    if status == "completed":
        return RecoveryClassification.COMPLETED
    if status == "partial":
        return RecoveryClassification.PARTIAL
    if status == "interrupted":
        return RecoveryClassification.DELIBERATELY_INTERRUPTED
    if status == "failed":
        return RecoveryClassification.FAILED
    active_state = status in {"running", "recording", "degraded"} or (
        status == "pending" and lease_present
    )
    if active_state and lock_state is RecoveryLockState.HELD:
        return RecoveryClassification.ACTIVE_OWNER
    if status == "pending" and not lease_present:
        return RecoveryClassification.PENDING_UNSTARTED
    if lease_present or status in {"running", "recording", "degraded"}:
        # A live PID without the cooperative lock is not proof that the writer
        # is active; preserve the ambiguity instead of treating age/PID as a lease.
        return RecoveryClassification.INDETERMINATE_AFTER_CRASH
    return RecoveryClassification.INDETERMINATE_AFTER_CRASH


def _inspect_single_runs(
    connection: sqlite3.Connection,
    tables: set[str],
    lock_state: RecoveryLockState,
) -> list[RecoveryFinding]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(measurements)")}
    elapsed_ns_sql = "MAX(m.elapsed_ns)" if "elapsed_ns" in columns else "NULL"
    summary_join = ""
    summary_column = "NULL AS summary_json"
    if "benchmark_summaries" in tables:
        summary_join = "LEFT JOIN benchmark_summaries z ON z.benchmark_id=b.id"
        summary_column = "z.summary_json"
    rows = connection.execute(
        f"""SELECT s.id AS session_id, s.source, s.metadata_json, b.id AS benchmark_id,
                   b.status, COUNT(m.id) AS measurement_count, {elapsed_ns_sql} AS last_elapsed_ns,
                   MAX(m.elapsed_seconds) AS last_elapsed_seconds, {summary_column} AS summary_json
            FROM sessions s JOIN benchmark_runs b ON b.session_id=s.id
            {summary_join}
            LEFT JOIN measurements m ON m.benchmark_id=b.id
            WHERE s.source='live_experiment' AND b.status!='completed'
            GROUP BY s.id,b.id ORDER BY s.id,b.id"""
    ).fetchall()
    results: list[RecoveryFinding] = []
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        pid = metadata.get("owner_pid")
        alive = _process_identity_alive(pid, metadata.get("owner_process_start_ticks"))
        stream = RecoveryStream(
            slot=None,
            benchmark_id=str(row["benchmark_id"]),
            status=str(row["status"]),
            measurement_count=int(row["measurement_count"]),
            last_elapsed_ns=(int(row["last_elapsed_ns"]) if row["last_elapsed_ns"] is not None else None),
            last_elapsed_seconds=(float(row["last_elapsed_seconds"]) if row["last_elapsed_seconds"] is not None else None),
        )
        status = str(row["status"])
        summary = _json_object(row["summary_json"])
        termination_reason = summary.get("termination_reason")
        findings_class = _classify(
            status, lock_state=lock_state, lease_present=False, owner_alive=alive,
            streams=(stream,),
        )
        if status == "running" and lock_state is RecoveryLockState.AVAILABLE:
            findings_class = RecoveryClassification.INDETERMINATE_AFTER_CRASH
        if status == "interrupted" and termination_reason == "application_shutdown_recovery":
            findings_class = RecoveryClassification.INDETERMINATE_AFTER_CRASH
        elif status == "interrupted" and not isinstance(termination_reason, str):
            # Older rows may have a terminal status but no durable reason from
            # which an intentional stop can be distinguished from recovery.
            findings_class = RecoveryClassification.INDETERMINATE_AFTER_CRASH
        if termination_reason in {"timing_queue_overflow", "database_write_failure"}:
            # Schema v2 records the reason but has no exact per-sample loss
            # counter. Incompleteness is known; the unpersisted count is not.
            findings_class = RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS
        results.append(RecoveryFinding(
            session_id=str(row["session_id"]), kind="single_camera", status=status,
            classification=findings_class, streams=(stream,), os_lock_state=lock_state,
            lease_present=False, owner_pid=pid if isinstance(pid, int) else None,
            owner_process_alive=alive,
            missing_tail_size_known=(
                findings_class not in {
                    RecoveryClassification.INDETERMINATE_AFTER_CRASH,
                    RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS,
                }
            ),
        ))
    return results


def _inspect_groups(
    connection: sqlite3.Connection,
    tables: set[str],
    lock_state: RecoveryLockState,
) -> list[RecoveryFinding]:
    has_lease = "recording_leases" in tables
    lease = None
    if has_lease:
        lease = connection.execute(
            "SELECT session_id, owner_pid, owner_process_start_ticks "
            "FROM recording_leases WHERE lease_key=1"
        ).fetchone()
    groups = connection.execute(
        """SELECT session_id, status, data_complete, owner_pid, owner_process_start_ticks
           FROM group_recordings ORDER BY session_id"""
    ).fetchall()
    findings: list[RecoveryFinding] = []
    for group in groups:
        session_id = str(group["session_id"])
        lease_for_group = lease is not None and str(lease["session_id"]) == session_id
        streams: list[RecoveryStream] = []
        child_rows = connection.execute(
            """SELECT bs.slot, bs.benchmark_id, bs.stream_status, bs.capture_frame_count,
                      bs.read_failure_count, bs.timing_queue_loss_count, bs.persistence_loss_count,
                      bs.sequence_gap_count, bs.data_complete, COUNT(m.id) AS measurement_count,
                      MAX(m.elapsed_ns) AS last_elapsed_ns, MAX(m.elapsed_seconds) AS last_elapsed_seconds
               FROM benchmark_streams bs LEFT JOIN measurements m ON m.benchmark_id=bs.benchmark_id
               WHERE bs.session_id=? GROUP BY bs.slot,bs.benchmark_id ORDER BY bs.slot""",
            (session_id,),
        ).fetchall()
        for child in child_rows:
            streams.append(RecoveryStream(
                slot=str(child["slot"]), benchmark_id=str(child["benchmark_id"]),
                status=str(child["stream_status"]), measurement_count=int(child["measurement_count"]),
                last_elapsed_ns=(int(child["last_elapsed_ns"]) if child["last_elapsed_ns"] is not None else None),
                last_elapsed_seconds=(float(child["last_elapsed_seconds"]) if child["last_elapsed_seconds"] is not None else None),
                capture_frame_count=int(child["capture_frame_count"]),
                read_failure_count=int(child["read_failure_count"]),
                timing_queue_loss_count=int(child["timing_queue_loss_count"]),
                persistence_loss_count=int(child["persistence_loss_count"]),
                sequence_gap_count=int(child["sequence_gap_count"]),
                data_complete=bool(child["data_complete"]),
            ))
        latest = connection.execute(
            "SELECT event_type, observed_at FROM experiment_events WHERE session_id=? "
            "ORDER BY event_id DESC LIMIT 1", (session_id,),
        ).fetchone()
        owner_pid = lease["owner_pid"] if lease_for_group else group["owner_pid"]
        owner_start = lease["owner_process_start_ticks"] if lease_for_group else group["owner_process_start_ticks"]
        alive = _process_identity_alive(owner_pid, owner_start)
        status = str(group["status"])
        data_complete = bool(group["data_complete"])
        classification = _classify(
            status, lock_state=lock_state, lease_present=lease_for_group,
            owner_alive=alive, streams=tuple(streams), dataset_incomplete=not data_complete,
        )
        findings.append(RecoveryFinding(
            session_id=session_id, kind="grouped", status=status,
            classification=classification, streams=tuple(streams),
            os_lock_state=lock_state, lease_present=lease_for_group,
            owner_pid=int(owner_pid) if owner_pid is not None else None,
            owner_process_alive=alive,
            last_event_type=str(latest["event_type"]) if latest else None,
            last_event_at=str(latest["observed_at"]) if latest else None,
            missing_tail_size_known=(
                classification not in {
                    RecoveryClassification.INDETERMINATE_AFTER_CRASH,
                    RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS,
                }
            ),
        ))
    return findings


def _json_object(value: str | None) -> dict[str, object]:
    try:
        result = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def validate_recovery_candidate_readonly(database: Database, session_id: str) -> RecoveryFinding:
    """Validate a point-in-time candidate without changing it or authorizing unlock.

    A later recovery transaction must acquire and retain the database lock across
    confirmation, finalization, commit, and durable lease resolution.
    """
    inspection = inspect_recording_recovery(database)
    if inspection.lock_state is not RecoveryLockState.AVAILABLE:
        raise RecoveryInspectionError("database is locked by an active or unknown owner")
    for finding in inspection.findings:
        if finding.session_id == session_id:
            if finding.classification is RecoveryClassification.ACTIVE_OWNER:
                raise RecoveryInspectionError("recording still has an active owner")
            if finding.classification in {
                RecoveryClassification.COMPLETED,
                RecoveryClassification.PENDING_UNSTARTED,
            }:
                raise RecoveryInspectionError("recording does not require recovery")
            return finding
    raise RecoveryInspectionError("recording was not found in the recovery inspection")
