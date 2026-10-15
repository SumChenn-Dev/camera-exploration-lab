# CEL-013 Pass 2B.1 / 2B.2 — Grouped Coordinator and Guarded Capture Adapter

## Scope and activation boundary

Pass 2B.1 added the camera-free coordinator for the schema-v3 grouped-recording repository. Pass 2B.2 adds a capture-event adapter that can subscribe to the timing callback of an already-running `OpenCVCaptureWorker`, plus a two-stream orchestration bridge. This code is **not** connected to `MultiCameraWorkspace`, PySide6, History, Analysis, or a GUI recording control. The only executable entry point is a developer harness that creates its own fresh marked synthetic database in a temporary directory. No physical camera or ordinary application database is opened.

Normal application startup remains on schema v2. `Database.initialize()` does not call the explicit synthetic-only v3 migration. The harness requires `--synthetic-only`, has no `--database` argument, refuses a non-fresh harness directory, and deletes its temporary database at exit. User-reported Bazzite Pass 1 validation showed concurrent preview at 100 FPS requested/readback for the PS3 Eye and 30 FPS requested/readback for the AVerMedia, plus independent stop behavior. This is not evidence for grouped recording, high-rate dual capture, driver timestamps, or synchronization.

## Ownership and API boundary

`GroupedRecordingCoordinator` composes the existing `GroupedExperimentRepository`; it does not introduce another schema, measurement insertion path, or camera backend. The caller first creates a group with two participant streams. `coordinator.start()` then acquires the database-wide lease and starts one writer thread. `GroupedCaptureRecordingBridge.start_recording()` accepts two already-active worker objects, then starts or explicitly fails each coordinator stream. Each `CaptureEventAdapter` claims that worker’s single timing-listener slot and directly submits only timestamp metadata to the coordinator.

The producer calls do not perform SQLite operations, wait for queue space, enqueue frame images, or emit one Qt signal per frame. They briefly update stream-local counters/sequences and use `put_nowait()` on that stream’s bounded queue. Bridge/coordinator start, stop, failure and finalization methods wait for writer acknowledgements and therefore must run outside a Qt event callback; a future GUI integration needs a dedicated orchestration worker. `stop_recording()` detaches timing only and leaves capture/preview workers running. `capture_ended(slot, ...)` finalizes one child while leaving the other worker and adapter active. `shutdown()` closes callback intake before bounded coordinator cancellation. No bridge method requests that a camera open, close, or change format.

`OpenCVCaptureWorker` samples `time.monotonic_ns()` immediately after `VideoCapture.read()` returns and creates a small `CaptureTimingEvent`. The adapter passes that event’s monotonic timestamp and UTC marker unchanged; it does not read a preview timer, render a frame, copy pixels, or submit an image. One successful capture callback causes at most one `submit_frame_arrival()` call. Read failures call `submit_read_failure()` and stay separate from sample-queue or database loss. Driver/source timestamp fields remain NULL.

The worker’s legacy `set_timing_listener()` remains available to the single-camera recorder. New subscriptions are exclusive and token-owned: the adapter cannot overwrite an existing listener, and its close operation cannot clear a listener that another owner installed later. Capture callbacks already copied by the worker are guarded after detach. Detach waits a bounded time for callbacks already inside submission before the bridge asks the coordinator to stop/finalize.

Useful API calls are:

```python
coordinator.start()
coordinator.start_stream("A", host_monotonic_ns=ready_a_ns)
coordinator.start_stream("B", host_monotonic_ns=ready_b_ns)
coordinator.submit_frame_arrival("A", host_monotonic_ns=read_return_a_ns)
coordinator.submit_read_failure("B", host_monotonic_ns=failed_read_b_ns)
coordinator.stop_stream("A", host_monotonic_ns=recording_stop_a_ns)
coordinator.stop_stream("B", host_monotonic_ns=recording_stop_b_ns)
group = coordinator.finalize(reason_code="manual_stop")
```

The coordinator APIs and bridge are exercised with fake workers in camera-free tests. The developer harness below is the only runnable integration demonstration; it never accepts a user database path or contacts a camera.

