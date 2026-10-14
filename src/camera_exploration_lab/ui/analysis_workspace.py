"""Asynchronous, read-only experiment analysis and comparison workspace."""

from __future__ import annotations

import logging
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analysis import (
    ExperimentAnalysis,
    ExperimentOption,
    compare_methodology,
    list_experiments,
    load_analysis,
    load_comparison,
)
from ..db import Database

logger = logging.getLogger(__name__)


class _TaskSignals(QObject):
    completed = Signal(int, object)
    failed = Signal(int, str)
    done = Signal(int)


class _AnalysisTask(QRunnable):
    def __init__(
        self,
        request_id: int,
        operation: str,
        database: Database,
        primary: tuple[str | None, str] | None = None,
        comparison: tuple[str | None, str] | None = None,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.database = database
        self.primary = primary
        self.comparison = comparison
        self.signals = _TaskSignals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            if self.operation == "list":
                result: object = list_experiments(self.database)
            else:
                assert self.primary is not None
                if self.comparison is not None:
                    result = load_comparison(
                        self.database, self.primary, self.comparison
                    )
                else:
                    result = load_analysis(
                        self.database, self.primary[0], self.primary[1]
                    )
            self.signals.completed.emit(self.request_id, result)
        except Exception as exc:
            logger.exception("Analysis request failed")
            self.signals.failed.emit(self.request_id, str(exc))
        finally:
            self.signals.done.emit(self.request_id)


class AnalysisWorkspace(QWidget):
    """History-backed analysis with bounded plots and a focused two-run comparison."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._database = database
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._request_id = 0
        self._tasks: dict[int, _AnalysisTask] = {}
        self._options: list[ExperimentOption] = []
        self._pending_selection: tuple[str, str | None] | None = None
        self._primary: ExperimentAnalysis | None = None
        self._comparison: ExperimentAnalysis | None = None
        self._build_ui()
        self.refresh_experiments()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.experiment_combo = QComboBox()
        self.experiment_combo.setMinimumWidth(350)
        self.experiment_combo.setToolTip("Select a saved live recording or imported history run.")
        self.compare_combo = QComboBox()
        self.compare_combo.setMinimumWidth(300)
        self.compare_combo.setToolTip("Optionally compare this experiment with a second saved run.")
        self.refresh_button = QPushButton("Refresh history")
        self.refresh_button.clicked.connect(self.refresh_experiments)
        controls.addWidget(QLabel("Experiment"))
        controls.addWidget(self.experiment_combo, 3)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Compare with"))
        controls.addWidget(self.compare_combo, 2)
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)

        self.status_label = QLabel("Loading saved experiments…")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.status_label)

        self.metadata_label = QLabel("Select a saved experiment to inspect its recorded settings and timing data.")
        self.metadata_label.setWordWrap(True)
        self.metadata_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata_label.setToolTip(
            "Requested settings and driver-reported settings are shown separately. "
            "The driver readback does not guarantee the delivered frame rate."
        )
        layout.addWidget(self.metadata_label)

        upper = QSplitter(Qt.Orientation.Horizontal)
        self.interval_plot = self._make_plot(
            "Capture frame intervals",
            "Frame interval (ms)",
            "Elapsed time (s)",
            "Recorded intervals are host-side differences between successive frame-arrival observations.",
        )
        self.fps_plot = self._make_plot(
            "Capture FPS over time",
            "Capture FPS (frames/s)",
            "Elapsed time (s)",
            "Derived from capture-scoped frame intervals in non-overlapping time windows. "
            "This is separate from the GUI preview update rate.",
        )
        upper.addWidget(self.interval_plot)
        upper.addWidget(self.fps_plot)
        upper.setStretchFactor(0, 1)
        upper.setStretchFactor(1, 1)

        lower = QSplitter(Qt.Orientation.Horizontal)
        comparison_group = QGroupBox("Two-experiment comparison")
        comparison_layout = QVBoxLayout(comparison_group)
        comparison_layout.setContentsMargins(8, 8, 8, 8)
        self.comparison_notice = QLabel(
            "Choose a second experiment to compare frame-interval distributions and summary metrics."
        )
        self.comparison_notice.setWordWrap(True)
        self.comparison_notice.setToolTip(
            "Timing methods and data scope are compared explicitly. Requested camera FPS is not delivered FPS."
        )
        comparison_layout.addWidget(self.comparison_notice)
        self.comparison_table = QTableWidget(0, 4)
        self.comparison_table.setHorizontalHeaderLabels(
            ("Metric", "Selected experiment", "Compared experiment", "Difference (compared − selected)")
        )
        self.comparison_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.comparison_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.comparison_table.verticalHeader().setVisible(False)
        self.comparison_table.horizontalHeader().setStretchLastSection(True)
        self.comparison_table.setMinimumHeight(180)
        comparison_layout.addWidget(self.comparison_table, 1)
        self.distribution_plot = self._make_plot(
            "Frame-interval distribution",
            "Fraction of positive intervals per bin",
            "Frame interval (ms, logarithmic)",
            "A normalized histogram with shared logarithmic bins from 0.001 to 10,000,000 ms. "
            "Values outside that range are shown in edge bins; the summary keeps their actual min/max. "
            "Percentiles use all stored intervals.",
        )
        self.distribution_plot.setLogMode(x=True, y=False)
        lower.addWidget(comparison_group)
        lower.addWidget(self.distribution_plot)
        lower.setStretchFactor(0, 3)
        lower.setStretchFactor(1, 2)

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(upper)
        vertical.addWidget(lower)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        layout.addWidget(vertical, 1)

        self.experiment_combo.currentIndexChanged.connect(self._selection_changed)
        self.compare_combo.currentIndexChanged.connect(self._selection_changed)

    @staticmethod
    def _make_plot(title: str, y_label: str, x_label: str, tooltip: str) -> pg.PlotWidget:
        plot = pg.PlotWidget()
        plot.setBackground(None)
        plot.showGrid(x=True, y=True, alpha=0.22)
        plot.setTitle(title)
        plot.setLabel("left", y_label)
        plot.setLabel("bottom", x_label)
        plot.setToolTip(tooltip)
        plot.setMinimumHeight(190)
        return plot

    def refresh_experiments(self) -> None:
        self.status_label.setStyleSheet("color: palette(mid);")
        self.status_label.setText("Reading saved experiment index…")
        self._submit("list")

    def select_experiment(self, session_id: str, benchmark_id: str | None) -> None:
        """Select a History row and load it, even if the asynchronous index is pending."""
        self._pending_selection = (session_id, benchmark_id)
        if self._options:
            self._select_option(self.experiment_combo, session_id, benchmark_id)
            self._load_current_selection()

    def _submit(
        self,
        operation: str,
        primary: tuple[str | None, str] | None = None,
        comparison: tuple[str | None, str] | None = None,
    ) -> None:
        self._request_id += 1
        request_id = self._request_id
        task = _AnalysisTask(request_id, operation, self._database, primary, comparison)
        task.signals.completed.connect(self._task_completed)
        task.signals.failed.connect(self._task_failed)
        task.signals.done.connect(self._task_done)
        self._tasks[request_id] = task
        self._pool.start(task)

    def _task_completed(self, request_id: int, result: object) -> None:
        if request_id != self._request_id:
            return
        task = self._tasks.get(request_id)
        if task is None:
            return
        if task.operation == "list":
            self._populate_options(result if isinstance(result, list) else [])
            if not self._options:
                self.status_label.setText("No saved sessions are available in this local history database.")
                self.metadata_label.setText("Record an experiment or import a historical CSV to begin analysis.")
                return
            self.status_label.setText(f"{len(self._options)} saved experiment(s) available.")
            target = self._pending_selection
            if target:
                self._select_option(self.experiment_combo, target[0], target[1])
                self._pending_selection = None
            elif self.experiment_combo.count() and self.experiment_combo.currentIndex() < 0:
                self.experiment_combo.setCurrentIndex(0)
            self._load_current_selection()
            return
        if isinstance(result, ExperimentAnalysis):
            self._render(result, None)
        elif (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], ExperimentAnalysis)
            and isinstance(result[1], ExperimentAnalysis)
        ):
            self._render(result[0], result[1])

    def _task_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.status_label.setText(f"Analysis could not be loaded: {message}")
            self.status_label.setStyleSheet("color: #a33;")

    def _task_done(self, request_id: int) -> None:
        self._tasks.pop(request_id, None)

    def _populate_options(self, options: list[ExperimentOption]) -> None:
        prior_primary = self._combo_identity(self.experiment_combo)
        prior_compare = self._combo_identity(self.compare_combo)
        self._options = options
        self.experiment_combo.blockSignals(True)
        self.compare_combo.blockSignals(True)
        self.experiment_combo.clear()
        self.compare_combo.clear()
        self.compare_combo.addItem("No comparison", None)
        for option in options:
            self.experiment_combo.addItem(self._option_label(option), (option.session_id, option.benchmark_id))
            self.compare_combo.addItem(self._option_label(option), (option.session_id, option.benchmark_id))
        self._restore_combo(self.experiment_combo, prior_primary)
        self._restore_combo(self.compare_combo, prior_compare)
        self.experiment_combo.blockSignals(False)
        self.compare_combo.blockSignals(False)

    def _selection_changed(self, _index: int = -1) -> None:
        self._load_current_selection()

    def _load_current_selection(self) -> None:
        primary = self.experiment_combo.currentData(Qt.ItemDataRole.UserRole)
        comparison = self.compare_combo.currentData(Qt.ItemDataRole.UserRole)
        if not primary:
            return
        self.status_label.setStyleSheet("color: palette(mid);")
        self.status_label.setText("Loading stored timings in the background…")
        self._submit(
            "analysis",
            (primary[1], primary[0]),
            (comparison[1], comparison[0]) if comparison else None,
        )

    def _render(
        self, primary: ExperimentAnalysis, comparison: ExperimentAnalysis | None
    ) -> None:
        self._primary = primary
        self._comparison = comparison
        self.status_label.setStyleSheet("color: palette(mid);")
        self.status_label.setText(
            f"{primary.saved_status.capitalize()} · "
            f"{primary.interval_stats.count:,} stored frame interval sample(s)"
            if primary.interval_data_available
            else f"{primary.saved_status.capitalize()} · raw frame-interval series unavailable"
        )
        self.metadata_label.setText(self._metadata_text(primary, comparison))
        self._draw_interval_plot(primary)
        self._draw_fps_plot(primary)
        self._draw_comparison(primary, comparison)

    def _draw_interval_plot(self, analysis: ExperimentAnalysis) -> None:
        self.interval_plot.clear()
        plot = self.interval_plot.plotItem
        plot.setLabel("bottom", analysis.interval_x_label)
        plot.addLegend(offset=(10, 8))
        if analysis.interval_points:
            x_values, y_values = zip(*analysis.interval_points)
            self.interval_plot.plot(
                x_values, y_values,
                pen=pg.mkPen("#167d9a", width=1.35),
                name="Recorded frame intervals",
            )
        stats = analysis.interval_stats
        scope = "capture" if analysis.interval_scope == "capture" else (
            "unknown scope" if analysis.interval_scope == "unknown" else "stored"
        )
        if stats.count:
            plot.setTitle(
                f"{scope.capitalize()} frame intervals · n={stats.count:,} · min {_format_ms(stats.minimum)} · "
                f"median {_format_ms(stats.median)} · p95 {_format_ms(stats.p95)} · "
                f"p99 {_format_ms(stats.p99)} · max {_format_ms(stats.maximum)}"
            )
            if not analysis.interval_points:
                plot.setTitle("Saved interval summary · raw timing series unavailable")
        else:
            plot.setTitle("Capture frame intervals · no raw interval samples")

    def _draw_fps_plot(self, analysis: ExperimentAnalysis) -> None:
        self.fps_plot.clear()
        plot = self.fps_plot.plotItem
        plot.setLabel("bottom", analysis.fps_x_label)
        plot.addLegend(offset=(10, 8))
        if analysis.fps_points:
            x_values, y_values = zip(*analysis.fps_points)
            plot.plot(
                x_values, y_values,
                pen=pg.mkPen("#b66518", width=1.5),
                symbol="o" if len(x_values) < 120 else None,
                symbolSize=4,
                name="Derived capture FPS",
            )
            stats = analysis.capture_fps_stats
            plot.setTitle(
                f"Capture FPS · mean {_format_fps(stats.mean)} · median {_format_fps(stats.median)}"
            )
        else:
            plot.setTitle("Capture FPS · no time series available")
        plot.setToolTip(analysis.fps_method)

    def _draw_comparison(
        self, primary: ExperimentAnalysis, comparison: ExperimentAnalysis | None
    ) -> None:
        self.comparison_table.setRowCount(0)
        self.distribution_plot.clear()
        if comparison is None:
            self.comparison_notice.setText(
                "Choose a second experiment to compare frame-interval distributions and summary metrics."
            )
            return

        notices = compare_methodology(primary, comparison)
        notice_lines = [
            ("⚠ " if notice.is_warning else "✓ ") + notice.message for notice in notices
        ]
        if any(
            analysis.interval_histogram.underflow_count or analysis.interval_histogram.overflow_count
            for analysis in (primary, comparison)
        ):
            notice_lines.append(
                "Histogram edge bins include out-of-range values; interval min/max and percentiles retain the actual values."
            )
        self.comparison_notice.setText("\n".join(notice_lines))
        self.comparison_notice.setStyleSheet(
            "color: #9b5c00;" if any(notice.is_warning for notice in notices) else "color: #26734d;"
        )
        rows = self._comparison_rows(primary, comparison)
        self.comparison_table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.comparison_table.setItem(row_index, column, item)
        for column, width in enumerate((190, 160, 160)):
            self.comparison_table.setColumnWidth(column, width)
        self._draw_histograms(primary, comparison)

    @staticmethod
    def _comparison_rows(
        left: ExperimentAnalysis, right: ExperimentAnalysis
    ) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = [
            ("Camera model", left.option.camera, right.option.camera, ""),
            ("Capture mode", _mode_text(left), _mode_text(right), ""),
            ("Capture FPS mean", _format_fps(left.capture_fps_stats.mean), _format_fps(right.capture_fps_stats.mean),
             _delta(left.capture_fps_stats.mean, right.capture_fps_stats.mean, " frames/s")),
            ("Capture FPS median", _format_fps(left.capture_fps_stats.median), _format_fps(right.capture_fps_stats.median),
             _delta(left.capture_fps_stats.median, right.capture_fps_stats.median, " frames/s")),
            ("Interval samples", _format_count(left.interval_stats.count), _format_count(right.interval_stats.count), ""),
        ]
        for label, field in (
            ("Minimum interval", "minimum"),
            ("Median interval", "median"),
            ("p95 interval", "p95"),
            ("p99 interval", "p99"),
            ("Maximum interval", "maximum"),
        ):
            left_value = getattr(left.interval_stats, field)
            right_value = getattr(right.interval_stats, field)
            rows.append((
                label,
                _format_ms(left_value),
                _format_ms(right_value),
                _delta(left_value, right_value, " ms"),
            ))
        rows.extend((
            ("Frame count", _format_count(left.frame_count), _format_count(right.frame_count), ""),
            ("Capture read failures", _format_count(left.read_failures), _format_count(right.read_failures), ""),
            ("Session duration", _format_duration(left.duration_seconds), _format_duration(right.duration_seconds), ""),
            ("Preview FPS mean (GUI)", _format_fps(left.preview_fps_mean), _format_fps(right.preview_fps_mean), ""),
        ))
        return rows

    def _draw_histograms(self, left: ExperimentAnalysis, right: ExperimentAnalysis) -> None:
        plot = self.distribution_plot.plotItem
        plot.setTitle("Normalized frame-interval distributions")
        plot.addLegend(offset=(10, 8))
        for analysis, color in ((left, "#167d9a"), (right, "#b66518")):
            histogram = analysis.interval_histogram
            if histogram.centers_ms:
                plot.plot(
                    histogram.centers_ms,
                    histogram.fractions,
                    pen=pg.mkPen(color, width=1.8),
                    name=f"{analysis.option.camera} · {analysis.option.label}",
                )
        if not left.interval_histogram.centers_ms and not right.interval_histogram.centers_ms:
            plot.setTitle("Frame-interval distribution unavailable for both experiments")

    def _metadata_text(
        self, analysis: ExperimentAnalysis, comparison: ExperimentAnalysis | None
    ) -> str:
        lines = [
            f"<b>{_html(analysis.option.label)}</b> · {_html(analysis.option.camera)}",
            f"Source: {_html(analysis.option.source)} · Status: {_html(analysis.saved_status)} · "
            f"Started: {_html(analysis.option.started_at or 'not recorded')}",
            f"Duration: {_format_duration(analysis.duration_seconds)}"
            + (f" ({_html(analysis.duration_source)})" if analysis.duration_source else "")
            + f" · Frames: {_format_count(analysis.frame_count)}"
            + (f" · Capture read failures: {_format_count(analysis.read_failures)}" if analysis.read_failures is not None else ""),
            f"Capture settings requested: {_html(_settings_text(analysis.requested_settings))}",
            f"Driver-reported settings: {_html(_settings_text(analysis.driver_settings))}",
            f"Capture FPS mean / median: {_format_fps(analysis.capture_fps_stats.mean)} / "
            f"{_format_fps(analysis.capture_fps_stats.median)} · GUI preview FPS mean: "
            f"{_format_fps(analysis.preview_fps_mean)} · Processing FPS: "
            f"{_format_fps(analysis.processing_fps_mean)}",
        ]
        if analysis.configuration_note:
            lines.append(_html(analysis.configuration_note))
        if analysis.option.source == "imported":
            lines.append(
                "Imported history may have sparse timestamps or summary-only fields; unavailable values are left blank."
            )
        if analysis.option.source == "live_experiment":
            lines.append(
                "Host frame-arrival timestamps are taken immediately after OpenCV read returns. "
                "UTC observed_at and recorder elapsed timestamps are stored with timing rows. "
                "Frame intervals are not sensor exposure or sensor-to-display latency."
            )
        if analysis.interval_scope == "unknown":
            lines.append("The stored frame-interval scope is unknown; no capture FPS was derived from it.")
        if analysis.interval_stats.count:
            saved_label = " · saved summary only" if analysis.interval_stats.from_saved_summary else ""
            lines.append(
                f"Frame intervals: n={analysis.interval_stats.count:,}; "
                f"min {_format_ms(analysis.interval_stats.minimum)}; "
                f"median {_format_ms(analysis.interval_stats.median)}; "
                f"p95 {_format_ms(analysis.interval_stats.p95)}; "
                f"p99 {_format_ms(analysis.interval_stats.p99)}; "
                f"max {_format_ms(analysis.interval_stats.maximum)}{saved_label}"
            )
        if analysis.reported_interval_summary:
            reported = "; ".join(
                f"{name}: mean {_format_ms(values.mean)}, range "
                f"{_format_ms(values.minimum)}–{_format_ms(values.maximum)} "
                f"across {values.count} stored field sample(s)"
                for name, values in analysis.reported_interval_summary.items()
            )
            lines.append(
                "Imported summary fields (stored values, not recalculated from raw intervals): "
                + _html(reported)
            )
        if analysis.timing_method:
            lines.append(f"Timing method: {_html(analysis.timing_method)}")
        if analysis.fps_method:
            lines.append(f"FPS curve: {_html(analysis.fps_method)}")
        if analysis.failure_source:
            lines.append(f"Failure metric: {_html(analysis.failure_source)}")
        if analysis.frame_count_source:
            lines.append(f"Frame count source: {_html(analysis.frame_count_source)}")
        if analysis.termination_reason:
            lines.append(f"Termination: {_html(analysis.termination_reason)}")
        if analysis.notes:
            lines.extend(("", f"Notes: {_html(analysis.notes)}"))
        if comparison is not None:
            lines.append(
                "Requested camera FPS is a driver request/readback setting; observed capture FPS above is "
                "derived from delivered host frame intervals. GUI preview FPS is a separate render cadence."
            )
        return "<br>".join(lines)

    @staticmethod
    def _option_label(option: ExperimentOption) -> str:
        date = option.started_at or "date unavailable"
        return f"{option.label} · {option.camera} · {date} · {option.status}"

    @staticmethod
    def _combo_identity(combo: QComboBox) -> tuple[str, str | None] | None:
        value = combo.currentData(Qt.ItemDataRole.UserRole)
        return tuple(value) if value else None

    @classmethod
    def _restore_combo(cls, combo: QComboBox, identity: tuple[str, str | None] | None) -> None:
        if identity:
            for index in range(combo.count()):
                if combo.itemData(index, Qt.ItemDataRole.UserRole) == identity:
                    combo.setCurrentIndex(index)
                    return

    @staticmethod
    def _select_option(combo: QComboBox, session_id: str, benchmark_id: str | None) -> None:
        for index in range(combo.count()):
            if combo.itemData(index, Qt.ItemDataRole.UserRole) == (session_id, benchmark_id):
                combo.setCurrentIndex(index)
                return


def _mode_text(analysis: ExperimentAnalysis) -> str:
    requested = _settings_text(analysis.requested_settings)
    reported = _settings_text(analysis.driver_settings)
    if requested and reported:
        return f"requested {requested}; driver {reported}"
    return requested or reported or analysis.configuration_note or "Mode unavailable"


def _settings_text(settings: dict[str, Any]) -> str:
    if not settings:
        return "not recorded"
    preferred = (
        "width", "height", "pixel_format", "format", "fourcc",
        "fps", "frame_rate", "requested_fps", "reported_fps",
    )
    pairs = [(key, settings[key]) for key in preferred if key in settings]
    if not pairs:
        pairs = list(settings.items())[:8]
    return ", ".join(f"{key}={value}" for key, value in pairs)


def _format_fps(value: float | None) -> str:
    return f"{value:.2f} frames/s" if value is not None else "not available"


def _format_ms(value: float | None) -> str:
    return f"{value:.3f} ms" if value is not None else "not available"


def _format_duration(value: float | None) -> str:
    return f"{value:.3f} s" if value is not None else "not available"


def _format_count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "not available"


def _delta(left: float | None, right: float | None, suffix: str) -> str:
    return f"{right - left:+.3f}{suffix}" if left is not None and right is not None else ""


def _html(value: object) -> str:
    return (
        str(value).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
