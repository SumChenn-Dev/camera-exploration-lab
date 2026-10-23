"""Validated replay of the six published Canon live-view benchmark CSVs.

Replay rows remain historical observations: the loader preserves each source
field's meaning and never converts them into live-camera measurements.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from math import isfinite
from importlib import resources
from typing import Iterable

from .performance import (
    Availability,
    ClockDomain,
    MeasurementKind,
    Metric,
    TelemetrySample,
    reading,
)


class ReplayFormatError(ValueError):
    """A replay fixture is malformed or does not match its public manifest."""


@dataclass(frozen=True, slots=True)
class ReplayRow:
    sample: TelemetrySample
    status: str
    stage: str | None
    condition: str | None
    replay_elapsed_s: float

    @property
    def elapsed_s(self) -> float:
        return self.sample.source_elapsed_s or 0.0


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    dataset_id: str
    title: str
    filename: str
    description: str
    rows: tuple[ReplayRow, ...]

    @property
    def duration_seconds(self) -> float:
        if len(self.rows) < 2:
            return 0.0
        return self.rows[-1].replay_elapsed_s - self.rows[0].replay_elapsed_s


@dataclass(frozen=True, slots=True)
class ReplayCatalogItem:
    filename: str
    title: str
    description: str


CATALOG = (
    ReplayCatalogItem("01_initial_fps_sweep.csv", "Initial requested-FPS sweep", "CHDKPTP GUI-reported rates and per-fetch timing across requested FPS stages."),
    ReplayCatalogItem("02_dropframe_ab.csv", "Software frame-skip A/B", "Requested rate, CHDKPTP statistics, poll attempts, and software skip counts."),
    ReplayCatalogItem("03_live_pipeline_profile.csv", "Live pipeline profile", "Completed CHDKPTP payload fetches, fetch-call duration, callback gaps, and skip counts."),
    ReplayCatalogItem("04_device_polling_ab.csv", "Device-polling A/B/A", "A single within-session comparison of completed payload fetches with device polling enabled and paused."),
    ReplayCatalogItem("05_smart_polling_fps_sweep.csv", "Smart-polling FPS sweep", "Completed payload fetches and host fetch-call timing during requested-rate stages."),
    ReplayCatalogItem("06_content_freshness.csv", "Image-content freshness", "Completed payload fetches and the published threshold-classified content-change estimate."),
)

_CATALOG_BY_FILENAME = {item.filename: item for item in CATALOG}
_COUNT_METRICS = {
    Metric.CHDKPTP_FETCH_ATTEMPTS,
    Metric.CHDKPTP_POLL_ATTEMPTS,
    Metric.DEVICE_POLL_CHECKS,
    Metric.CHDKPTP_FETCH_COUNT,
    Metric.CONTENT_SAMPLE_COUNT,
    Metric.EXACT_REPEAT_COUNT,
    Metric.SOFTWARE_SKIPS,
    Metric.CHDKPTP_DROPPED_COUNTER,
}


def _number(row: dict[str, str], columns: Iterable[str]) -> tuple[str, float] | None:
    for column in columns:
        if column not in row:
            continue
        cell = row[column]
        if cell is None:
            return None
        raw = cell.strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError as exc:
            raise ReplayFormatError(f"column {column!r} contains a non-numeric value") from exc
        if not isfinite(value):
            raise ReplayFormatError(f"column {column!r} contains a non-finite value")
        return column, value
    return None


def _measurement(
    row: dict[str, str],
    metric: Metric,
    columns: tuple[str, ...],
    *,
    kind: MeasurementKind = MeasurementKind.REPORTED,
    filename: str,
    not_recorded_reason: str | None = None,
):
    found = _number(row, columns)
    if found is None:
        return reading(
            metric,
            None,
            availability=(Availability.NOT_RECORDED if not_recorded_reason else Availability.UNAVAILABLE),
            provenance=(not_recorded_reason or "This historical source cannot establish this measurement."),
        )
    column, value = found
    if metric in _COUNT_METRICS:
        if value < 0 or not value.is_integer():
            raise ReplayFormatError(f"column {column!r} must contain a non-negative whole-number count")
        value = int(value)
    return reading(
        metric,
        value,
        kind=kind,
        provenance=f"Published CSV {filename}, column {column}; see the Canon research README for interpretation.",
    )


def parse_replay_csv(
    filename: str,
    text: str,
    *,
    expected_rows: int | None = None,
) -> ReplayDataset:
    """Parse and validate one published replay CSV without camera access."""
    item = _CATALOG_BY_FILENAME.get(filename)
    if item is None:
        raise ReplayFormatError(f"unrecognized Canon replay dataset: {filename}")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise ReplayFormatError("CSV is missing its header")
        required = {"sample_index", "elapsed_s", "status"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ReplayFormatError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
        rows: list[ReplayRow] = []
        previous_elapsed = -1.0
        previous_stage: str | None = None
        previous_sequence = -1
        replay_elapsed = 0.0
        for line_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise ReplayFormatError(f"line {line_number} has more values than the header")
            try:
                sequence = int(raw["sample_index"])
                elapsed = float(raw["elapsed_s"])
            except (TypeError, ValueError) as exc:
                raise ReplayFormatError(f"line {line_number} has an invalid sample index or elapsed time") from exc
            if sequence < 0 or sequence <= previous_sequence or not isfinite(elapsed) or elapsed < 0:
                raise ReplayFormatError(f"line {line_number} has a negative or non-finite index/time")
            previous_sequence = sequence
            status = (raw.get("status") or "").strip()
            if not status:
                raise ReplayFormatError(f"line {line_number} has no benchmark row status")
            stage = (raw.get("stage") or "").strip() or None
            condition = (raw.get("condition") or "").strip() or None
            if previous_stage is None:
                replay_elapsed = 0.0
            else:
                sample_duration = _number(raw, ("sample_s",))
                if sample_duration is not None:
                    replay_step = sample_duration[1]
                    if replay_step <= 0:
                        raise ReplayFormatError(f"line {line_number} has a non-positive sample duration")
                elif stage == previous_stage:
                    replay_step = elapsed - previous_elapsed
                else:
                    replay_step = elapsed
                if stage == previous_stage and elapsed <= previous_elapsed:
                    raise ReplayFormatError(
                        f"elapsed_s must increase within stage {stage!r} (line {line_number})"
                    )
                if replay_step <= 0:
                    raise ReplayFormatError(f"line {line_number} has no positive replay interval")
                replay_elapsed += replay_step
            previous_elapsed = elapsed
            previous_stage = stage

            available = [
                _measurement(raw, Metric.REQUESTED_FPS, ("target_fps",), kind=MeasurementKind.REQUESTED,
                             filename=filename, not_recorded_reason="No requested-rate column was published."),
                _measurement(raw, Metric.CHDKPTP_GUI_REPORTED_FPS,
                             ("actual_fps", "stats_fps", "rolling_stats_fps"),
                             filename=filename, not_recorded_reason="No CHDKPTP GUI/statistics FPS column was published."),
                _measurement(raw, Metric.CHDKPTP_FETCH_FPS, ("completed_fps", "transfers_fps"),
                             filename=filename, kind=MeasurementKind.DERIVED,
                             not_recorded_reason="Completed payload fetches/s was not recorded in this dataset."),
                _measurement(raw, Metric.CONTENT_CHANGE_RATE, ("changed_per_s",),
                             filename=filename, kind=MeasurementKind.ESTIMATED,
                             not_recorded_reason="A threshold-classified content-change rate was not recorded."),
                _measurement(raw, Metric.CHDKPTP_FETCH_DURATION_MS,
                             ("usb_transfer_ms", "usb_ms", "avg_usb_ms"), filename=filename,
                             not_recorded_reason="A CHDKPTP fetch-call duration was not recorded."),
                _measurement(raw, Metric.CHDKPTP_FETCH_ATTEMPTS, ("fetch_attempts",),
                             filename=filename, not_recorded_reason="Fetch attempts were not recorded."),
                _measurement(raw, Metric.CHDKPTP_POLL_ATTEMPTS, ("poll_attempts",),
                             filename=filename, not_recorded_reason="Poll attempts were not recorded."),
                _measurement(raw, Metric.DEVICE_POLL_CHECKS, ("device_checks",),
                             filename=filename, not_recorded_reason="Device-list checks were not recorded."),
                _measurement(raw, Metric.CHDKPTP_FETCH_COUNT, ("completed_transfers",),
                             filename=filename, not_recorded_reason="Completed payload-fetch counts were not recorded."),
                _measurement(raw, Metric.CHDKPTP_REPORTED_THROUGHPUT_KBPS, ("throughput_kbps",),
                             filename=filename, not_recorded_reason="A throughput value was not recorded."),
                _measurement(raw, Metric.CHDKPTP_REPORTED_USB_SPEED_KBPS, ("usb_speed_kbps",),
                             filename=filename, not_recorded_reason="A reported USB speed value was not recorded."),
                _measurement(raw, Metric.SOFTWARE_SKIPS, ("software_skips",),
                             filename=filename, not_recorded_reason="Software skip counts were not recorded."),
                _measurement(raw, Metric.CHDKPTP_DROPPED_COUNTER, ("dropped_total",),
                             filename=filename, not_recorded_reason="A CHDKPTP dropped counter was not recorded."),
                _measurement(raw, Metric.CONTENT_SAMPLE_COUNT, ("content_samples",),
                             filename=filename, not_recorded_reason="Content comparison sample counts were not recorded."),
                _measurement(raw, Metric.EXACT_REPEAT_COUNT, ("exact_repeats",),
                             filename=filename, not_recorded_reason="Exact repeated-content counts were not recorded."),
            ]
            # These facts are never recoverable from these sanitized CSVs.
            unavailable = [
                reading(Metric.OPENCV_CAPTURE_FPS, None, availability=Availability.UNAVAILABLE,
                        provenance="This historical CHDKPTP CSV does not contain CEL OpenCV/V4L2 capture events."),
                reading(Metric.GUI_PREVIEW_UPDATE_FPS, None, availability=Availability.UNAVAILABLE,
                        provenance="Historical CHDKPTP GUI FPS is not a CEL preview-update measurement."),
                reading(Metric.GUI_PRESENTATION_FPS, None, availability=Availability.UNAVAILABLE,
                        provenance="Physical display presentation cadence was not measured."),
                reading(Metric.SENSOR_EXPOSURE_FPS, None, availability=Availability.UNAVAILABLE,
                        provenance="Payload fetches and content changes do not establish sensor exposure cadence."),
                reading(Metric.OPENCV_READ_CALL_DURATION_MS, None, availability=Availability.UNAVAILABLE,
                        provenance="This dataset has no OpenCV/V4L2 read call."),
                reading(Metric.FRAME_INTERVAL_MS, None, availability=Availability.UNAVAILABLE,
                        provenance="This dataset has no per-frame host read-return timestamps."),
                reading(Metric.CONNECTION_STATE, None, availability=Availability.UNAVAILABLE,
                        provenance="The CSV row status is a benchmark status, not a camera connection state."),
            ]
            readings = tuple(available + unavailable)
            sample = TelemetrySample(
                timestamp_ns=round(elapsed * 1_000_000_000),
                source=f"Canon SX40 HS historical CSV · {filename}",
                clock_domain=ClockDomain.SOURCE_ELAPSED,
                readings=readings,
                sequence=sequence,
                source_elapsed_s=elapsed,
            )
            rows.append(ReplayRow(
                sample=sample,
                status=status,
                stage=stage,
                condition=condition,
                replay_elapsed_s=replay_elapsed,
            ))
    except csv.Error as exc:
        raise ReplayFormatError(f"invalid CSV: {exc}") from exc

    if expected_rows is not None and len(rows) != expected_rows:
        raise ReplayFormatError(f"expected {expected_rows} data rows, found {len(rows)}")
    if not rows:
        raise ReplayFormatError("CSV contains no data rows")
    return ReplayDataset(filename, item.title, filename, item.description, tuple(rows))


def load_builtin_replays() -> tuple[ReplayDataset, ...]:
    """Load packaged, hash-checked copies of the six published research CSVs."""
    data_root = resources.files("camera_exploration_lab").joinpath("replay_data", "canon_live_view")
    try:
        manifest = json.loads(data_root.joinpath("manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayFormatError("packaged Canon replay manifest is unavailable or invalid") from exc
    if not isinstance(manifest, list):
        raise ReplayFormatError("packaged Canon replay manifest must be a list")
    datasets: list[ReplayDataset] = []
    for item in manifest:
        if not isinstance(item, dict):
            raise ReplayFormatError("manifest entry must be an object")
        filename = item.get("public_file")
        if not isinstance(filename, str) or filename not in _CATALOG_BY_FILENAME:
            raise ReplayFormatError("manifest names an unknown replay dataset")
        expected_digest = item.get("public_sha256")
        expected_rows = item.get("rows")
        try:
            raw = data_root.joinpath(filename).read_bytes()
        except OSError as exc:
            raise ReplayFormatError(f"packaged replay data is missing: {filename}") from exc
        if not isinstance(expected_digest, str) or sha256(raw).hexdigest() != expected_digest:
            raise ReplayFormatError(f"packaged replay hash does not match the public manifest: {filename}")
        if not isinstance(expected_rows, int) or isinstance(expected_rows, bool):
            raise ReplayFormatError(f"manifest row count is invalid for {filename}")
        datasets.append(parse_replay_csv(filename, raw.decode("utf-8-sig"), expected_rows=expected_rows))
    if {dataset.filename for dataset in datasets} != set(_CATALOG_BY_FILENAME):
        raise ReplayFormatError("packaged replay manifest must include each of the six public datasets exactly once")
    if len(datasets) != len(_CATALOG_BY_FILENAME):
        raise ReplayFormatError("packaged replay manifest contains duplicate datasets")
    return tuple(datasets)


class ReplayController:
    """Deterministic pause/resume/reset timeline driven by an injected clock."""

    def __init__(self, dataset: ReplayDataset) -> None:
        self.dataset = dataset
        self._timeline = tuple(row.replay_elapsed_s - dataset.rows[0].replay_elapsed_s for row in dataset.rows)
        self._index = 0
        self._position_s = 0.0
        self._anchor_ns = 0
        self._anchor_position_s = 0.0
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def index(self) -> int:
        return self._index

    @property
    def position_seconds(self) -> float:
        return self._position_s

    @property
    def current(self) -> ReplayRow:
        return self.dataset.rows[self._index]

    def play(self, now_ns: int) -> None:
        if not self.dataset.rows or self._playing:
            return
        if self._index == len(self.dataset.rows) - 1:
            self.reset()
        self._anchor_ns = now_ns
        self._anchor_position_s = self._position_s
        self._playing = True

    def pause(self, now_ns: int) -> ReplayRow:
        row = self.advance(now_ns)
        self._playing = False
        return row

    def reset(self) -> ReplayRow:
        self._index = 0
        self._position_s = 0.0
        self._anchor_position_s = 0.0
        self._anchor_ns = 0
        self._playing = False
        return self.current

    def advance(self, now_ns: int) -> ReplayRow:
        if not self._playing:
            return self.current
        if now_ns < self._anchor_ns:
            raise ValueError("replay clock cannot move backwards")
        desired = self._anchor_position_s + (now_ns - self._anchor_ns) / 1_000_000_000
        self._index = min(len(self._timeline) - 1, max(0, bisect.bisect_right(self._timeline, desired) - 1))
        self._position_s = self._timeline[self._index]
        if self._index == len(self._timeline) - 1:
            self._playing = False
        return self.current
