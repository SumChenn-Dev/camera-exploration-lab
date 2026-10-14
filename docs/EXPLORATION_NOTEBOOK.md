# Camera Exploration Lab Exploration Notebook

This is the public, living record of reusable ideas, discoveries, design decisions, and future experiments. It complements the local SQLite history; it does not replace it. Per-run measurements, raw logs, images, serial numbers, and personal notes belong in local data storage and must not be copied here.

## Project boundaries

- **Core Lab:** camera discovery, stable identity, live preview, capture and preview diagnostics, reproducible recording, history, and later analysis and export.
- **Optional experiments:** marker tracking, calibration, motion analysis, digital zoom, and other computer-vision prototypes. Keep algorithm packages and processing metrics behind optional extension boundaries.
- **Future camera adapters:** CHDKPTP and other specialized backends should implement a small adapter contract after native V4L2 capture remains stable.
- **Separate applications:** wardrobe inventory and other domain-specific workflows use their own application and private data store.

The core records capture, preview, and future processing rates as separate measurements. Host-side frame arrival intervals and OpenCV read durations do not establish sensor exposure time or sensor-to-display latency.

## Completed development milestones

The original private development history contains three completed milestones. The public repository starts from a separate root commit and is the canonical checkout for future work; the original history remains preserved locally.

| Milestone | Outcome | Evidence |
| --- | --- | --- |
| Phase 0 — Foundation | SQLite schema, camera discovery model, historical CSV importer, session data model, logging, and camera-independent tests. | Automated tests use synthetic records and temporary databases. |
| Phase 1 — Live Workbench | Preview-first PySide6 workbench, independent capture and preview loops, persistent metric labels, and V4L2 mode/readback display. | Mocked tests and host capture checks with two native USB cameras. |
| Phase 2A — Experiment recording | Timed and manual sessions, notes, bounded streaming timing storage, SQLite summaries, and a basic History view. | Automated tests passed; a completed physical recording was inspected in History. Detailed run values remain in the local database. |

## Confirmed discoveries and design decisions

| Finding or decision | Reason |
| --- | --- |
| Prefer `/dev/v4l/by-id` for stable endpoint identity; retain `/dev/videoN` only as the current open path. | Kernel video numbers can change after reconnects. |
| Keep capture, preview, and processing rates in separate scopes. | Each stage has a different clock, load, and throughput. |
| Use a single latest-frame preview slot and a bounded timing queue with a database writer. | Slow rendering or storage must not create unbounded frame or sample queues or block capture. |
| Preserve requested settings separately from driver-reported and verified settings. | A request is not evidence that the driver accepted the value. |
| Preserve raw source rows alongside normalized historical measurements. | Later analysis needs both the original input and normalized fields. |
| Exclude a recording's first interval when it began before the recording boundary. | That interval does not describe two arrivals within the experiment. |
| Treat local databases and logs as user data. | They can contain camera identity, paths, experiment names, notes, and imported records. |
| Keep computer-vision prototypes optional and wardrobe inventory separate. | These workflows need different dependencies, interfaces, and privacy boundaries. |

## Research backlog

| ID | Idea | Area | Status | Next evidence needed |
| --- | --- | --- | --- | --- |
| CEL-001 | Harden recording across manual stop, disconnect/reconnect, and restart recovery. | Core | Implemented; more physical cases pending | Compare visible status with persisted session state for each case. |
| CEL-002 | Add graphs, comparisons, and annotated exports. | Core | Planned | Rebuild charts from stored measurements while keeping capture and preview series separate. |
| CEL-003 | Add capability-driven exposure and gain controls with readback. | Core | Planned | Probe supported ranges and verify safe failure and restart behavior per device. |
| CEL-004 | Compare camera modes and controlled environmental conditions. | Research | Proposed | Repeat runs with recorded lighting, exposure, focus, and lens position. |
| CEL-005 | Add a bounded optional processing-stage prototype. | Extension | Proposed | Show that sampling and analysis do not slow capture; report processing FPS separately. |
| CEL-006 | Track markers and analyze motion. | Extension | Proposed | Measure detection confidence, coordinates, processing time, and loss behavior against a known target. |
| CEL-007 | Calibrate two cameras and evaluate correspondence or stereo geometry. | Extension | Proposed | Record calibration quality and alignment uncertainty; start with static targets. |
| CEL-008 | Explore digital zoom or cropping during motion analysis. | Extension | Proposed | Compare source pixels, crop scale, tracking stability, and CPU cost. |
| CEL-009 | Map marker or hand positions to a known screen plane; later evaluate deliberate gestures. | Extension | Proposed | Measure mapping error first and define explicit activation and stop controls. |
| CEL-010 | Add a CHDKPTP adapter and optical zoom experiments. | Optional backend | Deferred | Define mockable process boundaries, timeouts, and bounded camera movement first. |
| CEL-011 | Create a private wardrobe catalog and outfit-planning application. | Separate application | Idea | Define its own data model, storage, and image review flow. |
| CEL-012 | Derive controlled experiment conclusions from repeatable evidence. | Research practice | Proposed | Define what result, uncertainty, and conditions support each conclusion. |
| CEL-013 | Record concurrent multi-camera experiments with per-camera timing and failure state. | Core (future) | Planned | Validate independent stream delivery, per-camera configuration, host-arrival timing, and interruption behavior; do not imply hardware synchronization. |

