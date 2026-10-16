"""Read-only, bounded-memory history and analysis for schema-v3 groups.

This module is used by the synthetic development explorer only. It never
migrates, updates, finalizes, or repairs a grouped experiment.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .analysis import (
    DEFAULT_MAX_PLOT_POINTS,
    HISTOGRAM_BINS,
    HISTOGRAM_MAX_MS,
    HISTOGRAM_MIN_MS,
    Histogram,
    MetricStats,
)
from .db import Database
from .grouped_recording import (
    ExperimentEventRecord,
    GroupedExperimentRecord,
    GroupedExperimentRepository,
    GroupedStreamRecord,
)
from .recording_ownership import RecoveryClassification


@dataclass(frozen=True, slots=True)
class GroupedStreamAnalysis:
    """One stream's persisted measurements, provenance, and bounded plots."""

    stream: GroupedStreamRecord
    persisted_frame_samples: int
    persisted_read_failure_samples: int
    last_sequence_index: int | None
    observed_sequence_gap_count: int
    interval_stats: MetricStats
    interval_stddev_ms: float | None
    interval_histogram: Histogram
    requested_capture_mode: str | None
    driver_reported_capture_mode: str | None
    requested_fps: float | None
    driver_reported_fps: float | None
    observed_capture_fps: float | None
    preview_fps: float | None
    processing_fps: float | None
    recording_start_elapsed_ns: int | None
    recording_end_elapsed_ns: int | None
    recording_duration_seconds: float | None
    first_arrival_elapsed_ns: int | None
    last_arrival_elapsed_ns: int | None
    missing_timestamp_sample_count: int
    zero_interval_count: int
    negative_interval_count: int
    boundary_interval_count: int
    interval_mismatch_count: int
    fps_points_shared: tuple[tuple[float, float], ...]
    fps_points_stream: tuple[tuple[float, float], ...]
    interval_points_shared: tuple[tuple[float, float], ...]
    interval_points_stream: tuple[tuple[float, float], ...]
    warnings: tuple[str, ...]

    @property
    def persisted_timing_sample_count(self) -> int:
        return self.persisted_frame_samples + self.persisted_read_failure_samples

    @property
    def effective_data_complete(self) -> bool:
        return (
            self.stream.data_complete
            and not self.stream.integrity_flags
            and self.stream.timing_queue_loss_count == 0
            and self.stream.persistence_loss_count == 0
            and self.stream.sequence_gap_count == 0
            and self.observed_sequence_gap_count == 0
            and self.interval_mismatch_count == 0
            and self.missing_timestamp_sample_count == 0
        )


@dataclass(frozen=True, slots=True)
class GroupedExperimentAnalysis:
    """Immutable one-snapshot view model for grouped History and Analysis."""

    group: GroupedExperimentRecord
    streams: tuple[GroupedStreamAnalysis, GroupedStreamAnalysis]
    recovery_classification: str | None
    shared_time_available: bool
    shared_time_reason: str | None
    shared_start_offset_seconds: float | None
    warnings: tuple[str, ...]

    def stream(self, slot: str) -> GroupedStreamAnalysis:
        if slot not in {"A", "B"}:
            raise ValueError("slot must be A or B")
        return self.streams[0] if self.streams[0].stream.slot == slot else self.streams[1]

    @property
    def display_status(self) -> str:
        if self.recovery_classification == RecoveryClassification.INDETERMINATE_AFTER_CRASH:
            return "Indeterminate after crash"
        status = self.group.status.replace("_", " ").capitalize()
        if self.group.status == "pending" and all(item.persisted_timing_sample_count == 0 for item in self.streams):
            return "Empty · not started"
        if self.group.status in {"partial", "interrupted", "failed"}:
            if self.recovery_classification == RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS:
                return f"{status} · timing data loss"
            return f"{status} · incomplete data"
        if self.group.status == "degraded":
            if self.recovery_classification == RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS:
                return "Degraded · timing data loss"
            return "Degraded · incomplete dataset"
        if self.group.status == "recording" and self.recovery_classification in {
            RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS,
            RecoveryClassification.INCOMPLETE_DATASET,
        }:
            return "Recording · timing data incomplete"
        if self.recovery_classification == RecoveryClassification.INCOMPLETE_PERSISTENCE_LOSS:
            return "Incomplete · timing data loss"
        if self.recovery_classification == RecoveryClassification.INCOMPLETE_DATASET:
            return "Incomplete dataset"
        if self.group.status == "completed" and not all(item.effective_data_complete for item in self.streams):
            return "Completed · integrity warning"
        return status


