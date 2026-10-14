"""Read-only, bounded-memory analysis of persisted experiment measurements."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .db import Database
from .identity import camera_family_key, identity_fingerprint, identity_source_label

DEFAULT_MAX_PLOT_POINTS = 2400
FPS_WINDOW_SECONDS = 1.0
HISTOGRAM_BINS = 72
HISTOGRAM_MIN_MS = 1e-3
HISTOGRAM_MAX_MS = 1e7

_DIRECT_INTERVAL_METRICS = ("frame_interval_ms", "capture_frame_interval_ms")
_CAPTURE_FPS_METRICS = ("capture_fps", "measured_fps")


@dataclass(frozen=True, slots=True)
class ExperimentOption:
    session_id: str
    benchmark_id: str | None
    label: str
    camera: str
    source: str
    status: str
    started_at: str | None
    camera_key: str = ""
    mode_label: str = "Mode unavailable"
    requested_fps: float | None = None
    camera_group_key: str = ""
    identity_source: str = ""
    identity_badge: str = ""


@dataclass(frozen=True, slots=True)
class MetricStats:
    count: int
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    median: float | None = None
    p95: float | None = None
    p99: float | None = None
    from_saved_summary: bool = False


@dataclass(frozen=True, slots=True)
class Histogram:
    centers_ms: tuple[float, ...] = ()
    fractions: tuple[float, ...] = ()
    positive_sample_count: int = 0
    zero_sample_count: int = 0
    underflow_count: int = 0
    overflow_count: int = 0


@dataclass(frozen=True, slots=True)
class ExperimentAnalysis:
    option: ExperimentOption
    notes: str | None
    requested_settings: dict[str, Any]
    driver_settings: dict[str, Any]
    configuration_note: str | None
    duration_seconds: float | None
    duration_source: str | None
    frame_count: int | None
    frame_count_source: str | None
    read_failures: int | None
    failure_source: str | None
    preview_fps_mean: float | None
    processing_fps_mean: float | None
    interval_stats: MetricStats
    interval_points: tuple[tuple[float, float], ...]
    interval_x_label: str
    interval_metric: str | None
    interval_scope: str | None
    interval_method: str | None
    interval_histogram: Histogram
    reported_interval_summary: dict[str, MetricStats]
    capture_fps_stats: MetricStats
    fps_points: tuple[tuple[float, float], ...]
    fps_x_label: str
    fps_method: str
    timing_method: str | None
    saved_status: str
    termination_reason: str | None
    interval_data_available: bool
    interval_boundary_samples_excluded: int = 0


@dataclass(frozen=True, slots=True)
class ComparisonNotice:
    message: str
    is_warning: bool


def list_experiments(database: Database) -> list[ExperimentOption]:
    """Return experiment labels and identities without opening a writable DB handle."""
    with database.connect_readonly() as connection:
        rows = connection.execute(
            """SELECT s.id AS session_id, b.id AS benchmark_id, s.label AS session_label,
                      s.source, b.label AS benchmark_label, b.status,
                      b.started_at AS benchmark_started_at, s.started_at AS session_started_at,
                      c.id AS camera_id, c.stable_identifier, c.identity_source,
                      c.vendor_id, c.product_id, c.model, c.manufacturer,
                      b.configuration_json
               FROM sessions s
               LEFT JOIN benchmark_runs b ON b.session_id=s.id
               LEFT JOIN cameras c ON c.id=s.camera_id
               ORDER BY COALESCE(b.started_at, s.started_at, s.created_at) DESC, b.id"""
        ).fetchall()
        setting_rows = connection.execute(
            """SELECT benchmark_id, setting_key, requested_value_json, reported_value_json
               FROM setting_snapshots WHERE benchmark_id IS NOT NULL ORDER BY setting_key"""
        ).fetchall()
    settings_by_benchmark: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for setting in setting_rows:
        requested_settings, reported_settings = settings_by_benchmark.setdefault(
            str(setting["benchmark_id"]), ({}, {})
        )
        key = str(setting["setting_key"])
        if setting["requested_value_json"] is not None:
            requested_settings.setdefault(key, _json_value(setting["requested_value_json"]))
        if setting["reported_value_json"] is not None:
            reported_settings.setdefault(key, _json_value(setting["reported_value_json"]))
    options: list[ExperimentOption] = []
    for row in rows:
        source = str(row["source"] or "unknown")
        label = str(row["session_label"] or "Unnamed experiment")
        benchmark_label = row["benchmark_label"]
        if benchmark_label and benchmark_label != label:
            label = f"{label} · {benchmark_label}"
        camera = row["model"] or row["manufacturer"] or "Camera identity unavailable"
        configuration = _json_object(row["configuration_json"])
        requested, reported, configuration_note = _capture_configuration(configuration)
        snapshot_requested, snapshot_reported = settings_by_benchmark.get(
            str(row["benchmark_id"]), ({}, {})
        )
        for key, value in snapshot_requested.items():
            requested.setdefault(key, value)
        for key, value in snapshot_reported.items():
            reported.setdefault(key, value)
        stored_identity = str(
            row["stable_identifier"] or row["camera_id"] or _camera_key_fallback(camera)
        )
        options.append(ExperimentOption(
            session_id=str(row["session_id"]),
            benchmark_id=str(row["benchmark_id"]) if row["benchmark_id"] else None,
            label=label,
            camera=str(camera),
            source=source,
            status=str(row["status"] or ("imported" if source == "imported" else "session")),
            started_at=row["benchmark_started_at"] or row["session_started_at"],
            camera_key=stored_identity,
            mode_label=_mode_filter_label(requested, reported, configuration_note),
            requested_fps=_requested_fps(requested),
            camera_group_key=camera_family_key(
                row["vendor_id"], row["product_id"], row["manufacturer"], row["model"],
                stored_identity,
            ),
            identity_source=str(row["identity_source"] or ""),
            identity_badge=(
                f"{identity_source_label(row['identity_source'])} · "
                f"{identity_fingerprint(row['stable_identifier'] or row['camera_id'])}"
            ),
        ))
    return options


def filter_experiments(
    options: Iterable[ExperimentOption],
    *,
    camera_key: str | None = None,
    camera_group_key: str | None = None,
    mode_label: str | None = None,
    requested_fps: float | None = None,
    search: str = "",
) -> list[ExperimentOption]:
    """Filter saved runs using recorded camera identity and requested mode only."""
    needle = search.strip().casefold()
    result = []
    for option in options:
        if camera_key and option.camera_key != camera_key:
            continue
        if camera_group_key and option.camera_group_key != camera_group_key:
            continue
        if mode_label and option.mode_label != mode_label:
            continue
        if requested_fps is not None and option.requested_fps != requested_fps:
            continue
        searchable = " ".join((
            option.label,
            option.camera,
            option.mode_label,
            option.source,
            option.status,
            option.started_at or "",
        )).casefold()
        if needle and needle not in searchable:
            continue
        result.append(option)
    return result


def rate_deviation_percent(
    points: Iterable[tuple[float, float]], mean_fps: float | None
) -> tuple[tuple[float, float], ...]:
    """Map recorded FPS points to deviation from the session-wide observed mean."""
    if mean_fps is None or not math.isfinite(mean_fps) or mean_fps <= 0:
        return ()
    return tuple((elapsed, (fps / mean_fps - 1.0) * 100.0) for elapsed, fps in points)


def load_analysis(
    database: Database,
    benchmark_id: str | None,
    session_id: str,
    *,
    max_plot_points: int = DEFAULT_MAX_PLOT_POINTS,
) -> ExperimentAnalysis:
    """Read and summarize one experiment on a query-only connection."""
    with database.connect_readonly() as connection:
        return _load_analysis(connection, benchmark_id, session_id, max_plot_points=max_plot_points)


def load_comparison(
    database: Database,
    left: tuple[str | None, str],
    right: tuple[str | None, str],
    *,
    max_plot_points: int = DEFAULT_MAX_PLOT_POINTS,
) -> tuple[ExperimentAnalysis, ExperimentAnalysis]:
    """Load both sides from the same read-only SQLite snapshot."""
    with database.connect_readonly() as connection:
        return (
            _load_analysis(connection, left[0], left[1], max_plot_points=max_plot_points),
            _load_analysis(connection, right[0], right[1], max_plot_points=max_plot_points),
        )


def compare_methodology(
    left: ExperimentAnalysis, right: ExperimentAnalysis
) -> tuple[ComparisonNotice, ...]:
    notices: list[ComparisonNotice] = []
    if left.interval_scope != right.interval_scope:
        notices.append(ComparisonNotice(
            "Interval stream scopes differ or are unavailable; compare the interval summaries cautiously.",
            True,
        ))
    if left.interval_method != right.interval_method:
        notices.append(ComparisonNotice(
            "Frame-interval timing methods differ. Values may not be directly comparable.",
            True,
        ))
    if left.interval_data_available != right.interval_data_available:
        notices.append(ComparisonNotice(
            "One experiment has raw frame intervals while the other has only summary or imported fields.",
            True,
        ))
    if left.option.source != right.option.source:
        notices.append(ComparisonNotice(
            "One experiment is imported history and the other is a locally recorded experiment.",
            True,
        ))
    if left.timing_method != right.timing_method and left.timing_method and right.timing_method:
        notices.append(ComparisonNotice(
            "Host timestamp methods differ. These are frame-arrival measurements, not sensor exposure timestamps.",
            True,
        ))
    if left.duration_source != right.duration_source and left.duration_source and right.duration_source:
        notices.append(ComparisonNotice(
            "Session duration boundaries were obtained differently; duration differences may reflect measurement scope.",
            True,
        ))
    if not notices:
        message = (
            "Both experiments use the same recorded interval scope and timing method."
            if left.interval_data_available and right.interval_data_available
            else "At least one experiment has no raw interval series; only available saved summaries can be compared."
        )
        notices.append(ComparisonNotice(message, False))
    return tuple(notices)


def _load_analysis(
    connection: Any,
    benchmark_id: str | None,
    session_id: str,
    *,
    max_plot_points: int,
) -> ExperimentAnalysis:
    if max_plot_points < 4:
        raise ValueError("max_plot_points must be at least 4")
    if benchmark_id:
        row = connection.execute(
            """SELECT s.id AS session_id, s.label AS session_label, s.source, s.notes,
                      s.started_at AS session_started_at, s.ended_at AS session_ended_at,
                      b.id AS benchmark_id, b.label AS benchmark_label, b.status,
                      b.started_at AS benchmark_started_at, b.ended_at AS benchmark_ended_at,
                      b.configuration_json, c.model, c.manufacturer,
                      z.frame_count, z.read_failures, z.duration_seconds,
                      z.capture_fps, z.preview_fps, z.processing_fps, z.interval_count,
                      z.median_interval_ms, z.p95_interval_ms, z.p99_interval_ms, z.summary_json,
                      c.id AS camera_id, c.stable_identifier, c.identity_source,
                      c.vendor_id, c.product_id
               FROM sessions s JOIN benchmark_runs b ON b.session_id=s.id
               LEFT JOIN cameras c ON c.id=s.camera_id
               LEFT JOIN benchmark_summaries z ON z.benchmark_id=b.id
               WHERE b.id=? AND s.id=?""",
            (benchmark_id, session_id),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT s.id AS session_id, s.label AS session_label, s.source, s.notes,
                      s.started_at AS session_started_at, s.ended_at AS session_ended_at,
                      NULL AS benchmark_id, NULL AS benchmark_label, NULL AS status,
                      NULL AS benchmark_started_at, NULL AS benchmark_ended_at,
                      NULL AS configuration_json, c.model, c.manufacturer,
                      NULL AS frame_count, NULL AS read_failures, NULL AS duration_seconds,
                      NULL AS capture_fps, NULL AS preview_fps, NULL AS processing_fps,
                      NULL AS interval_count, NULL AS median_interval_ms, NULL AS p95_interval_ms,
                      NULL AS p99_interval_ms, NULL AS summary_json,
                      c.id AS camera_id, c.stable_identifier, c.identity_source,
                      c.vendor_id, c.product_id
               FROM sessions s LEFT JOIN cameras c ON c.id=s.camera_id WHERE s.id=?""",
            (session_id,),
        ).fetchone()
    if row is None:
        raise ValueError("The selected experiment no longer exists in the local history database.")

    source = str(row["source"] or "unknown")
    label = str(row["session_label"] or "Unnamed experiment")
    if row["benchmark_label"] and row["benchmark_label"] != label:
        label = f"{label} · {row['benchmark_label']}"
    requested, reported, configuration_note = _capture_configuration(
        _json_object(row["configuration_json"])
    )
    if benchmark_id:
        setting_rows = connection.execute(
            """SELECT setting_key, requested_value_json, reported_value_json
               FROM setting_snapshots WHERE benchmark_id=? ORDER BY setting_key""",
            (benchmark_id,),
        ).fetchall()
        for setting in setting_rows:
            key = str(setting["setting_key"])
            if setting["requested_value_json"] is not None:
                requested.setdefault(key, _json_value(setting["requested_value_json"]))
            if setting["reported_value_json"] is not None:
                reported.setdefault(key, _json_value(setting["reported_value_json"]))

    camera = str(row["model"] or row["manufacturer"] or "Camera identity unavailable")
    option = ExperimentOption(
        session_id=session_id,
        benchmark_id=benchmark_id,
        label=label,
        camera=camera,
        source=source,
        status=str(row["status"] or ("imported" if source == "imported" else "session")),
        started_at=row["benchmark_started_at"] or row["session_started_at"],
        camera_key=str(row["stable_identifier"] or row["camera_id"] or _camera_key_fallback(camera)),
        mode_label=_mode_filter_label(requested, reported, configuration_note),
        requested_fps=_requested_fps(requested),
        camera_group_key=camera_family_key(
            row["vendor_id"], row["product_id"], row["manufacturer"], row["model"],
            str(row["stable_identifier"] or row["camera_id"] or _camera_key_fallback(camera)),
        ),
        identity_source=str(row["identity_source"] or ""),
        identity_badge=(
            f"{identity_source_label(row['identity_source'])} · "
            f"{identity_fingerprint(row['stable_identifier'] or row['camera_id'])}"
        ),
    )

    summary = _json_object(row["summary_json"])
    termination_reason = summary.get("termination_reason")
    if not benchmark_id:
        return _empty_analysis(option, row["notes"], requested, reported, configuration_note)

    reported_interval_summary = _reported_interval_summary(connection, benchmark_id)
    interval_source = _select_interval_source(connection, benchmark_id)
    if interval_source is None:
        interval_stats = _saved_interval_stats(row)
        interval_points: tuple[tuple[float, float], ...] = ()
        interval_histogram = Histogram()
        interval_x_label = "Elapsed time from recorded intervals (s)"
        interval_metric = None
        interval_scope = None
        interval_method = None
        interval_data_available = False
        interval_boundary_samples_excluded = 0
        fps_points: tuple[tuple[float, float], ...] = ()
        fps_x_label = interval_x_label
        fps_method = "No raw frame intervals were stored."
        capture_fps_stats, fps_points, fps_x_label, fps_method = _stored_capture_fps(
            connection, benchmark_id, row, max_plot_points
        )
    else:
        boundary_record_id = _legacy_first_interval_boundary_id(
            connection, benchmark_id, interval_source
        )
        interval_boundary_samples_excluded = int(boundary_record_id is not None)
        (
            interval_stats,
            interval_points,
            interval_x_label,
            interval_histogram,
            fps_points,
            fps_x_label,
            fps_method,
            interval_method,
        ) = _read_interval_series(
            connection, benchmark_id, interval_source, max_plot_points,
            excluded_raw_record_id=boundary_record_id,
        )
        interval_metric, interval_scope = interval_source.metric, interval_source.scope
        interval_data_available = interval_stats.count > 0
        if interval_data_available and interval_scope == "capture":
            capture_fps_stats = _capture_fps_from_intervals(
                connection, benchmark_id, interval_source, interval_stats,
                excluded_raw_record_id=boundary_record_id,
            )
        elif interval_data_available:
            capture_fps_stats, fps_points, fps_x_label, fps_method = _stored_capture_fps(
                connection, benchmark_id, row, max_plot_points
            )
            fps_points = ()
            fps_method = "Capture FPS was not derived because the interval stream scope is unknown."
        else:
            capture_fps_stats, fps_points, fps_x_label, fps_method = _stored_capture_fps(
                connection, benchmark_id, row, max_plot_points
            )

    preview_fps_mean = _mean_metric(connection, benchmark_id, ("preview_fps",), "preview")
    if preview_fps_mean is None and row["preview_fps"] is not None:
        preview_fps_mean = float(row["preview_fps"])
    processing_fps_mean = _mean_metric(connection, benchmark_id, ("processing_fps",), "processing")
    if processing_fps_mean is None and row["processing_fps"] is not None:
        processing_fps_mean = float(row["processing_fps"])

    frame_count, frame_count_source = _frame_count(connection, benchmark_id, row)
    read_failures, failure_source = _failure_count(connection, benchmark_id, row)
    duration_seconds, duration_source = _duration(connection, benchmark_id, row, summary)
    timing_method = _timing_method(connection, benchmark_id, interval_source)
    return ExperimentAnalysis(
        option=option,
        notes=row["notes"],
        requested_settings=requested,
        driver_settings=reported,
        configuration_note=configuration_note,
        duration_seconds=duration_seconds,
        duration_source=duration_source,
        frame_count=frame_count,
        frame_count_source=frame_count_source,
        read_failures=read_failures,
        failure_source=failure_source,
        preview_fps_mean=preview_fps_mean,
        processing_fps_mean=processing_fps_mean,
        interval_stats=interval_stats,
        interval_points=interval_points,
        interval_x_label=interval_x_label,
        interval_metric=interval_metric,
        interval_scope=interval_scope,
        interval_method=interval_method,
        interval_histogram=interval_histogram,
        reported_interval_summary=reported_interval_summary,
        capture_fps_stats=capture_fps_stats,
        fps_points=fps_points,
        fps_x_label=fps_x_label,
        fps_method=fps_method,
        timing_method=timing_method,
        saved_status=option.status,
        termination_reason=str(termination_reason) if termination_reason else None,
        interval_data_available=interval_data_available,
        interval_boundary_samples_excluded=interval_boundary_samples_excluded,
    )


