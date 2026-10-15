# CEL-013 Pass 2A — Grouped Recording Schema Foundation

## Scope and activation gate

Pass 2A adds a v3 schema and repository API for future grouped experiments. It does **not** attach to capture workers, persist live camera timings from the GUI, add History/Analysis UI, or store media.

Normal application initialization remains on schema version 2 (SCHEMA_VERSION = 2). Database.initialize() does not invoke the v3 migration. It retains the current behavior: a v1 file may be upgraded additively to v2, and a database with a version newer than v2 is rejected. The only v3 entry point is migrate_synthetic_database_to_v3(), which requires an existing file explicitly marked with the synthetic test application ID. There is no CLI option, GUI startup call, or production data path to enable it.

This is deliberately a synthetic-only foundation. A user-database migration requires a separate approval and release gate, explicit backup/restore procedures, additional compatibility review, and testing against synthetic copies only. Never point this migration at real experiment data.

The upstream review supports keeping CEL's experiment, provenance and analysis model in CEL. `v4l-utils` remains an optional external diagnostic tool and `cameractrls` an optional external controls application; neither is copied or added as a dependency here. Their licenses and the other reviewed project's distribution obligations remain component-specific. This schema work incorporates no third-party source or runtime dependency, and it does not replace the existing capture backend with a general-purpose multimedia recorder.

## Relationships

A grouped session is an explicit row in group_recordings. The existing sessions table remains the logical experiment parent, with camera_id = NULL. Exactly two benchmark_runs belong to that session, and benchmark_streams maps them to slots A and B, camera identities, endpoint keys, configuration provenance and independent counters.

```text
cameras ──────────────┐
                      ├── benchmark_streams ── benchmark_runs ── measurements
sessions ─ group_recordings                         ├──────────── setting_snapshots
  │                                                  └──────────── benchmark_summaries
  └── experiment_events
```

Existing single-camera and imported sessions have no group marker. Their camera identity continues to come from sessions.camera_id; no inference is made from matching labels, timestamps, model names, or measurement rates. Existing measurements, raw CSV rows, import provenance and summaries are not rewritten.

## Schema v3 additions

- group_recordings: one parent status, target duration, wall-clock event markers, monotonic clock epoch identifier, timing-method version, owner-process snapshot, completeness flag, structured integrity flags and metadata.
- benchmark_streams: exactly slot A or B; unique run association; camera FK with ON DELETE SET NULL; endpoint identity key; historical identity snapshot; timestamp source, clock type, collection point and timing method; per-stream state; first/last host-arrival bounds and separate first/last persisted-sample bounds; last persisted sample index; capture, read-failure, invalid-frame, queue-loss, persistence-loss, detected sequence-gap and optional preview-skip counters; stream completeness and flags.
- experiment_events: group or child lifecycle event with optional group-relative integer nanoseconds, UTC marker, structured reason code and JSON details.
- recording_leases: one database-wide lease slot with session, random owner token, PID and process-start identity. This is schema/repository support only; the current single-camera recorder and future coordinator do not yet acquire it. Cross-process enforcement begins only when a later coordinator consistently uses it.
- measurements.elapsed_ns: optional exact experiment-relative integer nanoseconds.
- measurements.source_timestamp_ns, source_sequence, source_clock: optional driver/backend provenance. They remain NULL for the current OpenCV read-return path unless a backend genuinely supplies those values.

A unique (benchmark_runs.id, benchmark_runs.session_id) key supports the composite stream-to-run/session foreign key. Stream slots are constrained to A/B and unique within the group; one benchmark run cannot be mapped twice; a canonical endpoint key cannot appear in both slots. A trigger prevents a parent entering a recording or terminal data state unless both stream mappings exist. Repository readiness requires both child streams to be recording.

The sample_index on a capture-scoped frame_arrival is the stream-local CEL sequence. It increases monotonically among persisted rows; gaps are retained. A future coordinator assigns the index before nonblocking enqueue. Each enqueue rejection increments that stream’s timing_queue_loss_count; future samples continue to be offered without waiting, and accepted samples may persist after capacity returns. The repository also counts gaps between observed sample indexes in `sequence_gap_count`, marks the stream and group incomplete, and emits an event even if the coordinator's queue-loss counter is missing. This diagnostic count overlaps with queue/persistence-loss counters; do not sum it as another independent lost-frame total. A gap is evidence of a missing CEL timing record, not proof of a sensor or driver dropped frame.

## States and completion

Group states: pending, recording, degraded, completed, partial, interrupted, failed.

Child states: pending, recording, completed, interrupted, failed. Capture read failures are recorded observations; they do not alone make a timed dataset incomplete. Queue loss, persistence loss, child interruption/failure and explicit integrity warnings do make it incomplete.

Only finalize_group() can write a terminal group result. A completed result requires a fixed requested duration, exactly one monotonic `stream_recording` and `stream_completed` event for each child, caller-reported durations matching those event deltas, both child runs finalized as completed, at least one arrival sample per stream, both stream durations meeting the target, no integrity flags, no queue/persistence loss or detected sequence gaps, and committed per-run summaries. It does not trust a caller-supplied duration alone. Early manual stop remains interrupted or partial; it is not relabeled as full success.

The parent and both child summaries/statuses are finalized in one SQLite transaction. If that transaction cannot commit, the caller must not report completion. Crash-recovery classification and writer shutdown ordering are future coordinator responsibilities.

## Timing meaning

Current OpenCV timing is the host monotonic time after VideoCapture.read() returns. Group-relative elapsed nanoseconds share one in-process monotonic clock epoch; UTC timestamps support human-readable provenance and are not used for duration arithmetic. Separate streams and separate experiments are never called hardware-synchronized. No sensor exposure time, camera latency, USB transfer delay, or physical dropped-frame count is inferred.