def load_grouped_analysis(
    database: Database,
    session_id: str,
    *,
    max_plot_points: int = DEFAULT_MAX_PLOT_POINTS,
) -> GroupedExperimentAnalysis:
    """Load a grouped experiment from one query-only SQLite snapshot.

    Measurement iteration is streaming. Full-data statistics are accumulated
    before plotting; only extrema-preserving, bounded plot points are returned.
    """
    if max_plot_points < 4:
        raise ValueError("max_plot_points must be at least 4")
    repository = GroupedExperimentRepository(database)
    with database.connect_readonly() as connection:
        group = repository.read_group_snapshot(connection, session_id)
        if group is None:
            raise ValueError("The selected synthetic grouped experiment was not found.")
        shared_plot_duration = _shared_plot_duration(group)
        streams = tuple(
            _load_stream(
                connection,
                stream,
                max_plot_points=max_plot_points,
                shared_plot_duration=shared_plot_duration,
            )
            for stream in group.streams
        )

    recovery_classification: str | None = None
    recovery_warning: str | None = None
    try:
        recovery = database.inspect_recording_recovery()
        finding = next((item for item in recovery.findings if item.session_id == session_id), None)
        if finding is not None:
            recovery_classification = str(finding.classification)
    except Exception:
        recovery_warning = "Read-only recovery status could not be confirmed."

    common, common_reason = _shared_time_supports(group, streams)
    offset = None
    if common:
        # Prefer persisted stream boundary markers, falling back to the first
        # actual persisted arrival only when provenance still supports the
        # shared monotonic clock domain.
        a_first = streams[0].first_arrival_elapsed_ns
        b_first = streams[1].first_arrival_elapsed_ns
        if a_first is not None and b_first is not None:
            offset = (b_first - a_first) / 1_000_000_000

    warnings: list[str] = []
    if group.status in {"recording", "degraded"} and recovery_classification == RecoveryClassification.INDETERMINATE_AFTER_CRASH:
        warnings.append("The database has an unresolved recording state; no recovery action was taken.")
    if not group.data_complete:
        warnings.append("The grouped dataset is marked incomplete; surviving samples are shown without gap filling.")
    for stream in streams:
        warnings.extend(f"Stream {stream.stream.slot}: {message}" for message in stream.warnings)
    if recovery_warning:
        warnings.append(recovery_warning)
    if not common_reason and common:
        warnings.append("Shared time uses one experiment monotonic clock domain; it does not imply synchronized exposure.")
    return GroupedExperimentAnalysis(
        group=group,
        streams=(streams[0], streams[1]),
        recovery_classification=recovery_classification,
        shared_time_available=common,
        shared_time_reason=common_reason,
        shared_start_offset_seconds=offset,
        warnings=tuple(dict.fromkeys(warnings)),
    )


@dataclass(slots=True)
class _BucketRate:
    count: int = 0
    duration_ns: int = 0
    x_sum: float = 0.0


@dataclass(slots=True)
class _BucketExtrema:
    low: tuple[float, float] | None = None
    high: tuple[float, float] | None = None

    def add(self, point: tuple[float, float]) -> None:
        if self.low is None or point[1] < self.low[1]:
            self.low = point
        if self.high is None or point[1] > self.high[1]:
            self.high = point


