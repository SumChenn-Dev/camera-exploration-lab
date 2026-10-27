# CEL-014 Phase 2 — Canon CHDKPTP Telemetry Bridge

## Scope and ownership boundary

Phase 2 adds a CEL-side consumer for scalar telemetry exported by a separate
CHDKPTP process. The process that already owns the camera remains the only
USB/PTP owner. CEL does not discover, open, poll, configure, or control a Canon
camera in this path. There is no CHDKPTP source code or camera connection in
the mock producer.

The consumer is started explicitly from the Performance Monitor's **Live Canon
CHDKPTP** mode. It runs one listener thread and accepts one local producer
connection at a time. A producer may identify up to eight source streams. The
listener publishes immutable scalar `TelemetrySample` objects into a separate
bounded `BoundedTelemetryBuffer` (default 4,096 entries). The UI polls that
ring at 10 Hz; it does not receive one Qt signal per telemetry event, read
images, or write samples to SQLite. On overflow, the ring overwrites oldest
monitor samples and exposes the overwrite/cursor gap; camera acquisition is
never paused by CEL.

The listener thread uses bounded socket timeouts. Stop requests are
nonblocking to the GUI; the thread performs socket and endpoint cleanup. Its
owner-only socket is placed under `$XDG_RUNTIME_DIR/camera-exploration-lab/`
when available. A per-user private runtime directory under the system temp
directory is the fallback. The containing directory must be owned by the user
and private, and the socket is chmod 0600. A pre-existing path is inspected with
`lstat`; symlinks and non-socket entries are never removed. For an existing
socket, CEL makes a short local connection probe and only removes it after a
connection-refused result, current-user ownership, and a second check that the
same socket inode remains at that path. Active, inaccessible, ambiguous, or changed endpoints are left
untouched and startup fails safely. Listener cleanup likewise unlinks only the
socket inode created by that listener. A persistent sibling `.lock` file is
opened without following symlinks and protected by a nonblocking `flock` while
the listener owns the endpoint. It serializes concurrent startups across
processes that use this lock; older CEL builds and unrelated programs do not
participate. The file is intentionally not removed, so lock release does not
depend on cleanup after a crash. An active lock, unsafe lock-file type, or
insecure permissions causes startup to fail closed.

The source selector keeps full labels available in a tooltip. The Canon chart
range selector shows the last 30 seconds, last 60 seconds, or the full retained
session. It changes only the plotted viewport; it does not discard or rewrite
the bounded telemetry history. A host-receipt interval at or beyond the stale
threshold inserts a plot break, so reconnecting sessions are not drawn as one
continuous line. Full session remains available to inspect disconnected gaps.
Legend labels occupy a layout row outside the plotted data area. Status text
keeps simulation, stale-source, transport, and loss counters visible; detailed
clock and timing definitions remain available as a tooltip.

## Protocol v1

Transport is an AF_UNIX `SOCK_STREAM` with one UTF-8 JSON object per line. Each
line is limited to 16 KiB; each measurement contains at most 32 readings.
Messages carry protocol/version, producer and camera IDs, an optional display
label, a simulation marker, a source-local sequence, a source timestamp and a
list of metric readings. Camera IDs must be stable model identifiers, not USB
serial numbers or personal labels.

Example measurement:

```json
{
  "protocol": "cel.canon.telemetry",
  "version": 1,
  "type": "measurement",
  "producer_id": "chdkptp-owner",
  "camera_id": "canon-sx40-hs",
  "camera_label": "Canon PowerShot SX40 HS",
  "simulated": false,
  "sequence": 42,
  "timestamp": {"clock_domain": "source_monotonic", "value_ns": 1234567890123},
  "readings": [
    {
      "metric": "chdkptp_payload_fetch_fps",
      "value": 31.6,
      "unit": "completed CHDKPTP payload fetches/s",
      "availability": "available",
      "kind": "derived",
      "provenance": "Completed payload fetches divided by the producer's monotonic window."
    },
    {
      "metric": "chdkptp_fetch_call_duration_ms",
      "value": 29.9,
      "unit": "ms/fetch call",
      "availability": "available",
      "kind": "observed",
      "provenance": "CHDKPTP host-monotonic duration around the payload fetch call."
    }
  ]
}
```

For source elapsed time instead of a source monotonic counter, use
`"timestamp": {"clock_domain":"source_elapsed", "elapsed_s": 12.5}`.
The consumer retains that source time and separately stamps receipt using CEL's
host-monotonic clock. It never subtracts source-process timestamps from CEL
timestamps or assumes separate processes have a synchronized clock epoch. The
live chart uses CEL host receipt time. Within one producer connection, sequence
and source time must be nondecreasing; a reconnect starts a new validation
epoch. Sequence gaps are reported as telemetry-message gaps, not physical
camera frame losses.