## Simultaneous V4L2 capture observations

### 2026-10-13 — PS3 Eye and AVerMedia concurrent delivery

- Kind: observation
- Context: both USB cameras captured concurrently on the Bazzite host through V4L2.
- Finding or choice: PS3 Eye at 640×480 / 60 FPS with AVerMedia CAM 313 at 720p MJPEG delivered average capture rates of 60.02 FPS and 23.91 FPS respectively; this configuration was repeated twice. PS3 Eye at 320×240 / 187 FPS with the AVerMedia at 1080p MJPEG delivered average capture rates of 187.35 FPS and 23.91 FPS respectively.
- Evidence: physical concurrent-capture tests. No measurable average-FPS degradation was observed in the tested modes.
- Limitations: these tests confirmed simultaneous frame delivery only. They did not measure cross-camera synchronization, stereo-tracking accuracy, or absolute sensor latency.
- Follow-up: add future multi-camera experiment recording with separate per-camera settings, host arrival timestamps, and failure reporting. This is roadmap work; no multi-camera recorder is implemented.

## Multi-camera experiment outline

1. Characterize each camera independently: image format, field of view, supported controls, readback, and stable capture.
2. Detect a printed or colored marker in individual frames. Log coordinates, confidence, processing time, and failures in the optional experiment layer.
3. Calibrate intrinsics and distortion with a checkerboard or Charuco target, then estimate the relative camera pose.
4. Compare matching observations and test triangulation first with a stationary target, then with slow movement. Report spatial error and timestamp uncertainty.
5. Prototype a known-plane 2D interaction before any 3D gesture workflow. Make activation and stop controls explicit.

The cameras are not hardware-synchronized. A paired observation rate is limited by the slower stream, and host arrival timestamps are not sensor exposure timestamps. Use non-biometric markers or printed identifiers by default.

Screen-interaction research should begin with 2D mapping from a known plane. Add 3D hovering or gesture interpretation only after stereo geometry and error are understood. Camera tracking results must not be presented as reliable identity or authentication.

## Separate wardrobe application concept

A private-first wardrobe catalog could support garment photos, editable categories and attributes, search, outfit combinations, and an optional wear log. A useful first flow is capture or import → review/crop → suggest labels → user correction → local catalog. Photos, purchase details, and wear history belong in the separate app's own private storage, not in Lab code or fixtures.

## Privacy and publication decisions

- The public snapshot excludes experiment databases, raw imports and exports, captured media, ignored data, and the original local commit history.
- The published license is MIT with `SumChenn-Dev` as the copyright holder; see [LICENSE](../LICENSE).
- Automated scanning is a useful filter, not a privacy guarantee. Review every proposed file, line of history, fixture, and generated manifest before any upload.
- The first public snapshot is published. Each later push requires review of the exact outgoing diff, commit identity, destination, and commit hash, followed by explicit owner approval.
- A consequential experiment should record an actionable conclusion only when its setup, evidence, and limitations support that conclusion.

### 2026-10-13 — Public checkout and local data boundary

- Kind: decision
- Context: the first standalone public snapshot is published and the public checkout is the canonical development location.
- Finding or choice: retain the original private checkout and its history; keep experiment databases and backups ignored and local; pass the existing database path explicitly when launching the app.
- Evidence: Bazzite host verified the public remote at the initial commit, editable-install source, dependency health, KDE launcher, both webcam previews, and existing History records. The original private checkout and database remain in place.
- Follow-up: keep publication audits local and review every outgoing diff, commit identity, and destination before a separately authorized push.

## Open questions

- Which experiment fields are needed for reproducibility without making session setup burdensome?
- What small extension interface provides bounded frame access without letting optional processing block capture?
- Which graph and export formats best preserve raw timestamps, units, and missing measurements?
- What is the smallest reliable adapter contract for a future CHDKPTP backend?
- Which physical recovery cases should be supported as automatic interrupted-session recovery versus explicit user-visible failure?

## Notebook entry template

Add concise entries as decisions are made or discoveries are confirmed:

```text
### YYYY-MM-DD — Short topic

- Kind: idea / observation / decision / follow-up
- Context:
- Finding or choice:
- Evidence: synthetic test / physical experiment / source review
- Follow-up:
```

Update an existing `CEL-###` item when possible. Mark proposed work clearly, preserve prior decisions when superseded, and keep run-level data in the local database rather than this document.
