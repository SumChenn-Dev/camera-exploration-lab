# CEL-013 Pass 2C.1 — Synthetic UI Orchestration Boundary

## Scope

This is a development-only Qt harness for the existing grouped recording coordinator and capture-event bridge. The ordinary application launch, `MultiCameraWorkspace`, `Database.initialize()`, and the user's schema-v2 history path remain unchanged. The harness accepts no database path and cannot discover or open a camera.

## Ownership and event flow

```text
GUI thread
  └── Synthetic recording window and controls
       └── queued lifecycle request (low rate)
            ↓
Dedicated Qt orchestration thread
  ├── fresh temporary directory + explicitly marked synthetic v3 database
  ├── GroupedRecordingRepository / Coordinator / CaptureRecordingBridge
  ├── bounded coordinator queues + single SQLite writer (existing implementation)
  └── two fake timing-only capture producers
       └── direct, nonblocking CaptureTimingEvent callback per sample
            ↓
       existing CaptureEventAdapter → existing GroupedRecordingCoordinator

GUI thread ← throttled snapshots and lifecycle acknowledgements only
```

The GUI shows an action as requested while the orchestration thread is working. It displays Recording only after the bridge has attached both running fake sources and the coordinator has acknowledged their stream-start events. It displays Completed only after the coordinator returns a terminal record following the durable finalization transaction. Per-frame timing never travels through Qt signals or widget callbacks.

## Lifecycle decisions

- Start Both creates two fake timing producers. They generate only counters and host-monotonic `CaptureTimingEvent` metadata; no video frame or image is created.
- Record Both creates a new `TemporaryDirectory`, initializes a new empty schema-v2 database, sets the synthetic-only SQLite application marker, explicitly invokes the existing synthetic v3 migration, then creates the grouped coordinator and attaches the already-running sources.
- Stop Recording detaches sample intake and finalizes the group on the orchestration thread. A user-requested early stop leaves the fake streams active, modeling preview continuing while timing recording stops.
- The synthetic harness offers **Keep synthetic capture running after recording finishes**, unchecked by default. At natural fixed-duration completion, the worker waits for the coordinator's committed terminal result, detaches recording intake, requests both fake producers to stop, and polls their termination without blocking the GUI. Only after the committed result is acknowledged may automatic source shutdown begin. Capture counters then remain fixed at their final totals, while committed timing counts remain tied to the database rows.
- When the option is checked, fixed-duration recording still finalizes and detaches sample intake, but the two synthetic producers keep running. Their live counters may continue increasing; the finalized recording counts and database rows do not. Manual early Stop Recording continues to leave capture running regardless of this option. Stop Both and window close retain their existing explicit shutdown behavior.
- If finalization is unconfirmed, automatic shutdown is not reported as a successful recording completion. If the database has a committed partial or failed terminal result, the harness reports that status honestly while stopping the synthetic sources. Source termination is asynchronous with bounded polling on the orchestration thread, never a GUI-thread wait.
- Stop A or Stop B stops that source and records only its terminal stream event; its sibling remains active.
- Stop Both finalizes an active recording first, then stops both fake sources. Close follows a bounded cancellation path and never labels an unacknowledged finalization successful.
- Temporary artifacts are removed only after fake sources have stopped and the coordinator writer thread has closed. If shutdown cannot establish that condition, the harness stays open and reports unresolved finalization.

## Qt thread and backpressure rules

The window/controller remains on the QApplication thread. A worker `QObject` moved to one owned `QThread` performs all database initialization, migration, coordinator lifecycle, reads, draining, and cleanup. Requests and acknowledgements use queued Qt signals. The worker emits a coalesced status snapshot on a modest timer; no signal is emitted for every frame. Fake producer threads invoke the adapter callback directly, which performs the bridge's existing bounded `put_nowait` handoff and no SQLite work.

The window does not call `QThread.wait()` or coordinator stop/finalize methods. A close request is deferred until fake producers and the coordinator writer have stopped. If they are still draining beyond the warning deadline, the window reports unresolved finalization and keeps polling without closing; it does not assert success. If threads have ended but finalization is unconfirmed, the window warns and retains the disposable synthetic database for inspection.

## Isolation gate

The standalone tool requires both `--synthetic-only` and `--acknowledge-synthetic-only`, defines no path argument, creates its database only beneath a newly allocated private temporary directory, and never imports V4L2 discovery or OpenCV capture construction. The normal `celab gui` command and KDE launcher are not changed. The UI is an explicit fake-camera demonstration, not a real-camera Record Both feature.