## Bounded queues and writer policy

Each stream has an independent `queue.Queue` with a configurable hard capacity. The initial default is 2,048 small timing records per stream; the default SQLite measurement batch is 128, bounded by the repository's 1,024-row maximum. A separate lifecycle-command queue and its pending dispatch buffer are each capped at 16 entries. Queue capacity counts timing records, including persisted read-failure observations. No queue stores video frames or images.

The writer takes at most one batch from a stream, then gives the sibling an opportunity before returning to the first stream. It writes through `GroupedExperimentRepository.record_stream_measurements()`, which uses a bounded batch transaction. It polls at a short interval when idle and is also woken by submissions. SQLite journal mode is unchanged; WAL is not enabled.

If a sample queue is full, the current sample is rejected; the oldest sample is never displaced. The producer immediately increments that stream's `timing_queue_loss_count`, and later submissions continue to try nonblocking enqueue. Once space returns, later samples can be persisted. Frame-arrival sequence numbers are assigned before enqueue, so persisted samples after an overflow retain gaps. Queue loss and sequence-gap counts overlap diagnostically; they are not added together as separate camera frame drops.

`capture_frame_count` and `read_failure_count` count host observations, while a summary's `frame_count` and `read_failures` count persisted measurement rows. A difference means the stored timing dataset is incomplete. Queue overflow permanently prevents an intact completion when the stream-boundary counters are committed. Read failures are observations and do not by themselves make timing persistence incomplete.

### Writer failure behavior

A measurement or metadata transaction exception is terminal for this coordinator. The failed batch is not retried; later submissions are rejected as `persistence_loss_count`, not queue overflow. Accepted but uncommitted records are counted as persistence loss. The writer makes one bounded best-effort attempt to persist counters and a `writer_failure` integrity event. It then stops; it never reports a successful finalization. If SQLite remains unavailable, the durable group may still show a nonterminal state and the database lease remains present. This is intentionally safer than guessing which writes committed.

The writer is a daemon thread and shutdown joins are bounded. `cancel()` returns false if it cannot confirm finalization by its deadline; callers must treat that as unresolved, not as success. A blocked SQLite call can still outlive the caller's deadline until SQLite returns or the process exits. If the thread later resumes, it may finish the already queued interrupted finalization. A process exit during the stall leaves the lease and nonterminal group for review.

## Lease semantics and recovery limit

Lease acquisition uses a SQLite `BEGIN IMMEDIATE` transaction and the schema-v3 singleton lease row, so two grouped coordinators using separate connections or processes against the same database cannot both acquire it. Grouped repository writes check the session and owner token inside an immediate write transaction. Lease age is not an ownership test. There is no automatic lease stealing or stale-owner recovery.

Successful group finalization and deletion of the matching lease happen in one transaction. If finalization validation or persistence fails, the transaction rolls back and the lease remains. A later coordinator is refused until an operator investigates and explicitly resolves the stale lease. This pass does not provide a recovery CLI or automatic resume path.

The existing single-camera `ExperimentRecorder` has not been changed and does not acquire this v3 lease. Therefore, the lease is enforced for grouped coordinators and their repository calls, not end-to-end across the existing v2 single-camera recorder. The coordinator is synthetic-only and must not be connected to a production database until a later reviewed stage makes both recording paths participate in one database ownership policy.

## Lifecycle and final status

Coordinator state:

```text
CREATED -> RUNNING -> FINALIZED
                   -> FAILED (writer/persistence/lease invariant failure)
```

Each child stream moves independently:

```text
PENDING -> STARTING -> RECORDING -> STOPPING -> COMPLETED | INTERRUPTED | FAILED
   └──────────────────────────────────────────> INTERRUPTED | FAILED
```

The persisted group remains pending while only one child is active. Once both child rows are recording, the coordinator validates readiness and marks the parent recording. Failure of one child does not stop the healthy sibling. A failed child while its sibling is active degrades the group. A participant that never starts must be explicitly failed or interrupted before finalization.