The new optional elapsed_seconds compatibility value may be derived from integer elapsed nanoseconds when absent, while the exact integer remains separately stored. Driver-source integer fields reject booleans, floats, negatives and values larger than SQLite’s signed 64-bit integer maximum.

## Migration and tests

The explicit migration recognizes:
- An empty, marked synthetic file (creates the existing v2 schema and then v3).
- The recognized additive v1 fixture layout (adds v2 summaries/indexes inside the same transaction, then v3).
- A compatible v2 schema.
- A valid v3 schema (idempotent no-op).

It rejects unmarked files, unsupported future versions, incomplete/malformed schemas and partially applied v3 objects. DDL is executed statement-by-statement under one BEGIN IMMEDIATE; user_version changes only inside the transaction; foreign-key and integrity checks happen before commit. Failure injection tests verify DDL and data roll back. SQLite errors are reported without exposing the database path.

The migration tests use temporary, synthetic SQLite databases marked with a reserved application ID. Existing single-camera rows are hashed before/after migration. Nothing reads, copies or migrates the user’s local history DB. Normal app startup stays at v2 and does not import or call the synthetic migration entry point.

## Repository API

GroupedExperimentRepository provides:
- create_group(spec): creates the parent, camera rows, two runs, stream mappings, independent requested/reported setting snapshots and a group-created event atomically.
- get_group(session_id) and list_groups(...): return typed records with deterministic A/B ordering, stream summaries, settings and events.
- validate_ready_to_record(session_id): requires both child streams recording and distinct endpoints.
- set_stream_status, set_group_status and finalize_group: enforce transitions and completion rules.
- increment_stream_counters and mark_stream_incomplete: retain separate observation/loss counts and integrity state.
- record_stream_measurements: validates child association, bounded batch size, integer timing and increasing sequence/time; it uses the existing DB batch insertion helper.
- add_event: stores structured reason codes/details.
- acquire_recording_lease and release_recording_lease: model one writer owner and verify release token.

The APIs are not connected to a worker, GUI, or persistent service. A local lease row is not sufficient by itself to claim enforcement; all recording entry points must use it in a future stage. Stale lease recovery must verify PID plus process-start identity and must not steal a live process’s lease.

## Query/index policy

Indexes support child streams filtered by camera, group status lookup, group event retrieval and existing per-run sample-index queries. Existing measurement indexes retain legacy Analysis access. Query plans and insert/query performance are measured with temporary synthetic databases in tools/benchmark_grouped_schema.py; no WAL mode or new dependency is enabled.

### Synthetic SQLite volume and query benchmark

The benchmark streamed synthetic `frame_arrival` rows in batches of 512 for stream A at 187 samples/s and stream B at 24 or 30 samples/s. The database-size measurement was taken after the two-stream recording was finalized and before adding 100 metadata-only groups used to exercise History listing. No camera or user database was opened. Sizes are decimal MB; timings are one local run and are not hardware capture results or performance guarantees.

| Duration | Stream B | Measurement rows | Theoretical at 150–300 B/row | SQLite file size | Insert rate | Analysis retrieval of A |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 s | 24 FPS | 6,330 | 0.95–1.90 MB | 3.15 MB | 112k rows/s | 2.9 ms |
| 30 s | 30 FPS | 6,510 | 0.98–1.95 MB | 3.22 MB | 113k rows/s | 2.7 ms |
| 5 min | 24 FPS | 63,300 | 9.50–18.99 MB | 29.88 MB | 91k rows/s | 26.3 ms |
| 5 min | 30 FPS | 65,100 | 9.77–19.53 MB | 30.74 MB | 90k rows/s | 24.8 ms |
| 30 min | 24 FPS | 379,800 | 56.97–113.94 MB | 179.04 MB | 75k rows/s | 146.9 ms |
| 30 min | 30 FPS | 390,600 | 58.59–117.18 MB | 184.46 MB | 74k rows/s | 147.2 ms |

The measured database was about 471–497 bytes per measurement row in these cases, including SQLite tables and indexes; the earlier 150–300 B/row planning range understates this benchmark's storage. Use roughly 0.18 GB for a 30-minute 187 + 30 sample/s recording as a planning observation, then validate against the eventual sample shape, SQLite build, transaction policy and integrity metadata. Actual collection duration and FPS can vary.

With 100 metadata-only groups added, `list_groups(limit=1000)` returned all 101 groups in about 8 ms; camera-filtered group lookup took about 0.6–0.8 ms, direct group lookup about 0.5 ms, and the unchanged legacy History query about 2 ms. `EXPLAIN QUERY PLAN` used the camera/session, per-run measurement, and group/event indexes. These timings are informational; they do not establish coordinator throughput or justify a queue size. Queue sizing, batch cadence, and writer latency budgets remain Pass 2B decisions.

The 150–300 B/row calculation is retained in benchmark output only as a comparison baseline. It is not a sizing guarantee. The 100 metadata-only rows are excluded from the reported measurement-bearing file size but included in History query timing.

## Pass 2B remains separate

The coordinator will own a bounded queue per stream and a single SQLite writer. Capture callbacks will use only nonblocking enqueue; rejected events increment stream-local loss counters, preserve sequence gaps, and permanently flag data incomplete while future samples continue to be offered. A database failure affects persistence for both streams but must not silently stop capture or be mislabeled as a camera drop. Queue sizes, batch cadence, retry policy, UI feedback and crash recovery still require coordinator implementation and tests.