@dataclass(frozen=True, slots=True)
class _IntervalSource:
    metric: str
    scope: str
    value_expression: str
    requires_interval_ns: bool = False


def _select_interval_source(connection: Any, benchmark_id: str) -> _IntervalSource | None:
    for metric in _DIRECT_INTERVAL_METRICS:
        rows = connection.execute(
            """SELECT stream_scope, COUNT(*) AS sample_count FROM measurements
               WHERE benchmark_id=? AND metric=? AND stream_scope IN ('capture', 'unknown')
                 AND value >= 0
               GROUP BY stream_scope ORDER BY CASE stream_scope WHEN 'capture' THEN 0 ELSE 1 END""",
            (benchmark_id, metric),
        ).fetchall()
        for row in rows:
            if int(row["sample_count"]) > 0:
                return _IntervalSource(metric, str(row["stream_scope"]), "value")
    count = connection.execute(
        """SELECT COUNT(*) FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'
             AND interval_ns IS NOT NULL AND interval_ns >= 0""",
        (benchmark_id,),
    ).fetchone()[0]
    if count:
        return _IntervalSource(
            "frame_arrival", "capture", "interval_ns / 1000000.0", requires_interval_ns=True
        )
    return None


def _legacy_first_interval_boundary_id(
    connection: Any, benchmark_id: str, source: _IntervalSource
) -> int | None:
    """Identify a legacy CSV first-row zero marker without deleting its raw record."""
    if source.metric != "capture_frame_interval_ms" or source.scope != "capture":
        return None
    row = connection.execute(
        """SELECT m.raw_record_id, m.value, m.method
           FROM measurements m LEFT JOIN raw_records rr ON rr.id=m.raw_record_id
           WHERE m.benchmark_id=? AND m.metric=? AND m.stream_scope=? AND m.value>=0
           ORDER BY rr.row_number IS NULL, rr.row_number, m.id LIMIT 1""",
        (benchmark_id, source.metric, source.scope),
    ).fetchone()
    if (
        row is not None
        and row["raw_record_id"] is not None
        and float(row["value"]) == 0.0
        and row["method"] == "legacy_csv:interval_ms"
    ):
        return int(row["raw_record_id"])
    return None


