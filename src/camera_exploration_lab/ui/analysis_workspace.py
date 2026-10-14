"""Asynchronous, read-only experiment analysis and comparison workspace."""

from __future__ import annotations

import logging
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis import (
    ExperimentAnalysis,
    ExperimentOption,
    compare_methodology,
    filter_experiments,
    list_experiments,
    load_analysis,
    load_comparison,
    rate_deviation_percent,
)
from ..db import Database
from .metrics_guide import MetricsGuideDialog

logger = logging.getLogger(__name__)


def contrast_ratio(foreground: QColor, background: QColor) -> float:
    """WCAG contrast ratio for opaque sRGB colors."""
    def luminance(color: QColor) -> float:
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                  for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)


class _RateAxisItem(pg.AxisItem):
    """Format normalized FPS deviations as readable percentages."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.percent_mode = False
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph API
        if not self.percent_mode:
            return super().tickStrings(values, scale, spacing)
        labels = []
        for value in values:
            absolute = abs(float(value))
            if absolute < 0.01:
                rendered = "0"
            elif absolute < 1:
                rendered = f"{value:.2f}"
            elif absolute < 100:
                rendered = f"{value:.1f}".rstrip("0").rstrip(".")
            else:
                rendered = f"{value:.0f}"
            labels.append(f"{rendered}%")
        return labels


class _MetricCard(QFrame):
    help_requested = Signal(str)

    def __init__(
        self, title: str, tooltip: str, topic_key: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._updating_contrast = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setToolTip(tooltip)
        self.topic_key = topic_key
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(3)
        title_row = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.value = QLabel("—")
        self.help_button = QToolButton()
        self.help_button.setText("?")
        self.help_button.setAutoRaise(True)
        self.help_button.setFixedSize(20, 20)
        self.help_button.setToolTip(f"Explain {title}")
        self.help_button.clicked.connect(self._request_help)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.help_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(title_row)
        layout.addWidget(self.value)
        self._update_contrast()

    def _request_help(self, _checked: bool = False) -> None:
        self.help_requested.emit(self.topic_key)

    def _update_contrast(self) -> None:
        if self._updating_contrast:
            return
        self._updating_contrast = True
        try:
            palette = self.palette()
            background = palette.color(QPalette.ColorRole.Button)
            candidates = (
                palette.color(QPalette.ColorRole.Text),
                QColor("#ffffff"),
                QColor("#000000"),
            )
            foreground = max(candidates, key=lambda color: contrast_ratio(color, background))
            self.surface_color = background
            self.foreground_color = foreground
            self.setStyleSheet(f"QFrame {{ background-color: {background.name()}; }}")
            self.title.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {foreground.name()};"
            )
            self.value.setStyleSheet(
                f"font-size: 18px; font-weight: 600; color: {foreground.name()};"
            )
            self.help_button.setStyleSheet(f"color: {foreground.name()};")
        finally:
            self._updating_contrast = False

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._update_contrast()


class _TaskSignals(QObject):
    completed = Signal(int, str, object)
    failed = Signal(int, str)


class _AnalysisTask(QRunnable):
    def __init__(
        self,
        request_id: int,
        operation: str,
        database: Database,
        signals: _TaskSignals,
        primary: tuple[str | None, str] | None = None,
        comparison: tuple[str | None, str] | None = None,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.database = database
        self.signals = signals
        self.primary = primary
        self.comparison = comparison

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
            self.signals.completed.emit(self.request_id, self.operation, result)
        except Exception as exc:
            logger.exception("Analysis request failed")
            self.signals.failed.emit(self.request_id, str(exc))


class AnalysisWorkspace(QWidget):
    """History-backed analysis with bounded plots and a focused two-run comparison."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._database = database
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._request_id = 0
        self._task_signals = _TaskSignals(self)
        self._task_signals.completed.connect(self._task_completed)
        self._task_signals.failed.connect(self._task_failed)
        self._options: list[ExperimentOption] = []
        self._pending_selection: tuple[str, str | None] | None = None
        self._primary: ExperimentAnalysis | None = None
        self._comparison: ExperimentAnalysis | None = None
        self._comparison_cleared = False
        self._updating_selectors = False
        self._shutting_down = False
        self._metrics_dialog: MetricsGuideDialog | None = None
        self._build_ui()
        self.refresh_experiments()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(14, 12, 14, 16)
        page_layout.setSpacing(8)

        filters = QHBoxLayout()
        self.camera_filter = QComboBox()
        self.camera_filter.setMinimumWidth(130)
        self.camera_filter.setToolTip("Filter recordings by stored camera identity.")
        self.mode_filter = QComboBox()
        self.mode_filter.setMinimumWidth(140)
        self.mode_filter.setToolTip("Filter by stored capture mode.")
        self.fps_filter = QComboBox()
        self.fps_filter.setMinimumWidth(115)
        self.fps_filter.setToolTip("Filter by requested FPS. This is not measured capture FPS.")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search experiment, date, camera, mode…")
        self.search_edit.setClearButtonEnabled(True)
        self.metrics_guide_button = QPushButton("Metrics Guide")
        self.metrics_guide_button.clicked.connect(self.open_metrics_guide)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_experiments)
        filters.addWidget(QLabel("Camera"))
        filters.addWidget(self.camera_filter, 2)
        filters.addWidget(QLabel("Mode"))
        filters.addWidget(self.mode_filter, 2)
        filters.addWidget(QLabel("Requested FPS"))
        filters.addWidget(self.fps_filter, 1)
        filters.addWidget(self.search_edit, 3)
        filters.addWidget(self.metrics_guide_button)
        filters.addWidget(self.refresh_button)
        page_layout.addLayout(filters)

        selectors = QHBoxLayout()
        self.session_a_combo = QComboBox()
        self.session_a_combo.setMinimumWidth(230)
        self.session_a_combo.setToolTip("Session A; labels include experiment, camera, date, and status.")
        self.session_b_combo = QComboBox()
        self.session_b_combo.setMinimumWidth(230)
        self.session_b_combo.setToolTip(
            "Session B; the same stored camera identity is preferred, then another run in the same model family."
        )
        self.experiment_combo = self.session_a_combo
        self.compare_combo = self.session_b_combo
        self.swap_button = QPushButton("Swap A / B")
        self.clear_comparison_button = QPushButton("Clear B")
        selectors.addWidget(QLabel("Session A"))
        selectors.addWidget(self.session_a_combo, 3)
        selectors.addWidget(QLabel("Session B"))
        selectors.addWidget(self.session_b_combo, 3)
        selectors.addWidget(self.swap_button)
        selectors.addWidget(self.clear_comparison_button)
        page_layout.addLayout(selectors)

        self.status_label = QLabel("Loading saved experiments…")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: palette(mid);")
        page_layout.addWidget(self.status_label)
        self.empty_state = QLabel()
        self.empty_state.setWordWrap(True)
        self.empty_state.setStyleSheet("padding: 8px; border: 1px solid palette(mid); border-radius: 4px;")
        self.empty_state.hide()
        page_layout.addWidget(self.empty_state)

        self.experiment_heading = QLabel("Select a saved experiment")
        self.experiment_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.experiment_heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        page_layout.addWidget(self.experiment_heading)
        header = QHBoxLayout()
        self.header_values: dict[str, QLabel] = {}
        for key, title in (("camera", "Camera"), ("mode", "Capture mode"), ("date", "Date"),
                           ("duration", "Duration"), ("status", "Status")):
            chip = QFrame()
            chip.setFrameShape(QFrame.Shape.StyledPanel)
            chip_layout = QVBoxLayout(chip)
            chip_layout.setContentsMargins(8, 5, 8, 5)
            chip_layout.setSpacing(2)
            caption = QLabel(title)
            caption.setStyleSheet("font-size: 10px; color: palette(mid);")
            value = QLabel("—")
            value.setWordWrap(True)
            value.setMaximumHeight(38)
            value.setToolTip(title)
            chip_layout.addWidget(caption)
            chip_layout.addWidget(value)
            header.addWidget(chip, 1)
            self.header_values[key] = value
        page_layout.addLayout(header)

        metrics = QGridLayout()
        metrics.setSpacing(6)
        self.metric_cards: dict[str, _MetricCard] = {}
        definitions = (
            ("capture_fps", "Capture FPS", "Observed delivered host frame-arrival rate.", "capture_fps"),
            ("preview_fps", "Preview FPS", "GUI preview update cadence, separate from capture FPS.", "preview_fps"),
            ("processing_fps", "Processing FPS", "Optional analysis processing rate, if recorded.", "processing_fps"),
            ("frame_count", "Frame Count", "Recorded frame count when available.", "frame_count"),
            ("median", "Median Interval", "Median analyzed capture interval.", "interval"),
            ("p95", "p95 Interval", "95th percentile of analyzed capture intervals.", "percentiles"),
            ("p99", "p99 Interval", "99th percentile of analyzed capture intervals.", "percentiles"),
            ("failures", "Read Failures", "Capture read failures, when recorded by the source.", "read_failures"),
        )
        for index, (key, title, tooltip, topic_key) in enumerate(definitions):
            card = _MetricCard(title, tooltip, topic_key)
            card.help_requested.connect(self.open_metrics_guide)
            self.metric_cards[key] = card
            metrics.addWidget(card, index // 4, index % 4)
        page_layout.addLayout(metrics)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #8b5e00;")
        self.warning_label.hide()
        page_layout.addWidget(self.warning_label)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Show technical details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_text = QLabel("Requested and driver-reported values are listed separately.")
        self.details_text.setTextFormat(Qt.TextFormat.PlainText)
        self.details_text.setWordWrap(True)
        self.details_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_text.setFrameShape(QFrame.Shape.StyledPanel)
        self.details_text.setContentsMargins(8, 6, 8, 6)
        self.details_text.hide()
        self.metadata_label = self.details_text
        page_layout.addWidget(self.details_toggle)
        page_layout.addWidget(self.details_text)

        chart_options = QHBoxLayout()
        self.fps_deviation_checkbox = QCheckBox("Show deviation from each run's mean (%)")
        self.fps_deviation_checkbox.setToolTip(
            "Each recorded FPS window is shown as a percentage of that session's measured mean. No interpolation is performed."
        )
        self.interval_overlay_checkbox = QCheckBox("Overlay compatible interval timelines")
        self.interval_overlay_checkbox.setToolTip(
            "Requires recorder elapsed timestamps, matching stream scope, and matching timing method."
        )
        chart_options.addWidget(self.fps_deviation_checkbox)
        self.interval_primary_checkbox = QCheckBox("Show Session A intervals")
        self.interval_primary_checkbox.setChecked(True)
        self.interval_primary_checkbox.setToolTip("Show or hide the primary interval trace without changing its stored measurements.")
        chart_options.addWidget(self.interval_primary_checkbox)
        chart_options.addWidget(self.interval_overlay_checkbox)
        chart_options.addStretch(1)
        page_layout.addLayout(chart_options)

        plots = QSplitter(Qt.Orientation.Horizontal)
        self.fps_plot = self._make_plot(
            "Capture FPS vs elapsed time", "Capture FPS (frames/s)", "Recorder elapsed time (s)",
            "Windowed observed capture rate. Samples retain each run's recorded times; curves are not synchronized.",
            percent_axis=True,
        )
        self.interval_plot = self._make_plot(
            "Capture interval timeline", "Frame interval (ms)", "Recorder elapsed time (s)",
            "Host frame-arrival intervals. This is not sensor exposure time or sensor-to-display latency.",
        )
        self.fps_note = QLabel("Select an experiment with recorded capture timing.")
        self.interval_note = QLabel("Select an experiment with recorded frame intervals.")
        plots.addWidget(self._plot_panel("Capture rate", self.fps_plot, self.fps_note))
        plots.addWidget(self._plot_panel("Frame intervals", self.interval_plot, self.interval_note))
        plots.setStretchFactor(0, 3)
        plots.setStretchFactor(1, 2)
        plots.setMinimumHeight(270)

        lower = QSplitter(Qt.Orientation.Horizontal)
        comparison_group = QGroupBox("Session comparison")
        comparison_layout = QVBoxLayout(comparison_group)
        self.comparison_notice = QLabel("Choose Session B to compare two saved recordings.")
        self.comparison_notice.setWordWrap(True)
        self.comparison_notice.setToolTip(
            "Requested FPS, delivered capture FPS, preview FPS, and processing FPS have separate meanings."
        )
        comparison_layout.addWidget(self.comparison_notice)
        self.comparison_table = QTableWidget(0, 4)
        self.comparison_table.setHorizontalHeaderLabels(("Metric", "Session A", "Session B", "B − A"))
        self.comparison_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.comparison_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.comparison_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.comparison_table.verticalHeader().setVisible(False)
        self.comparison_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.comparison_table.horizontalHeader().setMinimumSectionSize(86)
        self.comparison_table.setMinimumHeight(190)
        comparison_layout.addWidget(self.comparison_table, 1)
        self.distribution_plot = self._make_plot(
            "Frame-interval distribution", "Share of positive intervals", "Interval (ms, log scale)",
            "Shared logarithmic bins from 0.001 to 10,000,000 ms. Out-of-range values remain in edge bins; zeros are listed separately.",
        )
        self.distribution_note = QLabel("Choose Session B to compare interval distributions.")
        self.distribution_note.setWordWrap(True)
        lower.addWidget(comparison_group)
        lower.addWidget(self._plot_panel("Interval distribution", self.distribution_plot, self.distribution_note))
        lower.setStretchFactor(0, 3)
        lower.setStretchFactor(1, 2)
        lower.setMinimumHeight(250)

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(plots)
        vertical.addWidget(lower)
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 1)
        page_layout.addWidget(vertical, 1)
        scroll.setWidget(page)
        layout.addWidget(scroll)

        self.camera_filter.currentIndexChanged.connect(self._filters_changed)
        self.mode_filter.currentIndexChanged.connect(self._filters_changed)
        self.fps_filter.currentIndexChanged.connect(self._filters_changed)
        self.search_edit.textChanged.connect(self._filters_changed)
        self.session_a_combo.currentIndexChanged.connect(self._primary_changed)
        self.session_b_combo.currentIndexChanged.connect(self._comparison_changed)
        self.swap_button.clicked.connect(self._swap_sessions)
        self.clear_comparison_button.clicked.connect(self._clear_comparison)
        self.details_toggle.toggled.connect(self._toggle_details)
        self.fps_deviation_checkbox.toggled.connect(self._redraw_comparison_curves)
        self.interval_primary_checkbox.toggled.connect(self._redraw_comparison_curves)
        self.interval_overlay_checkbox.toggled.connect(self._redraw_comparison_curves)

    @staticmethod
    def _plot_panel(title: str, plot: pg.PlotWidget, note: QLabel) -> QWidget:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(4, 3, 4, 3)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 13px; font-weight: 600;")
        panel_layout.addWidget(heading)
        panel_layout.addWidget(plot, 1)
        note.setStyleSheet("font-size: 11px; color: palette(mid);")
        panel_layout.addWidget(note)
        return panel

    def _toggle_details(self, expanded: bool) -> None:
        self.details_text.setVisible(expanded)
        self.details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.details_toggle.setText("Hide technical details" if expanded else "Show technical details")

    @staticmethod
    def _make_plot(
        title: str, y_label: str, x_label: str, tooltip: str, *, percent_axis: bool = False
    ) -> pg.PlotWidget:
        axis = _RateAxisItem(orientation="left") if percent_axis else None
        plot = pg.PlotWidget(axisItems={"left": axis} if axis is not None else None)
        if percent_axis:
            plot._cel_rate_axis = axis
        plot.setBackground(None)
        plot.showGrid(x=True, y=True, alpha=0.22)
        plot.setTitle(title)
        plot.setLabel("left", y_label)
        plot.setLabel("bottom", x_label)
        plot.setToolTip(tooltip)
        plot.setMinimumHeight(190)
        return plot

    def open_metrics_guide(self, topic_key: str | None = None) -> None:
        if self._metrics_dialog is None:
            self._metrics_dialog = MetricsGuideDialog(topic_key, self)
        else:
            self._metrics_dialog.select_topic(topic_key)
        dialog = self._metrics_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def refresh_experiments(self) -> None:
        if self._shutting_down:
            return
        self.status_label.setStyleSheet("color: palette(mid);")
        self.status_label.setText("Reading saved experiment index…")
        self._submit("list")

    def stop_background_work(self) -> None:
        self._shutting_down = True
        self._pool.waitForDone()

    def select_experiment(self, session_id: str, benchmark_id: str | None) -> None:
        """Select a History row and load it, even if the asynchronous index is pending."""
        self._pending_selection = (session_id, benchmark_id)
        if self._options:
            self._reset_filters()
            self._apply_filters(preferred_primary=(session_id, benchmark_id))
            self._pending_selection = None

    def _submit(
        self,
        operation: str,
        primary: tuple[str | None, str] | None = None,
        comparison: tuple[str | None, str] | None = None,
    ) -> None:
        if self._shutting_down:
            return
        self._request_id += 1
        request_id = self._request_id
        task = _AnalysisTask(
            request_id, operation, self._database, self._task_signals, primary, comparison
        )
        self._pool.start(task)

    def _task_completed(self, request_id: int, operation: str, result: object) -> None:
        if self._shutting_down or request_id != self._request_id:
            return
        if operation == "list":
            self._options = result if isinstance(result, list) else []
            self._populate_filter_choices()
            if not self._options:
                self.status_label.setText("No saved sessions are available in this local history database.")
                self._clear_analysis()
                self.experiment_heading.setText("No saved experiments")
                return
            self.status_label.setText(f"{len(self._options)} saved experiment(s) available.")
            target = self._pending_selection
            if target:
                self._reset_filters()
                self._pending_selection = None
            self._apply_filters(preferred_primary=target)
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
        if not self._shutting_down and request_id == self._request_id:
            self.status_label.setText(f"Analysis could not be loaded: {message}")
            self.status_label.setStyleSheet("color: #a33;")

    def _populate_filter_choices(self) -> None:
        camera_value = self.camera_filter.currentData()
        mode_value = self.mode_filter.currentData()
        fps_value = self.fps_filter.currentData()
        for combo in (self.camera_filter, self.mode_filter, self.fps_filter):
            combo.blockSignals(True)
            combo.clear()
        self.camera_filter.addItem("All cameras", None)
        camera_families: dict[str, set[str]] = {}
        camera_names: dict[str, str] = {}
        for option in self._options:
            family_key = option.camera_group_key or option.camera_key
            camera_families.setdefault(family_key, set()).add(option.camera_key)
            camera_names[family_key] = option.camera
        for family_key in sorted(camera_families, key=lambda key: camera_names[key].casefold()):
            name = camera_names[family_key]
            identity_count = len(camera_families[family_key])
            suffix = f" · {identity_count} recorded identity groups" if identity_count > 1 else ""
            self.camera_filter.addItem(name + suffix, family_key)
            if identity_count > 1:
                self.camera_filter.setItemData(
                    self.camera_filter.count() - 1,
                    "This model-family filter includes separate imported and discovered identity records. "
                    "They remain distinct; use the identity badges in the session selectors to review provenance.",
                    Qt.ItemDataRole.ToolTipRole,
                )
        self.mode_filter.addItem("All modes", None)
        for mode in sorted({o.mode_label for o in self._options}, key=str.casefold):
            self.mode_filter.addItem(mode, mode)
        self.fps_filter.addItem("All requested FPS", None)
        values = sorted({o.requested_fps for o in self._options if o.requested_fps is not None})
        for value in values:
            self.fps_filter.addItem(f"{value:g} FPS requested", value)
        self._restore_filter(self.camera_filter, camera_value)
        self._restore_filter(self.mode_filter, mode_value)
        self._restore_filter(self.fps_filter, fps_value)
        for combo in (self.camera_filter, self.mode_filter, self.fps_filter):
            combo.blockSignals(False)

    @staticmethod
    def _restore_filter(combo: QComboBox, value: object) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def _reset_filters(self) -> None:
        for combo in (self.camera_filter, self.mode_filter, self.fps_filter):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._comparison_cleared = False

    def _filters_changed(self, _value: object = None) -> None:
        self._apply_filters()

    def _apply_filters(
        self,
        *,
        preferred_primary: tuple[str, str | None] | None = None,
        preferred_comparison: tuple[str, str | None] | None = None,
    ) -> None:
        if self._updating_selectors:
            return
        previous_primary = preferred_primary or self._combo_identity(self.session_a_combo)
        previous_comparison = preferred_comparison or self._combo_identity(self.session_b_combo)
        fps_value = self.fps_filter.currentData()
        options = filter_experiments(
            self._options,
            camera_group_key=self.camera_filter.currentData(),
            mode_label=self.mode_filter.currentData(),
            requested_fps=fps_value,
            search=self.search_edit.text(),
        )
        self._updating_selectors = True
        self.session_a_combo.clear()
        self.session_b_combo.clear()
        self.session_b_combo.addItem("No comparison", None)
        for option in options:
            identity = (option.session_id, option.benchmark_id)
            self.session_a_combo.addItem(self._option_label(option), identity)
            self.session_a_combo.setItemData(self.session_a_combo.count() - 1, self._option_label(option), Qt.ItemDataRole.ToolTipRole)
        primary_index = self._find_identity(self.session_a_combo, previous_primary)
        if primary_index < 0 and self.session_a_combo.count():
            primary_index = 0
        self.session_a_combo.setCurrentIndex(primary_index)
        primary = self.session_a_combo.currentData()
        primary_option = next(
            (o for o in options if (o.session_id, o.benchmark_id) == primary), None
        )
        eligible = [o for o in options if (o.session_id, o.benchmark_id) != primary]
        for option in eligible:
            identity = (option.session_id, option.benchmark_id)
            self.session_b_combo.addItem(self._option_label(option), identity)
            self.session_b_combo.setItemData(self.session_b_combo.count() - 1, self._option_label(option), Qt.ItemDataRole.ToolTipRole)
        compare_index = self._find_identity(self.session_b_combo, previous_comparison)
        if compare_index < 0 and not self._comparison_cleared and primary_option is not None:
            same_camera = next(
                (i for i, o in enumerate(eligible, start=1) if o.camera_key == primary_option.camera_key),
                -1,
            )
            same_family = next(
                (i for i, o in enumerate(eligible, start=1)
                 if o.camera_group_key == primary_option.camera_group_key),
                -1,
            )
            compare_index = (
                same_camera if same_camera >= 0
                else same_family if same_family >= 0
                else (1 if eligible else 0)
            )
        self.session_b_combo.setCurrentIndex(compare_index if compare_index >= 0 else 0)
        self._updating_selectors = False
        self.empty_state.setVisible(len(options) < 2)
        if not options:
            self.empty_state.setText("No experiments match these filters. Clear the camera, mode, FPS, or search filters.")
        elif len(self._options) < 2:
            self.empty_state.setText("At least two saved experiments are needed for a comparison. Session A analysis remains available.")
        elif len(options) < 2:
            self.empty_state.setText("Only one experiment matches. Clear filters or search to add another comparison candidate.")
        else:
            self.empty_state.clear()
        self._load_current_selection()

    def _primary_changed(self, _index: int = -1) -> None:
        if self._updating_selectors:
            return
        self._comparison_cleared = False
        self._apply_filters(preferred_primary=self._combo_identity(self.session_a_combo))

    def _comparison_changed(self, _index: int = -1) -> None:
        if self._updating_selectors:
            return
        self._comparison_cleared = self.session_b_combo.currentData() is None
        self._load_current_selection()

    def _swap_sessions(self) -> None:
        primary = self._combo_identity(self.session_a_combo)
        comparison = self._combo_identity(self.session_b_combo)
        if primary is None or comparison is None:
            return
        self._comparison_cleared = False
        self._apply_filters(preferred_primary=comparison, preferred_comparison=primary)

    def _clear_comparison(self) -> None:
        self._comparison_cleared = True
        self.session_b_combo.blockSignals(True)
        self.session_b_combo.setCurrentIndex(0)
        self.session_b_combo.blockSignals(False)
        self._load_current_selection()

    def _load_current_selection(self) -> None:
        primary = self.experiment_combo.currentData(Qt.ItemDataRole.UserRole)
        comparison = self.compare_combo.currentData(Qt.ItemDataRole.UserRole)
        if not primary:
            self._clear_analysis()
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
            f"{primary.saved_status.capitalize()} · {primary.interval_stats.count:,} analyzed interval sample(s)"
            if primary.interval_data_available else f"{primary.saved_status.capitalize()} · raw interval series unavailable"
        )
        self._render_header_and_metrics(primary)
        self._render_detail_text(primary)
        self._render_warnings(primary)
        can_overlay = self._can_overlay_intervals(primary, comparison)
        self.interval_overlay_checkbox.setEnabled(can_overlay)
        if not can_overlay:
            self.interval_overlay_checkbox.setChecked(False)
        can_compare_fps = self._can_compare_fps(primary, comparison)
        self.fps_deviation_checkbox.setEnabled(can_compare_fps)
        if not can_compare_fps:
            self.fps_deviation_checkbox.setChecked(False)
        self._draw_interval_plot(primary)
        self._draw_fps_plot(primary, comparison)
        self._draw_comparison(primary, comparison)

    def _draw_interval_plot(self, analysis: ExperimentAnalysis) -> None:
        self.interval_plot.clear()
        plot = self.interval_plot.plotItem
        plot.setLabel("bottom", analysis.interval_x_label)
        series = []
        if self.interval_primary_checkbox.isChecked():
            series.append((analysis, "#167d9a", "A"))
        if (self.interval_overlay_checkbox.isEnabled() and self.interval_overlay_checkbox.isChecked()
                and self._comparison):
            series.append((self._comparison, "#b66518", "B"))
        if any(item.interval_points for item, _color, _side in series):
            plot.addLegend(offset=(10, 8))
        for item, color, side in series:
            if not item.interval_points:
                continue
            x_values, y_values = zip(*item.interval_points)
            line = plot.plot(
                x_values, y_values,
                pen=pg.mkPen(color, width=1.25 if len(x_values) > 500 else 1.7,
                             style=Qt.PenStyle.SolidLine if side == "A" else Qt.PenStyle.DashLine),
                symbol=("o" if side == "A" else "t") if len(x_values) < 140 else None,
                symbolSize=5,
                name=f"Session {side} · {self._short_session(item)}",
            )
            line.setToolTip(self._full_session_label(item))
        if analysis.interval_data_available:
            self.interval_note.setText(
                "Recorder timestamps are host frame-arrival times; intervals are not sensor-to-display latency."
            )
        elif analysis.interval_boundary_samples_excluded:
            self.interval_note.setText(
                "The first CSV 0.000 ms row is retained as source data and excluded from interval statistics because it has no preceding frame."
            )
        elif analysis.interval_stats.from_saved_summary:
            self.interval_note.setText("A saved summary is available; the raw interval timeline was not stored.")
        else:
            self.interval_note.setText("No raw frame-interval timeline is available for this recording.")

    def _draw_fps_plot(
        self, analysis: ExperimentAnalysis, comparison: ExperimentAnalysis | None
    ) -> None:
        self.fps_plot.clear()
        plot = self.fps_plot.plotItem
        plot.setTitle("Capture FPS comparison" if comparison else "Capture FPS over time")
        plot.setLabel("bottom", analysis.fps_x_label)
        percent_axis = self.fps_plot.plotItem.getAxis("left")
        if isinstance(percent_axis, _RateAxisItem):
            percent_axis.percent_mode = self.fps_deviation_checkbox.isChecked()
            percent_axis.picture = None
            percent_axis.update()
        plot.setLabel("left", "Deviation from run mean (%)" if self.fps_deviation_checkbox.isChecked() else "Capture FPS (frames/s)")
        series = [(analysis, "#167d9a", "A")]
        if comparison is not None and self._can_compare_fps(analysis, comparison):
            series.append((comparison, "#b66518", "B"))
            plot.setLabel("bottom", "Recorder elapsed time (s)")
        if any(item.fps_points for item, _color, _side in series):
            plot.addLegend(offset=(10, 8))
        for item, color, side in series:
            points = item.fps_points
            if self.fps_deviation_checkbox.isChecked():
                points = rate_deviation_percent(points, item.capture_fps_stats.mean)
            if not points:
                continue
            x_values, y_values = zip(*points)
            line = plot.plot(
                x_values, y_values,
                pen=pg.mkPen(color, width=1.7, style=Qt.PenStyle.SolidLine if side == "A" else Qt.PenStyle.DashLine),
                symbol=("o" if side == "A" else "t") if len(x_values) < 120 else None,
                symbolSize=5,
                name=f"Session {side} · {self._short_session(item)}",
            )
            line.setToolTip(f"{self._full_session_label(item)}\n{item.fps_method}")
        self.fps_note.setText(
            "Windowed capture rate from stored intervals. Each run keeps its own elapsed-time samples; no interpolation or simultaneity is implied."
            if comparison and self._can_compare_fps(analysis, comparison)
            else analysis.fps_method if not analysis.fps_points else "Capture rate is separate from GUI preview and processing rates."
        )
        self.fps_plot.setToolTip(analysis.fps_method)

    def _draw_comparison(
        self, primary: ExperimentAnalysis, comparison: ExperimentAnalysis | None
    ) -> None:
        self.comparison_table.setRowCount(0)
        if comparison is None:
            self.comparison_notice.setText("Session A distribution is shown. Choose Session B to compare two separate recordings.")
            self._draw_histograms(primary, None)
            return

        notices = compare_methodology(primary, comparison)
        notice_lines = [notice.message for notice in notices if notice.is_warning]
        notice_lines.insert(0, "Separate recordings; elapsed-time overlays do not indicate simultaneous capture.")
        if not self._can_compare_fps(primary, comparison):
            notice_lines.append("FPS curves require recorder elapsed timestamps in both sessions; summary rates remain comparable where present.")
        if any(a.interval_histogram.underflow_count or a.interval_histogram.overflow_count for a in (primary, comparison)):
            notice_lines.append("Distribution edge bins include out-of-range intervals; summary statistics use actual values.")
        self.comparison_notice.setText("\n".join(dict.fromkeys(notice_lines)))
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
        self.comparison_table.resizeColumnsToContents()
        self._draw_histograms(primary, comparison)

    @staticmethod
    def _comparison_rows(
        left: ExperimentAnalysis, right: ExperimentAnalysis
    ) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = [
            ("Camera model", left.option.camera, right.option.camera, ""),
            ("Stored identity source", _identity_text(left.option), _identity_text(right.option), ""),
            ("Capture mode", _mode_text(left), _mode_text(right), ""),
            ("Requested FPS", _requested_fps_text(left), _requested_fps_text(right), ""),
            ("Driver-reported FPS", _driver_fps_text(left), _driver_fps_text(right), ""),
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

    def _draw_histograms(
        self, left: ExperimentAnalysis, right: ExperimentAnalysis | None
    ) -> None:
        self.distribution_plot.clear()
        plot = self.distribution_plot.plotItem
        plot.setTitle("Interval distribution")
        self.distribution_plot.setLogMode(x=True, y=False)
        analyses = [(left, "#167d9a", "A")]
        if right is not None:
            analyses.append((right, "#b66518", "B"))
        visible = [a for a, _color, _side in analyses if a.interval_histogram.centers_ms]
        if visible:
            plot.addLegend(offset=(10, 8))
        messages: list[str] = []
        for analysis, color, side in analyses:
            histogram = analysis.interval_histogram
            if histogram.centers_ms:
                curve = plot.plot(
                    histogram.centers_ms,
                    histogram.fractions,
                    pen=pg.mkPen(color, width=2.0, style=Qt.PenStyle.SolidLine if side == "A" else Qt.PenStyle.DashLine),
                    symbol="o" if side == "A" else "t", symbolSize=7,
                    name=f"Session {side} · {self._short_session(analysis)}",
                )
                curve.setToolTip(self._full_session_label(analysis))
            if histogram.zero_sample_count:
                messages.append(
                    f"Session {side}: {histogram.zero_sample_count:,} zero interval(s) retained in summaries but not placed on the log axis."
                )
            if analysis.interval_boundary_samples_excluded:
                messages.append(
                    f"Session {side}: first-row zero marker retained in raw history and excluded from derived statistics."
                )
            if not histogram.centers_ms and not analysis.interval_boundary_samples_excluded:
                messages.append(f"Session {side}: no raw positive interval series is available.")
        self.distribution_note.setText(" ".join(messages) or (
            "Shared logarithmic bins are normalized over positive intervals; zero values remain visible in the interval summary."
            if visible else "No raw positive interval distribution is available; saved summaries are shown in the cards and table."
        ))

    def _render_header_and_metrics(self, analysis: ExperimentAnalysis) -> None:
        self.experiment_heading.setText(analysis.option.label)
        self.experiment_heading.setToolTip(self._full_session_label(analysis))
        values = {
            "camera": analysis.option.camera,
            "mode": analysis.option.mode_label,
            "date": (analysis.option.started_at or "not recorded")[:19].replace("T", " "),
            "duration": _format_duration(analysis.duration_seconds),
            "status": analysis.saved_status,
        }
        for key, value in values.items():
            self.header_values[key].setText(value)
            self.header_values[key].setToolTip(value)
        metrics = {
            "capture_fps": _format_fps(analysis.capture_fps_stats.mean),
            "preview_fps": _format_fps(analysis.preview_fps_mean),
            "processing_fps": _format_fps(analysis.processing_fps_mean),
            "frame_count": _format_count(analysis.frame_count),
            "median": _format_ms(analysis.interval_stats.median),
            "p95": _format_ms(analysis.interval_stats.p95),
            "p99": _format_ms(analysis.interval_stats.p99),
            "failures": _format_count(analysis.read_failures),
        }
        for key, value in metrics.items():
            self.metric_cards[key].value.setText(value)

    def _render_warnings(self, analysis: ExperimentAnalysis) -> None:
        warnings = []
        if analysis.interval_boundary_samples_excluded:
            warnings.append(
                f"{analysis.interval_boundary_samples_excluded} first-row legacy zero interval excluded from derived statistics; its CSV row remains preserved."
            )
        if analysis.saved_status.lower() == "interrupted":
            warnings.append("Interrupted session: summaries cover only measurements saved before interruption.")
        if not analysis.interval_data_available:
            warnings.append("Raw interval data is unavailable; no timing series is fabricated from summary fields.")
        if analysis.option.source == "imported":
            warnings.append("Imported runs may have different timestamp scope or summary precision.")
        self.warning_label.setText("  ·  ".join(warnings))
        self.warning_label.setVisible(bool(warnings))

    def _render_detail_text(self, analysis: ExperimentAnalysis) -> None:
        lines = [
            f"Experiment: {analysis.option.label}",
            f"Camera: {analysis.option.camera} · identity: {analysis.option.identity_badge or 'unavailable'}",
            f"Record source: {analysis.option.source} · status: {analysis.saved_status}",
            f"Started: {analysis.option.started_at or 'not recorded'}",
            f"Duration: {_format_duration(analysis.duration_seconds)} ({analysis.duration_source or 'source unavailable'})",
            f"Requested capture settings: {_settings_text(analysis.requested_settings)}",
            f"Driver-reported capture settings: {_settings_text(analysis.driver_settings)}",
            f"Observed capture FPS mean / median: {_format_fps(analysis.capture_fps_stats.mean)} / {_format_fps(analysis.capture_fps_stats.median)}",
            f"GUI preview FPS mean: {_format_fps(analysis.preview_fps_mean)}; processing FPS mean: {_format_fps(analysis.processing_fps_mean)}",
            "Capture FPS is derived from delivered host frame-arrival intervals. Preview FPS is GUI update cadence. Requested/driver FPS is not delivered FPS.",
        ]
        if analysis.configuration_note:
            lines.append(analysis.configuration_note)
        if analysis.option.source == "imported":
            lines.append("Imported history may have sparse timestamps or summary-only fields; missing values remain unavailable.")
        if analysis.option.source == "live_experiment":
            lines.append("Host arrival timestamps are recorded immediately after OpenCV read returns. They do not measure sensor exposure or sensor-to-display latency.")
        if analysis.interval_scope == "unknown":
            lines.append("The stored frame-interval scope is unknown; no capture FPS was derived from it.")
        if analysis.interval_stats.count:
            lines.append(
                f"Analyzed intervals: n={analysis.interval_stats.count:,}; "
                f"min {_format_ms(analysis.interval_stats.minimum)}; "
                f"median {_format_ms(analysis.interval_stats.median)}; "
                f"p95 {_format_ms(analysis.interval_stats.p95)}; "
                f"p99 {_format_ms(analysis.interval_stats.p99)}; "
                f"max {_format_ms(analysis.interval_stats.maximum)}"
            )
        if analysis.interval_boundary_samples_excluded:
            lines.append("Boundary policy: preserve the legacy first CSV row (0.000 ms) in raw_records, but exclude it from interval statistics and FPS derivation because no earlier frame exists.")
        if analysis.interval_histogram.zero_sample_count:
            lines.append(f"Zero intervals retained in analyzed measurements: {analysis.interval_histogram.zero_sample_count:,}; zeros cannot appear on the logarithmic histogram axis.")
        if analysis.reported_interval_summary:
            reported = "; ".join(
                f"{name}: mean {_format_ms(values.mean)}, range "
                f"{_format_ms(values.minimum)}–{_format_ms(values.maximum)} "
                f"across {values.count} stored field sample(s)"
                for name, values in analysis.reported_interval_summary.items()
            )
            lines.append(
                "Imported summary fields (stored values, not recalculated from raw intervals): " + reported
            )
        if analysis.timing_method:
            lines.append(f"Interval timing method: {analysis.timing_method}")
        if analysis.fps_method:
            lines.append(f"FPS curve method: {analysis.fps_method}")
        if analysis.failure_source:
            lines.append(f"Failure metric: {analysis.failure_source}")
        if analysis.frame_count_source:
            lines.append(f"Frame count source: {analysis.frame_count_source}")
        if analysis.termination_reason:
            lines.append(f"Termination: {analysis.termination_reason}")
        if analysis.notes:
            lines.extend(("", f"Experiment notes: {analysis.notes}"))
        self.details_text.setText("\n".join(lines))

    @staticmethod
    def _can_compare_fps(
        left: ExperimentAnalysis, right: ExperimentAnalysis | None
    ) -> bool:
        return bool(
            right is not None and left.fps_points and right.fps_points
            and left.fps_x_label == right.fps_x_label == "Recorder elapsed time (s)"
        )

    @staticmethod
    def _can_overlay_intervals(
        left: ExperimentAnalysis, right: ExperimentAnalysis | None
    ) -> bool:
        return bool(
            right is not None and left.interval_points and right.interval_points
            and left.interval_x_label == right.interval_x_label == "Recorder elapsed time (s)"
            and left.interval_scope == right.interval_scope
            and left.interval_method and left.interval_method == right.interval_method
        )

    @staticmethod
    def _full_session_label(analysis: ExperimentAnalysis) -> str:
        return f"{analysis.option.camera} · {analysis.option.label} · {analysis.option.started_at or 'date unavailable'}"

    @staticmethod
    def _short_session(analysis: ExperimentAnalysis) -> str:
        label = f"{analysis.option.camera} · {analysis.option.label}"
        return label if len(label) <= 38 else label[:35] + "…"

    def _redraw_comparison_curves(self, _checked: bool = False) -> None:
        if self._primary is not None:
            self._draw_interval_plot(self._primary)
            self._draw_fps_plot(self._primary, self._comparison)

    @staticmethod
    def _option_label(option: ExperimentOption) -> str:
        date = option.started_at or "date unavailable"
        identity = f" · {option.identity_badge}" if option.identity_badge else ""
        return f"{option.label} · {option.camera}{identity} · {option.mode_label} · {date} · {option.status}"

    @staticmethod
    def _combo_identity(combo: QComboBox) -> tuple[str, str | None] | None:
        value = combo.currentData(Qt.ItemDataRole.UserRole)
        return tuple(value) if value else None

    @staticmethod
    def _find_identity(combo: QComboBox, identity: tuple[str, str | None] | None) -> int:
        if identity is None:
            return -1
        for index in range(combo.count()):
            if combo.itemData(index, Qt.ItemDataRole.UserRole) == identity:
                return index
        return -1

    def _clear_analysis(self) -> None:
        self._primary = None
        self._comparison = None
        self.experiment_heading.setText("Select a saved experiment")
        for value in self.header_values.values():
            value.setText("—")
        for card in self.metric_cards.values():
            card.value.setText("—")
        self.warning_label.hide()
        self.details_text.setText("Select a saved experiment to inspect technical details.")
        self.interval_plot.clear()
        self.fps_plot.clear()
        self.distribution_plot.clear()
        self.comparison_table.setRowCount(0)
        self.comparison_notice.setText("Choose Session B to compare two saved recordings.")
        self.distribution_note.setText("No raw interval distribution is available.")
        self.fps_note.setText("Select an experiment with recorded capture timing.")
        self.interval_note.setText("Select an experiment with recorded frame intervals.")


def _mode_text(analysis: ExperimentAnalysis) -> str:
    return analysis.option.mode_label


def _identity_text(option: ExperimentOption) -> str:
    return option.identity_badge or "Identity provenance unavailable"


def _requested_fps_text(analysis: ExperimentAnalysis) -> str:
    value = next((analysis.requested_settings[key] for key in ("requested_fps", "fps", "frame_rate")
                  if key in analysis.requested_settings), None)
    try:
        return f"{float(value):g} FPS requested" if value is not None else "not recorded"
    except (TypeError, ValueError):
        return "not recorded"


def _driver_fps_text(analysis: ExperimentAnalysis) -> str:
    value = next((analysis.driver_settings[key] for key in ("reported_fps", "fps", "frame_rate")
                  if key in analysis.driver_settings), None)
    try:
        return f"{float(value):g} FPS" if value is not None else "not reported"
    except (TypeError, ValueError):
        return "not reported"


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
