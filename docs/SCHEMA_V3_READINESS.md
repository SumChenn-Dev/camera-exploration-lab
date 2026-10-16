# CEL-013 Pass 2E.1 — Schema-v3 Readiness

## Current startup boundary

The normal CLI, GUI, Live Workbench, and ordinary History/Analysis startup continue to use `Database.initialize()` at schema version 2. `initialize()` creates or opens v2 and rejects a version newer than v2; it does not call the v2-to-v3 migration. The explicit migration remains in `schema_migrations.py` and is synthetic-fixture gated. There is no production migration option, hidden environment switch, or grouped Record Both control.

The additive v3 schema preserves all preexisting `sessions`, `benchmark_runs`, imports, settings, summaries, and measurements. New grouped status and membership are represented only by `group_recordings` and `benchmark_streams`. Legacy sessions are not assigned a group based on their camera or timestamps.

## Compatibility adapter

`application_compatibility.py` validates the v2/v3 structure without modifying it and returns structured compatibility failures and capabilities. A capability requires both a recognized schema and an explicit runtime feature profile. The production profile shipped here enables legacy History/Analysis and schema-v2 single-camera recording but does not enable schema-v3 access. The synthetic reader profile requires the synthetic application marker and enables grouped reads only; it still does not enable grouped recording.

Read operations use a read-only, query-only connection. The unified history model retains the existing single-camera/imported rows and, when grouped History is enabled, presents each explicit group once while omitting its child runs from the generic list. The selected experiment kind and IDs determine whether the established single-run Analysis reader or grouped Analysis reader is called. This adapter is not connected to MainWindow or the ordinary application startup.

The existing generic `Database.history_entries()` and `analysis.list_experiments()` joins are still legacy-oriented. Direct use against v3 can expose child runs as ordinary rows. The adapter prevents that ambiguity for the synthetic compatibility tests; future production v3 UI work must route through an approved compatibility service before advertising grouped support.

## Synthetic migration and backup rehearsal

Run from a checkout using the existing CEL Python environment:

```bash
cd "$HOME/Documents/Codex/Camera-Exploration-Lab-public-snapshot"
VENV="$HOME/.local/share/camera-exploration-lab/venv"
PYTHONPATH=src "$VENV/bin/python" tools/schema_v3_rehearsal.py \
  --synthetic-only --acknowledge-synthetic-only
```

The command accepts no input or database path. It creates a marked v2 fixture, imported and native synthetic sessions, multiple runs, settings, summaries, sparse and interrupted samples, Unicode labels, and a bounded 5,000-row synthetic timing series. A private temporary directory holds the source, online SQLite backup, separate migration copy, rollback copy, and restore copy. All are removed after the printed digest/timing summary. The command does not inspect the application's configured database.

The SQLite backup API is used instead of copying the database file, so committed WAL content is included in the snapshot. A cooperative database owner is acquired and active owners are rejected. The backup is written as a new file, verified by schema/application marker/integrity/foreign-key/content checks, then fsynced together with its temporary parent directory. Fsync success does not prove the host storage device has made durable media writes; this temporary rehearsal is not a production backup procedure.

In the implementation rehearsal, 5,019 legacy rows across the existing tables retained the same deterministic content digest before and after migration. Existing single-camera Analysis output was unchanged. `integrity_check` returned `ok`; foreign-key violations were zero. Failure injection after DDL and during post-migration integrity validation both rolled back to the exact v2 schema/content digest. A separate database restored from the verified backup passed v2 schema and content checks. A WAL-specific test confirmed that a committed fixture update present in `-wal` was included in the backup.

## Synthetic resource benchmark

The benchmark requires the same explicit acknowledgement and accepts no paths:

```bash
PYTHONPATH=src "$VENV/bin/python" tools/benchmark_schema_v3_readiness.py \
  --synthetic-only --acknowledge-synthetic-only
```

It measures SQLite and application query work for synthetic 30-second, 5-minute, and 30-minute timing volumes at 100+24 and 187+30 events/s. Samples are generated as bounded 512-row batches. The grouped-history test uses actual SQLite rows, and Analysis is computed from full persisted data. The results do not measure physical capture or real-time writer performance.