def _read_interval_series(
    connection: Any,
    benchmark_id: str,
    source: _IntervalSource,
    max_plot_points: int,
    *,
    excluded_raw_record_id: int | None = None,
) -> tuple[MetricStats, tuple[tuple[float, float], ...], str, Histogram,
           tuple[tuple[float, float], ...], str, str, str | None]:
    where = "benchmark_id=? AND metric=? AND stream_scope=? AND value >= 0"
    params: tuple[object, ...] = (benchmark_id, source.metric, source.scope)
    if source.requires_interval_ns:
        where = "benchmark_id=? AND metric=? AND stream_scope=? AND interval_ns IS NOT NULL AND interval_ns >= 0"
        params = (benchmark_id, source.metric, source.scope)
    if excluded_raw_record_id is not None:
        where += " AND (raw_record_id IS NULL OR raw_record_id != ?)"
        params += (excluded_raw_record_id,)
    aggregate = connection.execute(
        f"""SELECT COUNT(*) AS n, MIN({source.value_expression}) AS minimum,
                   MAX({source.value_expression}) AS maximum, AVG({source.value_expression}) AS mean,
                   COUNT(elapsed_seconds) AS elapsed_count,
                   MIN(elapsed_seconds) AS first_elapsed, MAX(elapsed_seconds) AS last_elapsed,
                   SUM({source.value_expression}) AS total_interval_ms
            FROM measurements WHERE {where}""",
        params,
    ).fetchone()
    count = int(aggregate["n"] or 0)
    if not count:
        return MetricStats(0), (), "Elapsed time from recorded intervals (s)", Histogram(), (), (
            "Elapsed time from recorded intervals (s)"
        ), "No valid frame intervals were stored.", _first_method(connection, benchmark_id, source)
    minimum = float(aggregate["minimum"])
    maximum = float(aggregate["maximum"])
    mean = float(aggregate["mean"])
    median = _ordered_percentile(
        connection, source, benchmark_id, count, 0.50, median=True,
        excluded_raw_record_id=excluded_raw_record_id,
    )
    p95 = _ordered_percentile(
        connection, source, benchmark_id, count, 0.95,
        excluded_raw_record_id=excluded_raw_record_id,
    )
    p99 = _ordered_percentile(
        connection, source, benchmark_id, count, 0.99,
        excluded_raw_record_id=excluded_raw_record_id,
    )
    stats = MetricStats(count, minimum, maximum, mean, median, p95, p99)
    all_have_elapsed = int(aggregate["elapsed_count"]) == count
    x_label = (
        "Recorder elapsed time (s)"
        if all_have_elapsed
        else "Cumulative recorded intervals (s)"
    )
    method = _first_method(connection, benchmark_id, source)

    bucket_count = min(max_plot_points // 2, count)
    extrema: dict[int, list[tuple[int, float, float]]] = {}
    rates: dict[int, list[float]] = {}
    histogram_counts = [0] * HISTOGRAM_BINS
    positive_count = zero_count = underflow_count = overflow_count = 0
    cumulative_seconds = 0.0
    if all_have_elapsed:
        time_origin = float(aggregate["first_elapsed"] or 0.0)
        time_span = max(0.0, float(aggregate["last_elapsed"] or time_origin) - time_origin)
    else:
        time_origin = 0.0
        time_span = max(0.0, float(aggregate["total_interval_ms"] or 0.0) / 1000.0)
    window_width = max(FPS_WINDOW_SECONDS, time_span / max_plot_points)
    cursor = connection.execute(
        f"""SELECT id, sample_index, elapsed_seconds, {source.value_expression} AS interval_ms
            FROM measurements WHERE {where}
            ORDER BY sample_index IS NULL, sample_index, observed_at, id""",
        params,
    )
    for ordinal, sample in enumerate(cursor):
        interval_ms = float(sample["interval_ms"])
        if not math.isfinite(interval_ms) or interval_ms < 0:
            continue
        elapsed_seconds = (
            float(sample["elapsed_seconds"])
            if all_have_elapsed and sample["elapsed_seconds"] is not None
            else None
        )
        if elapsed_seconds is None:
            cumulative_seconds += interval_ms / 1000.0
            x = cumulative_seconds
        else:
            x = elapsed_seconds
        bucket = min(bucket_count - 1, ordinal * bucket_count // count)
        point = (ordinal, x, interval_ms)
        pair = extrema.get(bucket)
        if pair is None:
            extrema[bucket] = [point, point]
        else:
            if interval_ms < pair[0][2]:
                pair[0] = point
            if interval_ms > pair[1][2]:
                pair[1] = point
        window = math.floor(max(0.0, x - time_origin) / window_width)
        window = min(max_plot_points - 1, max(0, window))
        rate = rates.setdefault(window, [0.0, 0.0, 0.0])
        rate[0] += 1.0
        rate[1] += interval_ms / 1000.0
        rate[2] += x

        if interval_ms == 0:
            zero_count += 1
        else:
            positive_count += 1
            if interval_ms < HISTOGRAM_MIN_MS:
                index = 0
                underflow_count += 1
            elif interval_ms >= HISTOGRAM_MAX_MS:
                index = HISTOGRAM_BINS - 1
                overflow_count += 1
            else:
                ratio = math.log(interval_ms / HISTOGRAM_MIN_MS) / math.log(
                    HISTOGRAM_MAX_MS / HISTOGRAM_MIN_MS
                )
                index = min(HISTOGRAM_BINS - 1, max(0, int(ratio * HISTOGRAM_BINS)))
            histogram_counts[index] += 1

    points_by_index: dict[int, tuple[float, float]] = {}
    for pair in extrema.values():
        for ordinal, x, value in pair:
            points_by_index[ordinal] = (x, value)
    points = tuple(points_by_index[index] for index in sorted(points_by_index))
    histogram = _make_histogram(
        histogram_counts, positive_count, zero_count, underflow_count, overflow_count
    )

    fps_points = tuple(
        (values[2] / values[0], values[0] / values[1])
        for _window, values in sorted(rates.items())
        if values[0] and values[1] > 0
    )
    if not all_have_elapsed:
        x_label = "Cumulative recorded intervals (s)"
    fps_method = _fps_window_method(all_have_elapsed, window_width, max_plot_points)
    return stats, points, x_label, histogram, fps_points, x_label, fps_method, method


def _ordered_percentile(
    connection: Any,
    source: _IntervalSource,
    benchmark_id: str,
    count: int,
    fraction: float,
    *,
    median: bool = False,
    descending: bool = False,
    excluded_raw_record_id: int | None = None,
) -> float | None:
    if count < 1:
        return None
    if median:
        low = (count - 1) // 2
        high = count // 2
        values = [
            _value_at_offset(connection, source, benchmark_id, low, descending, excluded_raw_record_id),
            _value_at_offset(connection, source, benchmark_id, high, descending, excluded_raw_record_id),
        ]
        valid = [value for value in values if value is not None]
        return sum(valid) / len(valid) if valid else None
    offset = max(0, math.ceil(count * fraction) - 1)
    return _value_at_offset(
        connection, source, benchmark_id, offset, descending, excluded_raw_record_id
    )


def _value_at_offset(
    connection: Any,
    source: _IntervalSource,
    benchmark_id: str,
    offset: int,
    descending: bool,
    excluded_raw_record_id: int | None = None,
) -> float | None:
    where = "benchmark_id=? AND metric=? AND stream_scope=? AND value >= 0"
    if source.requires_interval_ns:
        where = "benchmark_id=? AND metric=? AND stream_scope=? AND interval_ns IS NOT NULL AND interval_ns >= 0"
    direction = "DESC" if descending else "ASC"
    params: tuple[object, ...] = (benchmark_id, source.metric, source.scope)
    if excluded_raw_record_id is not None:
        where += " AND (raw_record_id IS NULL OR raw_record_id != ?)"
        params += (excluded_raw_record_id,)
    row = connection.execute(
        f"""SELECT {source.value_expression} AS value FROM measurements WHERE {where}
            ORDER BY {source.value_expression} {direction} LIMIT 1 OFFSET ?""",
        (*params, offset),
    ).fetchone()
    return float(row["value"]) if row else None


def _capture_fps_from_intervals(
    connection: Any,
    benchmark_id: str,
    source: _IntervalSource,
    interval_stats: MetricStats,
    *,
    excluded_raw_record_id: int | None = None,
) -> MetricStats:
    if interval_stats.mean is None or interval_stats.mean <= 0:
        return MetricStats(0)
    count = interval_stats.count
    median_interval_in_fps_order = _ordered_percentile(
        connection, source, benchmark_id, count, 0.5, median=True, descending=True,
        excluded_raw_record_id=excluded_raw_record_id,
    )
    fps_median = 1000.0 / median_interval_in_fps_order if median_interval_in_fps_order and median_interval_in_fps_order > 0 else None
    fps_mean = 1000.0 / interval_stats.mean
    return MetricStats(count, mean=fps_mean, median=fps_median)


def _stored_capture_fps(
    connection: Any, benchmark_id: str, row: Any, max_plot_points: int
) -> tuple[MetricStats, tuple[tuple[float, float], ...], str, str]:
    placeholders = ",".join("?" for _ in _CAPTURE_FPS_METRICS)
    params: tuple[object, ...] = (benchmark_id, *_CAPTURE_FPS_METRICS)
    aggregate = connection.execute(
        f"""SELECT COUNT(*) AS n, MIN(value) AS minimum, MAX(value) AS maximum, AVG(value) AS mean,
                   COUNT(elapsed_seconds) AS elapsed_count
            FROM measurements WHERE benchmark_id=? AND metric IN ({placeholders})
              AND stream_scope='capture' AND value >= 0""",
        params,
    ).fetchone()
    count = int(aggregate["n"] or 0)
    points_by_bucket: dict[int, list[tuple[int, float, float]]] = {}
    if count:
        all_elapsed = int(aggregate["elapsed_count"]) == count
        bucket_count = min(max_plot_points // 2, count)
        cursor = connection.execute(
            f"""SELECT sample_index, elapsed_seconds, value FROM measurements
                WHERE benchmark_id=? AND metric IN ({placeholders})
                  AND stream_scope='capture' AND value >= 0
                ORDER BY sample_index IS NULL, sample_index, observed_at, id""",
            params,
        )
        for ordinal, sample in enumerate(cursor):
            x = (
                float(sample["elapsed_seconds"])
                if all_elapsed and sample["elapsed_seconds"] is not None
                else float(sample["sample_index"] if sample["sample_index"] is not None else ordinal)
            )
            value = float(sample["value"])
            bucket = min(bucket_count - 1, ordinal * bucket_count // count)
            point = (ordinal, x, value)
            pair = points_by_bucket.get(bucket)
            if pair is None:
                points_by_bucket[bucket] = [point, point]
            else:
                if value < pair[0][2]:
                    pair[0] = point
                if value > pair[1][2]:
                    pair[1] = point
    minimum = float(aggregate["minimum"]) if aggregate["minimum"] is not None else None
    maximum = float(aggregate["maximum"]) if aggregate["maximum"] is not None else None
    mean = float(aggregate["mean"]) if aggregate["mean"] is not None else None
    median = _generic_percentile(connection, benchmark_id, _CAPTURE_FPS_METRICS, count, 0.5, median=True)
    if count:
        bounded = {
            point[0]: point
            for pair in points_by_bucket.values()
            for point in pair
        }
        points = [bounded[index] for index in sorted(bounded)]
        x_label = "Recorder elapsed time (s)" if int(aggregate["elapsed_count"]) == count else "Sample index"
        method = "Stored capture-FPS samples; sampling cadence and window are defined by the source recording."
        return MetricStats(count, minimum, maximum, mean, median), tuple((x, y) for _, x, y in points), x_label, method

    saved = row["capture_fps"]
    if saved is not None:
        return (
            MetricStats(1, mean=float(saved), median=None, from_saved_summary=True),
            (),
            "Elapsed time from recorded intervals (s)",
            "Only a saved session capture-FPS summary is available; no time series was persisted.",
        )
    return (
        MetricStats(0),
        (),
        "Elapsed time from recorded intervals (s)",
        "No capture-scoped frame intervals or capture-FPS samples were stored.",
    )


def _generic_percentile(
    connection: Any,
    benchmark_id: str,
    metrics: Iterable[str],
    count: int,
    fraction: float,
    *,
    median: bool = False,
) -> float | None:
    if count < 1:
        return None
    names = tuple(metrics)
    placeholders = ",".join("?" for _ in names)
    offsets = ((count - 1) // 2, count // 2) if median else (max(0, math.ceil(count * fraction) - 1),)
    values = []
    for offset in offsets:
        result = connection.execute(
            f"""SELECT value FROM measurements WHERE benchmark_id=? AND metric IN ({placeholders})
                AND stream_scope='capture' AND value >= 0
                ORDER BY value LIMIT 1 OFFSET ?""",
            (benchmark_id, *names, offset),
        ).fetchone()
        if result:
            values.append(float(result["value"]))
    return sum(values) / len(values) if values else None


def _mean_metric(connection: Any, benchmark_id: str, metrics: tuple[str, ...], scope: str) -> float | None:
    placeholders = ",".join("?" for _ in metrics)
    row = connection.execute(
        f"""SELECT AVG(value) FROM measurements WHERE benchmark_id=?
            AND metric IN ({placeholders}) AND stream_scope=? AND value >= 0""",
        (benchmark_id, *metrics, scope),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _saved_interval_stats(row: Any) -> MetricStats:
    count = int(row["interval_count"] or 0)
    if not count:
        return MetricStats(0)
    return MetricStats(
        count=count,
        median=float(row["median_interval_ms"]) if row["median_interval_ms"] is not None else None,
        p95=float(row["p95_interval_ms"]) if row["p95_interval_ms"] is not None else None,
        p99=float(row["p99_interval_ms"]) if row["p99_interval_ms"] is not None else None,
        from_saved_summary=True,
    )


def _reported_interval_summary(connection: Any, benchmark_id: str) -> dict[str, MetricStats]:
    labels = {
        "frame_interval_median_ms": "Median interval field",
        "frame_interval_p95_ms": "p95 interval field",
        "frame_interval_p99_ms": "p99 interval field",
    }
    result: dict[str, MetricStats] = {}
    for metric, label in labels.items():
        row = connection.execute(
            """SELECT COUNT(*) AS n, MIN(value) AS minimum, MAX(value) AS maximum,
                      AVG(value) AS mean
               FROM measurements WHERE benchmark_id=? AND metric=? AND value >= 0""",
            (benchmark_id, metric),
        ).fetchone()
        count = int(row["n"] or 0)
        if count:
            result[label] = MetricStats(
                count,
                minimum=float(row["minimum"]),
                maximum=float(row["maximum"]),
                mean=float(row["mean"]),
                from_saved_summary=True,
            )
    return result


def _frame_count(connection: Any, benchmark_id: str, row: Any) -> tuple[int | None, str | None]:
    if row["frame_count"] is not None:
        return int(row["frame_count"]), "Saved benchmark summary"
    arrival_count = connection.execute(
        """SELECT COUNT(*) FROM measurements
           WHERE benchmark_id=? AND metric='frame_arrival' AND stream_scope='capture'""",
        (benchmark_id,),
    ).fetchone()[0]
    if arrival_count:
        return int(arrival_count), "Stored host frame-arrival records"
    reported = connection.execute(
        """SELECT MAX(value) FROM measurements
           WHERE benchmark_id=? AND metric='frame_count' AND value >= 0""",
        (benchmark_id,),
    ).fetchone()[0]
    return (int(reported), "Maximum imported frame-count field") if reported is not None else (None, None)


def _failure_count(connection: Any, benchmark_id: str, row: Any) -> tuple[int | None, str | None]:
    if row["read_failures"] is not None:
        return int(row["read_failures"]), "Saved benchmark summary"
    events = connection.execute(
        """SELECT COUNT(*), SUM(value) FROM measurements
           WHERE benchmark_id=? AND metric='read_failure' AND stream_scope='capture'""",
        (benchmark_id,),
    ).fetchone()
    if int(events[0] or 0):
        return int(round(float(events[1] or 0))), "Stored capture read-failure events"
    legacy = connection.execute(
        """SELECT MAX(value) FROM measurements
           WHERE benchmark_id=? AND metric='read_failure_count' AND value >= 0""",
        (benchmark_id,),
    ).fetchone()[0]
    if legacy is not None:
        return int(legacy), "Maximum imported failure-field value; legacy total semantics are not guaranteed"
    return None, None


def _duration(
    connection: Any, benchmark_id: str, row: Any, summary: dict[str, Any]
) -> tuple[float | None, str | None]:
    if row["duration_seconds"] is not None:
        source = str(summary.get("duration_source") or "Recorder monotonic timer")
        if source == "last_committed_sample":
            source = "Last committed sample (lower bound after restart recovery)"
        if row["source"] == "imported" and not summary.get("duration_source"):
            source = "Stored imported duration field; timing source unavailable"
        return max(0.0, float(row["duration_seconds"])), source
    if row["source"] == "imported" and row["benchmark_started_at"] and row["benchmark_ended_at"]:
        try:
            started = datetime.fromisoformat(str(row["benchmark_started_at"]))
            ended = datetime.fromisoformat(str(row["benchmark_ended_at"]))
            seconds = (ended - started).total_seconds()
        except (TypeError, ValueError):
            seconds = -1.0
        if seconds >= 0:
            return seconds, "Imported first-to-last timestamp span; may not cover the full test"
    if row["source"] == "live_experiment":
        last_elapsed = connection.execute(
            "SELECT MAX(elapsed_seconds) FROM measurements WHERE benchmark_id=?",
            (benchmark_id,),
        ).fetchone()[0]
        if last_elapsed is not None:
            return max(0.0, float(last_elapsed)), "Last stored elapsed sample (lower bound)"
    return None, None


def _timing_method(connection: Any, benchmark_id: str, source: _IntervalSource | None) -> str | None:
    if source is None:
        return None
    row = connection.execute(
        """SELECT method FROM measurements WHERE benchmark_id=? AND metric=?
           AND stream_scope=? AND method IS NOT NULL ORDER BY sample_index LIMIT 1""",
        (benchmark_id, source.metric, source.scope),
    ).fetchone()
    if row and row["method"]:
        return str(row["method"])
    if source.metric == "frame_arrival":
        return "Host monotonic timestamp immediately after OpenCV VideoCapture.read returned."
    return None


def _first_method(connection: Any, benchmark_id: str, source: _IntervalSource) -> str | None:
    row = connection.execute(
        """SELECT method FROM measurements WHERE benchmark_id=? AND metric=?
           AND stream_scope=? AND method IS NOT NULL ORDER BY sample_index LIMIT 1""",
        (benchmark_id, source.metric, source.scope),
    ).fetchone()
    return str(row["method"]) if row and row["method"] else None


def _make_histogram(
    counts: list[int], total: int, zero_count: int, underflow: int, overflow: int
) -> Histogram:
    if total <= 0:
        return Histogram(zero_sample_count=zero_count)
    log_span = math.log(HISTOGRAM_MAX_MS / HISTOGRAM_MIN_MS)
    centers = tuple(
        HISTOGRAM_MIN_MS * math.exp(log_span * (index + 0.5) / HISTOGRAM_BINS)
        for index in range(HISTOGRAM_BINS)
    )
    nonzero = [(center, count / total) for center, count in zip(centers, counts) if count]
    return Histogram(
        centers_ms=tuple(point[0] for point in nonzero),
        fractions=tuple(point[1] for point in nonzero),
        positive_sample_count=total,
        zero_sample_count=zero_count,
        underflow_count=underflow,
        overflow_count=overflow,
    )


def _fps_window_method(all_have_elapsed: bool, window_width: float, max_points: int) -> str:
    time_basis = "recorder elapsed time" if all_have_elapsed else "cumulative recorded intervals"
    return (
        "Capture FPS is calculated in non-overlapping endpoint windows: interval-transition count divided by "
        "the summed recorded interval duration. Windows containing only zero-duration intervals have no rate. "
        "The target window is 1 s; it widens for long "
        f"recordings to cap the curve at {max_points} points (current width {window_width:.3g} s). "
        "Windows are grouped by "
        f"{time_basis}. Each point is a window rate, not an instantaneous or sensor rate."
    )


def _json_object(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: object) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)


def _capture_configuration(configuration: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    requested_raw = configuration.get("requested")
    reported_raw = configuration.get("driver_reported")
    requested = dict(requested_raw) if isinstance(requested_raw, dict) else {}
    reported = dict(reported_raw) if isinstance(reported_raw, dict) else {}
    if not requested and not reported and configuration.get("legacy_mode"):
        return {}, {}, f"Imported mode: {configuration['legacy_mode']}"
    return requested, reported, None


def _requested_fps(settings: dict[str, Any]) -> float | None:
    for key in ("requested_fps", "fps", "frame_rate"):
        value = settings.get(key)
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result) and result > 0:
            return result
    return None


def _mode_filter_label(
    requested: dict[str, Any], reported: dict[str, Any], configuration_note: str | None
) -> str:
    settings = requested or reported
    dimensions = []
    width, height = settings.get("width"), settings.get("height")
    if width is not None and height is not None:
        dimensions.append(f"{width}×{height}")
    pixel_format = settings.get("pixel_format") or settings.get("format") or settings.get("fourcc")
    if pixel_format:
        dimensions.append(str(pixel_format))
    fps = _requested_fps(requested)
    if fps is not None:
        dimensions.append(f"{fps:g} FPS requested")
    if dimensions:
        source = "requested" if requested else "driver-reported"
        return f"{source} · " + " · ".join(dimensions)
    if configuration_note:
        return "legacy · " + configuration_note.removeprefix("Imported mode: ")
    return "Mode unavailable"


def _camera_key_fallback(camera: str) -> str:
    return "camera-name:" + " ".join(camera.casefold().split())


def _empty_analysis(
    option: ExperimentOption,
    notes: str | None,
    requested: dict[str, Any],
    reported: dict[str, Any],
    configuration_note: str | None,
) -> ExperimentAnalysis:
    x_label = "Elapsed time from recorded intervals (s)"
    return ExperimentAnalysis(
        option=option,
        notes=notes,
        requested_settings=requested,
        driver_settings=reported,
        configuration_note=configuration_note,
        duration_seconds=None,
        duration_source=None,
        frame_count=None,
        frame_count_source=None,
        read_failures=None,
        failure_source=None,
        preview_fps_mean=None,
        processing_fps_mean=None,
        interval_stats=MetricStats(0),
        interval_points=(),
        interval_x_label=x_label,
        interval_metric=None,
        interval_scope=None,
        interval_method=None,
        interval_histogram=Histogram(),
        reported_interval_summary={},
        capture_fps_stats=MetricStats(0),
        fps_points=(),
        fps_x_label=x_label,
        fps_method="No benchmark run or timing samples are attached to this history entry.",
        timing_method=None,
        saved_status=option.status,
        termination_reason=None,
        interval_data_available=False,
    )