def _load_stream(
    connection: sqlite3.Connection,
    stream: GroupedStreamRecord,
    *,
    max_plot_points: int,
    shared_plot_duration: float,
) -> GroupedStreamAnalysis:
    benchmark_id = stream.benchmark_id
    count_row = connection.execute(
        """SELECT COUNT(*) AS frame_count, MAX(sample_index) AS last_index
           FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'""",
        (benchmark_id,),
    ).fetchone()
    persisted_frames = int(count_row["frame_count"] or 0)
    last_index = int(count_row["last_index"]) if count_row["last_index"] is not None else None
    persisted_failures = int(connection.execute(
        """SELECT COUNT(*) FROM measurements
           WHERE benchmark_id=? AND metric='read_failure' AND stream_scope='capture'""",
        (benchmark_id,),
    ).fetchone()[0])

    rate_summary = _summary_value(stream.summary, "capture_fps")
    preview_fps = _summary_value(stream.summary, "preview_fps")
    processing_fps = _summary_value(stream.summary, "processing_fps")
    requested_fps = _configuration_fps(stream.configuration, "requested_fps")
    if requested_fps is None:
        requested_fps = _setting_fps(stream.settings, "requested_value")
    driver_fps = _setting_fps(stream.settings, "reported_value")
    if driver_fps is None:
        driver_fps = _setting_fps(stream.settings, "verified_value")
    requested_mode = _capture_mode_text(
        stream.configuration,
        stream.settings,
        configuration_keys=("requested", "requested_configuration"),
        setting_attribute="requested_value",
        include_top_level=True,
    )
    driver_mode = _capture_mode_text(
        stream.configuration,
        stream.settings,
        configuration_keys=("negotiated", "negotiated_configuration", "driver_reported"),
        setting_attribute="verified_value",
        include_top_level=False,
    ) or _capture_mode_text(
        stream.configuration,
        stream.settings,
        configuration_keys=("negotiated", "negotiated_configuration", "driver_reported"),
        setting_attribute="reported_value",
        include_top_level=False,
    )

    # A first aggregate bounds the point decimators before a streaming pass.
    bounds = connection.execute(
        """SELECT MIN(elapsed_ns) AS min_ns, MAX(elapsed_ns) AS max_ns,
                  COUNT(elapsed_ns) AS timestamp_count,
                  MIN(elapsed_seconds) AS min_seconds, MAX(elapsed_seconds) AS max_seconds,
                  COUNT(elapsed_seconds) AS seconds_count
           FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'""",
        (benchmark_id,),
    ).fetchone()
    timestamp_count = int(bounds["timestamp_count"] or 0)
    seconds_count = int(bounds["seconds_count"] or 0)
    use_ns = persisted_frames > 0 and timestamp_count == persisted_frames
    use_seconds = persisted_frames > 0 and seconds_count == persisted_frames
    first_row = connection.execute(
        """SELECT elapsed_ns, elapsed_seconds FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
           ORDER BY sample_index IS NULL, sample_index, id LIMIT 1""",
        (benchmark_id,),
    ).fetchone()
    timestamp_origin_ns = (
        int(first_row["elapsed_ns"])
        if use_ns and first_row is not None and first_row["elapsed_ns"] is not None
        else None
    )
    timestamp_span_s = (
        max(0.0, (int(bounds["max_ns"]) - int(bounds["min_ns"])) / 1e9)
        if use_ns else 0.0
    )
    timestamp_origin_s = (
        float(first_row["elapsed_seconds"])
        if use_seconds and first_row is not None and first_row["elapsed_seconds"] is not None
        else None
    )
    timestamp_span_s_fallback = (
        max(0.0, float(bounds["max_seconds"]) - float(bounds["min_seconds"]))
        if use_seconds else 0.0
    )
    span_seconds = timestamp_span_s if use_ns else timestamp_span_s_fallback
    stream_window_seconds = max(1.0, span_seconds / max_plot_points) if span_seconds else 1.0
    shared_window_seconds = max(
        1.0, shared_plot_duration / max_plot_points
    ) if shared_plot_duration else 1.0
    rate_bins_shared: dict[int, _BucketRate] = {}
    rate_bins_stream: dict[int, _BucketRate] = {}
    interval_bins_shared: dict[int, _BucketExtrema] = {}
    interval_bins_stream: dict[int, _BucketExtrema] = {}
    interval_bucket_budget = max(1, min(max_plot_points // 2, max(1, persisted_frames - 1)))
    shared_interval_bin_count = interval_bucket_budget if shared_plot_duration else 1
    stream_interval_bin_count = interval_bucket_budget if span_seconds else 1
    shared_interval_bin_width = max(
        1e-12,
        shared_plot_duration / max(1, shared_interval_bin_count - 1)
        if shared_plot_duration else 1.0,
    )
    stream_interval_bin_width = max(
        1e-12,
        span_seconds / max(1, stream_interval_bin_count - 1)
        if span_seconds else 1.0,
    )
    histogram_counts = [0] * HISTOGRAM_BINS
    positive_count = zero_count = underflow_count = overflow_count = 0
    interval_count = 0
    interval_sum_ns = 0
    interval_mean_ns = 0.0
    interval_m2_ns = 0.0
    interval_min_ns: int | None = None
    interval_max_ns: int | None = None
    sequence_gaps = 0
    zero_intervals = negative_intervals = boundary_intervals = mismatches = 0
    missing_timestamps = persisted_frames - timestamp_count
    first_elapsed_ns: int | None = None
    last_elapsed_ns: int | None = None
    first_elapsed_seconds: float | None = None
    last_elapsed_seconds: float | None = None
    previous_index: int | None = None
    previous_elapsed_ns: int | None = None
    previous_elapsed_seconds: float | None = None

    cursor = connection.execute(
        """SELECT sample_index, elapsed_ns, elapsed_seconds, interval_ns
           FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
           ORDER BY sample_index IS NULL, sample_index, id""",
        (benchmark_id,),
    )
    processed = 0
    for row in cursor:
        processed += 1
        index = int(row["sample_index"]) if row["sample_index"] is not None else None
        elapsed_ns = int(row["elapsed_ns"]) if row["elapsed_ns"] is not None else None
        elapsed_seconds = float(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else None
        if processed == 1:
            if index is not None and index > 0:
                sequence_gaps += index
            first_elapsed_ns = elapsed_ns
            first_elapsed_seconds = elapsed_seconds
        if index is not None and previous_index is not None:
            if index > previous_index + 1:
                sequence_gaps += index - previous_index - 1
            elif index <= previous_index:
                sequence_gaps += 1
        contiguous = (
            index is not None and previous_index is not None and index == previous_index + 1
        )
        direct_interval = int(row["interval_ns"]) if row["interval_ns"] is not None else None
        delta_ns: int | None = None
        if contiguous:
            if elapsed_ns is not None and previous_elapsed_ns is not None:
                delta_ns = elapsed_ns - previous_elapsed_ns
                if direct_interval is not None and direct_interval != delta_ns:
                    mismatches += 1
            elif elapsed_seconds is not None and previous_elapsed_seconds is not None:
                delta_ns = round((elapsed_seconds - previous_elapsed_seconds) * 1_000_000_000)
                if direct_interval is not None and abs(direct_interval - delta_ns) > 2:
                    mismatches += 1
            else:
                delta_ns = direct_interval
        elif processed == 1 and direct_interval is not None:
            boundary_intervals += 1

        if contiguous and delta_ns is not None:
            if delta_ns < 0:
                negative_intervals += 1
            elif delta_ns == 0:
                zero_intervals += 1
                zero_count += 1
            else:
                interval_count += 1
                interval_sum_ns += delta_ns
                interval_min_ns = delta_ns if interval_min_ns is None else min(interval_min_ns, delta_ns)
                interval_max_ns = delta_ns if interval_max_ns is None else max(interval_max_ns, delta_ns)
                difference = delta_ns - interval_mean_ns
                interval_mean_ns += difference / interval_count
                interval_m2_ns += difference * (delta_ns - interval_mean_ns)
                interval_ms = delta_ns / 1_000_000
                positive_count += 1
                if interval_ms < HISTOGRAM_MIN_MS:
                    hist_index = 0
                    underflow_count += 1
                elif interval_ms >= HISTOGRAM_MAX_MS:
                    hist_index = HISTOGRAM_BINS - 1
                    overflow_count += 1
                else:
                    ratio = math.log(interval_ms / HISTOGRAM_MIN_MS) / math.log(
                        HISTOGRAM_MAX_MS / HISTOGRAM_MIN_MS
                    )
                    hist_index = min(HISTOGRAM_BINS - 1, max(0, int(ratio * HISTOGRAM_BINS)))
                histogram_counts[hist_index] += 1
                _add_interval_point(
                    interval_bins_shared,
                    interval_bins_stream,
                    shared_bucket_count=shared_interval_bin_count,
                    stream_bucket_count=stream_interval_bin_count,
                    shared_bucket_width=shared_interval_bin_width,
                    stream_bucket_width=stream_interval_bin_width,
                    elapsed_ns=elapsed_ns, elapsed_seconds=elapsed_seconds,
                    origin_ns=timestamp_origin_ns, origin_seconds=timestamp_origin_s,
                    value_ms=interval_ms,
                )
                _add_rate_interval(
                    rate_bins_shared, rate_bins_stream, interval_seconds=delta_ns / 1e9,
                    elapsed_ns=elapsed_ns, elapsed_seconds=elapsed_seconds,
                    timestamp_origin_ns=timestamp_origin_ns, timestamp_origin_s=timestamp_origin_s,
                    shared_window_seconds=shared_window_seconds,
                    stream_window_seconds=stream_window_seconds,
                    max_plot_points=max_plot_points,
                )
        previous_index = index
        previous_elapsed_ns = elapsed_ns
        previous_elapsed_seconds = elapsed_seconds
        if elapsed_ns is not None:
            last_elapsed_ns = elapsed_ns
        if elapsed_seconds is not None:
            last_elapsed_seconds = elapsed_seconds

    if processed != persisted_frames:
        raise RuntimeError("grouped history row count changed inside a read-only snapshot")
    median_ns, p95_ns, p99_ns = _exact_interval_percentiles(
        connection, benchmark_id, interval_count
    )
    interval_stats = MetricStats(
        count=interval_count,
        minimum=interval_min_ns / 1e6 if interval_min_ns is not None else None,
        maximum=interval_max_ns / 1e6 if interval_max_ns is not None else None,
        mean=(interval_sum_ns / interval_count / 1e6) if interval_count else None,
        median=median_ns / 1e6 if median_ns is not None else None,
        p95=p95_ns / 1e6 if p95_ns is not None else None,
        p99=p99_ns / 1e6 if p99_ns is not None else None,
    )
    histogram = _build_histogram(
        histogram_counts, positive_count, zero_count, underflow_count, overflow_count
    )
    interval_points_shared = _flatten_extrema(interval_bins_shared)
    interval_points_stream = _flatten_extrema(interval_bins_stream)
    fps_points_shared = _flatten_rates(rate_bins_shared)
    fps_points_stream = _flatten_rates(rate_bins_stream)
    timing_loss = (
        stream.timing_queue_loss_count > 0
        or stream.persistence_loss_count > 0
        or stream.sequence_gap_count > 0
        or sequence_gaps > 0
        or mismatches > 0
    )
    if timing_loss:
        rate_summary = None
    elif rate_summary is None and interval_count >= 2 and interval_sum_ns > 0:
        rate_summary = interval_count * 1e9 / interval_sum_ns
    recording_start_ns, recording_end_ns, duration = _stream_recording_markers(
        stream_event_rows(connection, stream)
    )

    warnings: list[str] = []
    if stream.timing_queue_loss_count:
        warnings.append(f"{stream.timing_queue_loss_count:,} timing samples were rejected before persistence.")
    if stream.persistence_loss_count:
        warnings.append(f"{stream.persistence_loss_count:,} timing samples were not committed after a writer failure.")
    if sequence_gaps:
        warnings.append(
            f"{sequence_gaps:,} missing stream sequence value(s) are visible; they do not prove camera sensor frame drops."
        )
    if missing_timestamps:
        warnings.append(
            f"{missing_timestamps:,} persisted frame row(s) lack exact integer elapsed_ns; "
            "compatibility elapsed_seconds may support per-stream interval calculations, but shared alignment is disabled."
        )
    if zero_intervals:
        warnings.append(f"{zero_intervals:,} zero-length interval(s) were counted and excluded from positive-interval statistics.")
    if negative_intervals:
        warnings.append(f"{negative_intervals:,} negative elapsed interval(s) were counted as invalid and excluded.")
    if boundary_intervals:
        warnings.append("A first-row interval had no preceding persisted arrival and was excluded as a recording boundary.")
    if mismatches:
        warnings.append(
            f"{mismatches:,} stored interval value(s) disagreed with adjacent host timestamps; "
            "the host timestamp difference is used and the discrepancy is flagged."
        )
    if persisted_frames < 2:
        warnings.append("Fewer than two persisted frame arrivals are available; an interval-derived rate is unavailable.")
    if not interval_count:
        warnings.append("No valid positive adjacent frame intervals are available for a timing curve or distribution.")
    if stream.clock_type == "monotonic_ns" and "host" in stream.timestamp_source:
        warnings.append("Timing is based on host frame-arrival events; sensor exposure and sensor-to-display latency are not measured.")
    else:
        warnings.append("Timing provenance does not identify a host monotonic frame-arrival source.")
    return GroupedStreamAnalysis(
        stream=stream,
        persisted_frame_samples=persisted_frames,
        persisted_read_failure_samples=persisted_failures,
        last_sequence_index=last_index,
        observed_sequence_gap_count=sequence_gaps,
        interval_stats=interval_stats,
        interval_stddev_ms=(
            math.sqrt(max(0.0, interval_m2_ns / interval_count)) / 1e6
            if interval_count > 0 else None
        ),
        interval_histogram=histogram,
        requested_capture_mode=requested_mode,
        driver_reported_capture_mode=driver_mode,
        requested_fps=requested_fps,
        driver_reported_fps=driver_fps,
        observed_capture_fps=rate_summary,
        preview_fps=preview_fps,
        processing_fps=processing_fps,
        recording_start_elapsed_ns=recording_start_ns,
        recording_end_elapsed_ns=recording_end_ns,
        recording_duration_seconds=duration,
        first_arrival_elapsed_ns=(
            stream.first_host_arrival_elapsed_ns if stream.first_host_arrival_elapsed_ns is not None else first_elapsed_ns
        ),
        last_arrival_elapsed_ns=(
            stream.last_host_arrival_elapsed_ns if stream.last_host_arrival_elapsed_ns is not None else last_elapsed_ns
        ),
        missing_timestamp_sample_count=missing_timestamps,
        zero_interval_count=zero_intervals,
        negative_interval_count=negative_intervals,
        boundary_interval_count=boundary_intervals,
        interval_mismatch_count=mismatches,
        fps_points_shared=fps_points_shared,
        fps_points_stream=fps_points_stream,
        interval_points_shared=interval_points_shared,
        interval_points_stream=interval_points_stream,
        warnings=tuple(warnings),
    )


def stream_event_rows(
    connection: sqlite3.Connection,
    stream: GroupedStreamRecord,
) -> tuple[ExperimentEventRecord, ...]:
    # The parent record is a complete snapshot, but this helper reads only the
    # two lifecycle markers needed for a duration and remains query-only.
    rows = connection.execute(
        """SELECT event_id, benchmark_id, event_type, elapsed_ns, observed_at, reason_code, detail_json
           FROM experiment_events WHERE session_id=? AND benchmark_id=?
           ORDER BY event_id""",
        (stream.session_id, stream.benchmark_id),
    ).fetchall()
    import json

    return tuple(ExperimentEventRecord(
        event_id=int(row["event_id"]),
        benchmark_id=row["benchmark_id"],
        event_type=str(row["event_type"]),
        elapsed_ns=row["elapsed_ns"],
        observed_at=str(row["observed_at"]),
        reason_code=row["reason_code"],
        details=json.loads(row["detail_json"] or "{}"),
    ) for row in rows)


def _stream_recording_markers(
    events: Iterable[ExperimentEventRecord],
) -> tuple[int | None, int | None, float | None]:
    items = tuple(events)
    starts = [item.elapsed_ns for item in items if item.event_type == "stream_recording"]
    ends = [item.elapsed_ns for item in items if item.event_type in {
        "stream_completed", "stream_interrupted", "stream_failed"
    }]
    if len(starts) != 1 or len(ends) != 1 or starts[0] is None or ends[0] is None:
        return None, None, None
    if int(ends[0]) < int(starts[0]):
        return int(starts[0]), int(ends[0]), None
    start_ns, end_ns = int(starts[0]), int(ends[0])
    return start_ns, end_ns, (end_ns - start_ns) / 1e9


def _shared_time_supports(
    group: GroupedExperimentRecord,
    streams: tuple[GroupedStreamAnalysis, ...],
) -> tuple[bool, str | None]:
    if not group.clock_epoch_id:
        return False, "The group has no shared monotonic clock-epoch identifier."
    marker = next((event for event in group.events if event.event_type == "recording_coordinator_started"), None)
    if marker is None or marker.details.get("clock_epoch_id") != group.clock_epoch_id:
        return False, "The coordinator start event does not confirm the group's clock epoch."
    for stream in group.streams:
        if stream.clock_type != "monotonic_ns" or "host_monotonic" not in stream.timestamp_source:
            return False, "Both streams must identify host-monotonic arrival timing."
    if any(item.missing_timestamp_sample_count for item in streams):
        return False, "One or more persisted frame arrivals lack integer elapsed timestamps."
    return True, None


def _shared_plot_duration(group: GroupedExperimentRecord) -> float:
    elapsed_values = [
        event.elapsed_ns for event in group.events if event.elapsed_ns is not None
    ]
    elapsed_values.extend(
        value for stream in group.streams
        for value in (stream.first_host_arrival_elapsed_ns, stream.last_host_arrival_elapsed_ns)
        if value is not None
    )
    return max((int(value) for value in elapsed_values), default=0) / 1e9


def _add_interval_point(
    shared: dict[int, _BucketExtrema],
    per_stream: dict[int, _BucketExtrema],
    *,
    shared_bucket_count: int,
    stream_bucket_count: int,
    shared_bucket_width: float,
    stream_bucket_width: float,
    elapsed_ns: int | None,
    elapsed_seconds: float | None,
    origin_ns: int | None,
    origin_seconds: float | None,
    value_ms: float,
) -> None:
    shared_x = elapsed_ns / 1e9 if elapsed_ns is not None else None
    if elapsed_ns is not None and origin_ns is not None:
        stream_x = (elapsed_ns - origin_ns) / 1e9
    elif elapsed_seconds is not None and origin_seconds is not None:
        stream_x = elapsed_seconds - origin_seconds
    else:
        stream_x = None
    if shared_x is None and stream_x is None:
        return
    for target, x, bucket_count, bucket_width in (
        (shared, shared_x, shared_bucket_count, shared_bucket_width),
        (per_stream, stream_x, stream_bucket_count, stream_bucket_width),
    ):
        if x is None or not math.isfinite(x):
            continue
        index = max(0, min(bucket_count - 1, int(max(0.0, x) / bucket_width)))
        target.setdefault(index, _BucketExtrema()).add((x, value_ms))


def _add_rate_interval(
    shared: dict[int, _BucketRate],
    per_stream: dict[int, _BucketRate],
    *,
    interval_seconds: float,
    elapsed_ns: int | None,
    elapsed_seconds: float | None,
    timestamp_origin_ns: int | None,
    timestamp_origin_s: float | None,
    shared_window_seconds: float,
    stream_window_seconds: float,
    max_plot_points: int,
) -> None:
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        return
    shared_x = elapsed_ns / 1e9 if elapsed_ns is not None else None
    if elapsed_ns is not None and timestamp_origin_ns is not None:
        stream_x = (elapsed_ns - timestamp_origin_ns) / 1e9
    elif elapsed_seconds is not None and timestamp_origin_s is not None:
        stream_x = elapsed_seconds - timestamp_origin_s
    else:
        stream_x = None
    for target, x, window_seconds in (
        (shared, shared_x, shared_window_seconds),
        (per_stream, stream_x, stream_window_seconds),
    ):
        if x is None or not math.isfinite(x):
            continue
        index = max(0, int(max(0.0, x) / window_seconds))
        if index >= max_plot_points:
            index = max_plot_points - 1
        bucket = target.setdefault(index, _BucketRate())
        bucket.count += 1
        bucket.duration_ns += round(interval_seconds * 1e9)
        bucket.x_sum += x


def _flatten_extrema(buckets: dict[int, _BucketExtrema]) -> tuple[tuple[float, float], ...]:
    points: dict[float, tuple[float, float]] = {}
    for bucket in buckets.values():
        for point in (bucket.low, bucket.high):
            if point is not None:
                points[point[0]] = point
    return tuple(points[key] for key in sorted(points))


def _flatten_rates(buckets: dict[int, _BucketRate]) -> tuple[tuple[float, float], ...]:
    points = []
    for bucket in buckets.values():
        # One interval is too sparse to label a window as a rate.
        if bucket.count < 2 or bucket.duration_ns <= 0:
            continue
        points.append((bucket.x_sum / bucket.count, bucket.count * 1e9 / bucket.duration_ns))
    return tuple(sorted(points))


def _build_histogram(
    counts: list[int], total: int, zero_count: int, underflow: int, overflow: int
) -> Histogram:
    if total <= 0:
        return Histogram(zero_sample_count=zero_count)
    log_span = math.log(HISTOGRAM_MAX_MS / HISTOGRAM_MIN_MS)
    centers = tuple(
        HISTOGRAM_MIN_MS * math.exp(log_span * (index + 0.5) / HISTOGRAM_BINS)
        for index in range(HISTOGRAM_BINS)
    )
    return Histogram(
        centers_ms=tuple(center for center, count in zip(centers, counts) if count),
        fractions=tuple(count / total for count in counts if count),
        positive_sample_count=total,
        zero_sample_count=zero_count,
        underflow_count=underflow,
        overflow_count=overflow,
    )


def _exact_interval_percentiles(
    connection: sqlite3.Connection,
    benchmark_id: str,
    valid_count: int,
) -> tuple[int | None, int | None, int | None]:
    if valid_count < 1:
        return None, None, None
    median_low = (valid_count - 1) // 2 + 1
    median_high = valid_count // 2 + 1
    p95 = max(1, math.ceil(valid_count * 0.95))
    p99 = max(1, math.ceil(valid_count * 0.99))
    ranks = tuple(sorted({median_low, median_high, p95, p99}))
    placeholders = ",".join("?" for _ in ranks)
    rows = connection.execute(
        f"""WITH ordered AS (
                 SELECT sample_index, elapsed_ns, elapsed_seconds, interval_ns,
                        LAG(sample_index) OVER (
                            ORDER BY sample_index IS NULL, sample_index, id
                        ) AS previous_index,
                        LAG(elapsed_ns) OVER (
                            ORDER BY sample_index IS NULL, sample_index, id
                        ) AS previous_elapsed_ns,
                        LAG(elapsed_seconds) OVER (
                            ORDER BY sample_index IS NULL, sample_index, id
                        ) AS previous_elapsed_seconds
                 FROM measurements
                 WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
             ), candidates AS (
                 SELECT CASE
                          WHEN elapsed_ns IS NOT NULL AND previous_elapsed_ns IS NOT NULL
                            THEN elapsed_ns - previous_elapsed_ns
                          WHEN elapsed_seconds IS NOT NULL AND previous_elapsed_seconds IS NOT NULL
                            THEN CAST(ROUND((elapsed_seconds-previous_elapsed_seconds)*1000000000) AS INTEGER)
                          ELSE interval_ns
                        END AS interval_ns
                 FROM ordered
                 WHERE sample_index=previous_index+1
             ), valid AS (
                 SELECT interval_ns FROM candidates WHERE interval_ns>0
             ), ranked AS (
                 SELECT interval_ns, ROW_NUMBER() OVER (ORDER BY interval_ns) AS rank
                 FROM valid
             )
             SELECT rank, interval_ns FROM ranked WHERE rank IN ({placeholders})""",
        (benchmark_id, *ranks),
    ).fetchall()
    values = {int(row["rank"]): int(row["interval_ns"]) for row in rows}
    if len(values) != len(ranks):
        raise RuntimeError("grouped interval percentile query disagrees with the streamed sample count")
    median = (values[median_low] + values[median_high]) / 2
    return median, values[p95], values[p99]


def _configuration_fps(configuration: dict[str, Any], key: str) -> float | None:
    value = configuration.get(key)
    return _finite_positive(value)


def _capture_mode_text(
    configuration: Mapping[str, Any],
    settings: tuple[Any, ...],
    *,
    configuration_keys: tuple[str, ...],
    setting_attribute: str,
    include_top_level: bool,
) -> str | None:
    candidate: Mapping[str, Any] = {}
    for key in configuration_keys:
        value = configuration.get(key)
        if isinstance(value, Mapping):
            candidate = value
            break
    if not candidate and include_top_level:
        candidate = configuration
    values = {str(key).lower(): value for key, value in candidate.items()}
    for item in settings:
        key = str(getattr(item, "setting_key", "")).lower()
        if key not in {"width", "capture_width", "height", "capture_height", "pixel_format", "format", "fps", "frame_rate"}:
            continue
        value = getattr(item, setting_attribute, None)
        if value is not None:
            values[key] = value
    width = values.get("width", values.get("capture_width"))
    height = values.get("height", values.get("capture_height"))
    pixel = values.get("pixel_format", values.get("format"))
    rate = _finite_positive(values.get("fps", values.get("frame_rate")))
    if not width or not height or not pixel:
        return None
    result = f"{width}×{height} {pixel}"
    return f"{result} · {rate:g} FPS" if rate is not None else result


def _setting_fps(settings: tuple[Any, ...], attribute: str) -> float | None:
    for setting in settings:
        if setting.setting_key.lower() in {"fps", "frame_rate", "framerate"}:
            value = getattr(setting, attribute, None)
            rate = _finite_positive(value)
            if rate is not None:
                return rate
    return None


def _summary_value(summary: dict[str, Any] | None, key: str) -> float | None:
    return _finite_positive(summary.get(key)) if summary else None


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None