| Synthetic duration / rates | Persisted samples | Database / backup size | Batch insert throughput | Grouped Analysis preparation |
|---|---:|---:|---:|---:|
| 30 s, 100+24/s | 3,720 | 3.76 MB | 52.5k rows/s | 186 ms |
| 30 s, 187+30/s | 6,510 | 5.06 MB | 57.4k rows/s | 309 ms |
| 5 min, 100+24/s | 37,200 | 19.47 MB | 52.8k rows/s | 1.74 s |
| 5 min, 187+30/s | 65,100 | 32.61 MB | 51.3k rows/s | 3.12 s |
| 30 min, 100+24/s | 223,200 | 107.79 MB | 47.2k rows/s | 10.57 s |
| 30 min, 187+30/s | 390,600 | 186.55 MB | 45.5k rows/s | 18.88 s |

Across these runs, schema migration took 21–22 ms; v3 backup took 18 ms to 1.18 s; integrity checks took 13 ms to 1.10 s; opening the database took 0.2–0.3 ms. Legacy History reads were below 1 ms and grouped History list reads about 0.9–1.0 ms in these small-history fixtures. Traced Python peak memory was 0.8–2.7 MiB; this excludes native SQLite allocations and operating-system page cache. Grouped Analysis time grows with the input and must stay off a Qt GUI thread for larger histories. Each database passed integrity and foreign-key checks and all generated sample counts matched the database row counts.

These values are one local synthetic run per scenario and vary with storage, SQLite/Python version, filesystem cache, and CPU. The benchmark writes through the repository in bounded transactions; it is not a measurement of the grouped writer coordinator.

## Read-only recovery policy

| Evidence | Read-only classification | Required future handling |
|---|---|---|
| Terminal completed group, no SQL lease, complete data | Completed | No recovery action |
| Terminal interrupted group, no SQL lease, terminal stream/event evidence | Deliberately interrupted only when persisted evidence supports it; otherwise indeterminate | Preserve current status; inspect before any proposed repair |
| Recording/degraded state and OS lock is held | Active owner | Do not mutate or unlock |
| Recording/degraded state, no OS lock, SQL lease remains | Indeterminate after crash | Keep lease; inspect committed rows and last event, do not infer missing tail size |
| Terminal status plus a surviving SQL lease | Indeterminate/contradictory | Do not mark complete or clear lease automatically |
| Queue/persistence loss, sequence gaps, or incomplete flags | Incomplete persistence/data | Retain loss counters and incomplete state; missing sample count may be unknown |
| Failed group with terminal failure evidence | Failed | Preserve failure and child stream outcomes |
| Pending group without a lease or recording events | Pending/unstarted | No recovery mutation |
| Lock state unknown or database cannot be safely inspected | Unavailable/unknown | Fail closed; no lease clearing |

Read-only inspection never updates a status or deletes a lease. Any later recovery mutation requires an explicit user choice, a confirmed OS-lock acquisition, a fresh integrity/foreign-key check, the last committed event/sample evidence, an audit event, and a transaction that preserves the original failure state. No exact loss count may be invented from a missing tail. A recovery action may classify an experiment as interrupted or failed; it may never turn an uncertain or incomplete dataset into Completed.

## Activation gates

Before a real database could be migrated, a separately reviewed milestone must provide: explicit per-database user approval; source-version and schema validation; no active recording owner; a verified SQLite online backup placed on durable storage; backup reopen, integrity, foreign-key, and content verification; exclusive access for migration; transactional migration; post-migration checks; and a tested restoration plan. The application must explain that older CEL versions reject schema v3 and may not be used to reopen the migrated database. Existing History/Analysis readers must route through the v3-compatible data service before the production app accepts v3.

Activation cannot rely only on a pathname check or an environment variable. The target must be explicitly selected and opened without fallback, ownership and schema must be confirmed on the same database identity, and a repeated activation must be idempotent. Restoring a backup after a later application defect is distinct from rolling back a failed DDL transaction and must be a separately confirmed operation.

The next proposed stage is production-read compatibility integration against synthetic mixed v2/v3 databases, followed by an explicit activation review. Live grouped recording remains unavailable until the compatibility service, ownership/recovery workflow, database backup procedure, and separately reviewed GUI are approved. No real migration is authorized by this readiness work.