Readings use CEL's existing metric identifiers, fixed units, availability
(`available`, `not_recorded`, or `unavailable`), kind (`requested`, `observed`,
`derived`, `estimated`, or `reported`), and provenance. Unknown JSON fields and
unknown metric names are ignored for additive compatibility. A known CEL metric
that is not in the Canon allowlist, malformed values, duplicate metrics, scalar
values above 1,000,000,000, and unsupported protocol versions are rejected and
counted. The scalar ceiling keeps accidental or hostile magnitudes from
collapsing live chart scales; source timestamps and sequence values retain their
separate signed 64-bit bounds. An oversized line closes that producer
connection. Rejected payload contents are not logged.

`camera_status` messages use the same identity, sequence, and timestamp fields
plus a `camera_state` of `connected`, `disconnected`, `reconnecting`, or
`error`. IPC transport state (listening, connected, stale, stopped, or error)
remains separate from the camera state last reported by the producer. A valid
telemetry gap of two seconds marks the transport stale by default; that timeout
uses CEL receipt time, not the producer clock.

## Measurement meanings

| Metric | Meaning and limitation |
| --- | --- |
| Requested FPS | Producer's configured request; not achieved throughput. |
| Completed payload fetches/s | CHDKPTP payload fetch completions per producer window; not unique sensor exposures. |
| CHDKPTP-reported FPS | Separate statistic reported by CHDKPTP; not substituted for payload count. |
| Content changes/s | Optional threshold-based estimate from compared decoded images; not sensor FPS and only reproducible if the producer documents its classifier. |
| Fetch-call duration | Producer-measured host duration around the CHDKPTP fetch call; not USB bus latency or sensor-to-host latency. |
| CEL IPC receive gap | Host-monotonic interval between messages for that source; transport arrival spacing, not camera transfer duration. |
| Fetch/poll attempts, device checks, software skips | Distinct producer counters. Software skips or a CHDKPTP dropped counter are not labeled as sensor frame loss. |
| GUI presentation FPS | Unavailable in this bridge. CHDKPTP does not measure CEL presentation, and CEL does not infer physical display scanout. |
| Sensor exposure time/FPS | Unavailable unless a future adapter obtains a documented exposure measurement. |

Missing readings display as **Not recorded**; explicitly unavailable readings
display as **Unavailable**. The monitor labels mock sources **SIMULATED**.
Historical CSV replay remains a separate mode and is never used to fill live
measurements.

## Mock producer and demonstration

Start CEL and select **Performance Monitor → Live Canon CHDKPTP**, then click
**Listen for CHDKPTP**. In another terminal, run this from the checkout root:
```bash
PYTHONPATH="$PWD/src" python tools/run_mock_chdkptp_telemetry.py \
  --simulate --scenario regular --samples 40
```

Other camera-free scenarios include `repeated`, `interrupted`, `slow`,
`malformed`, and `unsupported`. The producer fabricates values inspired by the
published Canon research for demonstration only. It never opens a device. Its
fetch-rate and duration examples are not a replay of any specific benchmark
row and are not new camera measurements.

## CHDKPTP-side work still required

No genuine live Canon producer is included. A future separately reviewed
CHDKPTP-side exporter must:

1. Run inside or alongside the already-owning CHDKPTP process and reuse its
   existing live-view transfer results; never open a second PTP session.
2. Measure successful payload fetches separately from attempts, requested
   FPS, CHDKPTP statistics, software skips, device-list checks, and optional
   content comparisons.
3. Use a bounded/coalescing telemetry handoff and a writer outside the live
   transfer callback. A blocked CEL consumer must not stall CHDKPTP acquisition.
4. Send source sequence, source clock metadata, units, availability, measurement
   kind, simulation flag, and truthful provenance. Do not send serial numbers
   or raw images.
5. Reconnect without assuming that a new source clock or sequence continues
   the previous connection epoch, and send unavailable values rather than
   fabricating metrics.
6. Receive its own license/source review; CEL does not copy CHDKPTP GPL code.

Phase 2 validates the CEL receiver with fake messages only. No physical Canon,
CHDKPTP installation, camera settings, or live source has been accessed or
changed.

## Replay control correction

Changing datasets creates a stopped replay controller. The dataset-selection
handler now explicitly restores the Play button and disables Pause, regardless
of whether the previous dataset was playing, paused, or at end-of-playback.
Reset, pause/resume, dataset ordering, and the historical replay schedule are
otherwise unchanged.

## Recommended Phase 3 hardware validation

After a separately approved exporter exists, validate on one supervised Canon
session: start the existing CHDKPTP GUI as the sole PTP owner; start CEL's local
listener; check producer identity, status, sequence continuity, stale/reconnect
display, and units; compare a short run's payload count and fetch-call timings
with the owning CHDKPTP process's own counters; then stop export, CEL listener,
and CHDKPTP cleanly. Repeat with listener disconnect/reconnect and a deliberate
slow consumer. Measure exporter overhead using the same monotonic clock around
the transfer callback and confirm the bounded producer handoff does not wait
for IPC. Keep requested FPS, completed fetches, content-change estimates, and
any GUI presentation rate separate. Do not claim sensor synchronization,
exposure FPS, physical frame losses, or USB-only latency from these checks.