If shutdown arrives while a child already has a queued stop transition, it reuses that terminal command instead of enqueueing a second transition. Concurrent repeated stop requests are idempotent around the already queued command. A full-duration group with no persisted frame arrivals finalizes as failed (or interrupted for explicit shutdown) rather than being reported complete or leaving a lease behind due to an avoidable completion-validation exception. Finalization requires at least one persisted arrival per child for a completed outcome; read-failure records remain distinct observations and never substitute for frame arrivals.

The coordinator chooses `completed` only when both streams reach their requested duration, both have committed useful frame-arrival samples, both child states are completed, and neither stream has queue loss, persistence loss, sequence gaps, nor integrity flags. The repository repeats these checks transactionally. A healthy early manual stop is represented by interrupted child state; a group with persisted observations but an incomplete child becomes partial. A run with no persisted observations is failed, except an explicit application cancellation may be recorded as interrupted. A read failure is a valid recorded observation, not proof of a sensor frame drop.

## Timing and synchronization semantics

The coordinator receives monotonic host timestamps, establishes one process-local epoch, and stores integer experiment-relative `elapsed_ns` plus legacy-compatible `elapsed_seconds`. Every stream has an independent `sample_index` sequence and first/last arrival bounds. The shared group clock enables host-relative offsets; it does not synchronize camera exposures. A late stream retains its later `stream_recording` event and first arrival time.

The frame sample method comes from that stream's declared timing method. In the current OpenCV method, timestamps identify the host observation immediately after `VideoCapture.read()` returns. `source_timestamp_ns`, `source_sequence`, and `source_clock` remain NULL. The coordinator does not fabricate V4L2 buffer, sensor, exposure, USB transfer, or preview-presentation timestamps. UTC event markers are useful for human chronology but are not subtracted to calculate durations.

## Synthetic persistence measurements

Run from the public checkout with the existing Python environment:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  "$VENV/bin/python" tools/benchmark_grouped_recording_coordinator.py \
  --durations 30,300,1800 --batch-sizes 32,64,128,256 --rate-b 24
```

The benchmark uses temporary, marked synthetic v3 databases, concurrent synthetic producers, bounded queues, and camera-rate event timestamps. It reports accepted/committed/rejected samples, queue high-water marks, database size, traced Python peak memory, process CPU time, group/History/Analysis query times, and integrity checks. It does not measure camera or host capture performance. The database output is deleted with each temporary directory.

The final batch-size comparison at the 30-second-equivalent workload measured approximately 20.3k, 25.1k, 29.4k, and 32.6k persisted events/second for batches 32, 64, 128, and 256. The 128-row default is retained because it already exceeded the modeled 211 events/second by more than two orders of magnitude, matched the existing single-camera recorder's batch convention, and gives each stream service at a smaller batch boundary. The 256-row result is faster synthetically, but does not justify increasing per-turn batching before live persistence latency is measured.

Representative 128-row results at 187 FPS + 24 FPS were:

| Simulated elapsed time | Expected/committed rows | Database size | Persistence throughput | Writer thread CPU | Process CPU | Python traced peak | Result |
|---:|---:|---:|---:|---:|---:|---:|---|
| 30 s | 6,330 / 6,330 | about 3.00 MB | about 29.4k rows/s | 0.113 s | 0.236 s | about 0.42 MB | complete; no queue rejects |
| 5 min | 63,300 / 63,300 | about 29.20 MB | about 27.4k rows/s | 1.254 s | 2.471 s | about 0.43 MB | complete; no queue rejects |
| 30 min | 379,800 / 379,800 | about 174.38 MB | about 26.1k rows/s | 8.145 s | 15.521 s | about 0.43 MB | complete; no queue rejects |

For the 30-minute fixture, the measurements table occupied about 74.16 MB and its measured indexes about 100.05 MB. Full database size was about 459 bytes per persisted event because of table/index pages and SQLite page allocation. Writer CPU is measured with the writer thread's CPU clock; process CPU includes the main and producer threads as well. These figures are synthetic, specific to the test runtime/filesystem, and are not a promise about real-time latency or physical capture. The workload was accelerated; its timestamp values cover 30 simulated minutes while execution took seconds. The benchmark's 512-event producer chunks stayed below the 2,048-per-stream queue bound. It does not prove behavior under a real device, a slow user disk, competing application writers, or system suspend.

A separate 30-second-equivalent 187 + 30 run committed 6,510/6,510 rows at about 28.2k rows/s, used about 0.123 s writer-thread CPU, produced a 3.12 MB database, and rejected zero rows. The full-duration table above uses 187 + 24 events/s.

The injected slow-writer test fills a two-slot queue deterministically, confirms the next item is rejected, then releases the writer. Subsequent attempts are accepted, both streams drain independently, the lost sequence remains visible, and the final group is partial rather than complete. The database-failure test simulates a disk-full-style transaction exception and confirms the writer terminates, incomplete status is best-effort persisted, accepted rows are not falsely retried, and the lease remains held. Separate-process contention tests confirm a second process cannot acquire a lease while the first owner is active. Shutdown-race tests cover cancellation while an already queued stop waits behind a deliberately blocked synthetic write.

## Pass 2B.2 capture-adapter checks

Run the synthetic adapter/performance harness from the public checkout:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  "$VENV/bin/python" tools/benchmark_capture_event_adapter.py \
  --synthetic-only --seconds 30
```

The required acknowledgement stops argument parsing before any directory is created when it is absent. The tool has no database argument, no fallback to the application database, and no capture or OpenCV code path. It creates a new marked v3 SQLite file per scenario under a fresh temporary root and removes it on exit. It simulates 30 elapsed seconds in accelerated time and reports callback time, writer throughput/CPU, queue depth, loss, SQLite integrity, file size, finalization time, and traced Python peak memory. These results describe only this synthetic SQLite path.

The 30-second-equivalent cases cover 100+24, 187+24, and 187+30 synthetic timing events per second. A controlled burst holds the first SQLite write, fills only stream A’s 64-item queue, verifies B remains loss-free, then releases the writer and confirms later A samples are accepted with the original sequence gap retained. Any rejected sample prevents complete status. The burst does not simulate camera performance.

One local run produced these results (database sizes are bytes; rates and timings are synthetic):

| Scenario | Samples committed | Callback p95 | Persisted rows/s | Queue high-water A/B | SQLite size | Writer CPU | Traced peak | Finalize |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 + 24 | 3,720 / 3,720 | 39.1 µs | 17,162 | 413 / 100 | 1,859,584 | 0.094 s | 0.36 MB | 10.4 ms |
| 187 + 24 | 6,330 / 6,330 | 35.4 µs | 17,932 | 454 / 59 | 3,014,656 | 0.145 s | 0.44 MB | 11.4 ms |
| 187 + 30 | 6,510 / 6,510 | 31.1 µs | 18,842 | 442 / 71 | 3,112,960 | 0.141 s | 0.44 MB | 10.9 ms |

Each normal case used a 2,048-event capacity per stream and had no rejections; `integrity_check` returned `ok` and foreign-key checks returned zero violations. Callback p95 was 31–39 µs; the observed maximum varied from 0.15 to 4.12 ms under synthetic host scheduling and is not a real-time bound. In the blocked-writer burst, A rejected 136 samples and reached the configured high-water of 64; B accepted all 10 and reached a high-water of 10. A later sample was accepted after the write gate reopened. Final group status was partial and data completeness was false. These single-run timings are informational and do not establish hardware capture capacity.

## Remaining work and gate

This pass does not add the Grouped Recording UI, a shared lease for single-camera recording, stale-lease recovery, crash-tail classification, periodic durable counters, preview metrics, History grouping, Analysis overlays, media recording, or production migration. The schema-v3 grouped lease protects grouped coordinators, but the existing v2 `ExperimentRecorder` does not participate; the synthetic harness’s private temporary DB is the only supported adapter use. Before production activation, a later reviewed stage must define one ownership policy shared by both recorders, a separately approved real-database migration, asynchronous GUI lifecycle control, and crash/recovery behavior. User-reported Pass 1 validation is not evidence for grouped persistence, hardware synchronization, native V4L2 timestamps, or PS3 Eye 187 FPS dual capture.
