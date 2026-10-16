"""Read-only grouped History and Analysis window for synthetic schema-v3 runs.

This UI is only imported by the standalone synthetic recording harness. The
normal application History and Analysis workspaces remain schema-v2-only.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import pyqtgraph as pg
from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtGui import QCloseEvent, QPalette, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QPushButton,
)

from ..grouped_analysis import GroupedExperimentAnalysis, GroupedStreamAnalysis
from .plot_navigation import PlotNavigationControls, reset_plot_view


@dataclass(frozen=True, slots=True)
class _Series:
    x: tuple[float, ...]
    y: tuple[float, ...]
    name: str
    color: str
    dashed: bool = False
    tooltip: str = ""


class _UnitAxis(pg.AxisItem):
    def __init__(self, suffix: str, *, decimals: int = 2, orientation: str = "left") -> None:
        super().__init__(orientation=orientation)
        self.suffix = suffix
        self.decimals = decimals

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - PyQtGraph API
        return [f"{value:.{self.decimals}f}{self.suffix}" for value in values]


class _PlotPanel(QWidget):
    reset_requested = Signal(str)
    expand_requested = Signal(str, bool)

    def __init__(
        self,
        key: str,
        title: str,
        *,
        y_suffix: str,
        y_decimals: int = 2,
        log_x: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._title = title
        self._x_label = "Elapsed time (s)"
        self._y_label = ""
        self._x_range: tuple[float, float] | None = None
        self._series: tuple[_Series, ...] = ()
        self._log_x = log_x
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.heading = QLabel(title)
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet("color: palette(text); font-weight: 600;")
        layout.addWidget(self.heading)
        self.plot = pg.PlotWidget(axisItems={"left": _UnitAxis(y_suffix, decimals=y_decimals)})
        self.plot.setMinimumHeight(260)
        self.plot.setBackground(None)
        self.plot.plotItem.vb.setMouseMode(pg.ViewBox.PanMode)
        self.plot.plotItem.vb.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setLogMode(x=log_x, y=False)
        self.controls = PlotNavigationControls(key, self.plot, self)
        self.controls.reset_requested.connect(self.reset_requested)
        self.controls.expand_requested.connect(self.expand_requested)
        layout.addWidget(self.controls)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.plot)
        self.empty_label = QLabel("No valid persisted timing data is available for this plot.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setMinimumHeight(180)
        self.empty_label.setStyleSheet(
            "color: palette(text); border: 1px solid palette(mid); border-radius: 4px; padding: 16px;"
        )
        self.stack.addWidget(self.empty_label)
        layout.addWidget(self.stack, 1)

    def set_series(
        self,
        series: Iterable[_Series],
        *,
        title: str,
        x_label: str,
        y_label: str,
        x_range: tuple[float, float] | None,
        empty_message: str,
        zero_baseline: bool = False,
    ) -> None:
        self._series = tuple(series)
        self._title = title
        self._x_label = x_label
        self._y_label = y_label
        self._x_range = x_range
        self.heading.setText(title)
        self.empty_label.setText(empty_message)
        self.plot.clear()
        self.plot.setLogMode(x=self._log_x, y=False)
        text_color = self.palette().color(QPalette.ColorRole.Text).name()
        self.plot.setLabel("bottom", x_label, color=text_color)
        self.plot.setLabel("left", y_label, color=text_color)
        self.plot.plotItem.setTitle(title, color=text_color)
        visible = [item for item in self._series if item.x and item.y]
        if visible:
            self.plot.addLegend(
                offset=(10, 8),
                labelTextColor=self.palette().color(QPalette.ColorRole.Text),
            )
            for item in visible:
                curve = self.plot.plot(
                    item.x,
                    item.y,
                    pen=pg.mkPen(
                        item.color,
                        width=1.8,
                        style=Qt.PenStyle.DashLine if item.dashed else Qt.PenStyle.SolidLine,
                    ),
                    name=item.name,
                )
                if item.tooltip:
                    curve.setToolTip(item.tooltip)
            if zero_baseline:
                self.plot.addItem(pg.InfiniteLine(
                    pos=0, angle=0, pen=pg.mkPen("#777777", width=1, style=Qt.PenStyle.DotLine)
                ))
            self.stack.setCurrentWidget(self.plot)
            self.controls.set_data_available(True)
            self.reset_view()
        else:
            self.stack.setCurrentWidget(self.empty_label)
            self.controls.set_data_available(False)

    def reset_view(self) -> bool:
        return reset_plot_view(self.plot, self._x_range)

    def fit_to_data(self) -> bool:
        return reset_plot_view(self.plot)

    def expanded_dialog(self) -> QDialog | None:
        if not self._series or not any(item.x and item.y for item in self._series):
            return None
        dialog = QDialog(self, Qt.WindowType.Window)
        dialog.setWindowTitle(self._title)
        dialog.resize(1120, 760)
        layout = QVBoxLayout(dialog)
        plot = pg.PlotWidget(axisItems={"left": _UnitAxis("", decimals=2)})
        palette = self.palette()
        plot.setBackground(palette.color(QPalette.ColorRole.Base))
        plot.plotItem.vb.setMouseMode(pg.ViewBox.PanMode)
        plot.plotItem.vb.setMenuEnabled(False)
        plot.hideButtons()
        plot.setLogMode(x=self._log_x, y=False)
        text_color = palette.color(QPalette.ColorRole.Text).name()
        plot.setLabel("bottom", self._x_label, color=text_color)
        plot.setLabel("left", self._y_label, color=text_color)
        plot.plotItem.setTitle(self._title, color=text_color)
        plot.getAxis("left").setTextPen(text_color)
        plot.getAxis("bottom").setTextPen(text_color)
        plot.getAxis("left").setPen(palette.color(QPalette.ColorRole.Mid))
        plot.getAxis("bottom").setPen(palette.color(QPalette.ColorRole.Mid))
        plot.addLegend(
            offset=(10, 8),
            labelTextColor=self.palette().color(QPalette.ColorRole.Text),
        )
        for item in self._series:
            if not item.x or not item.y:
                continue
            curve = plot.plot(
                item.x,
                item.y,
                pen=pg.mkPen(item.color, width=1.8,
                             style=Qt.PenStyle.DashLine if item.dashed else Qt.PenStyle.SolidLine),
                name=item.name,
            )
            if item.tooltip:
                curve.setToolTip(item.tooltip)
        controls = PlotNavigationControls(f"focus-{self.key}", plot, dialog)
        controls.expand_button.hide()
        controls.reset_requested.connect(lambda _key: reset_plot_view(plot, self._x_range))
        layout.addWidget(controls)
        layout.addWidget(plot, 1)
        reset_plot_view(plot, self._x_range)
        return dialog


class GroupedExperimentExplorerDialog(QDialog):
    """Read-only grouped experiment summary and comparative plots."""

    def __init__(
        self,
        analysis: GroupedExperimentAnalysis,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.analysis = analysis
        self.setWindowTitle(f"{analysis.group.label} · Grouped History and Analysis")
        self.resize(1280, 900)
        self.setMinimumSize(760, 560)
        self._expanded: dict[str, QDialog] = {}
        self._plot_panels: dict[str, _PlotPanel] = {}
        self._build_ui()
        self._apply_theme()
        self._draw_analysis()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        title = QLabel(self.analysis.group.label)
        title.setWordWrap(True)
        title.setStyleSheet("color: palette(text); font-size: 18px; font-weight: 600;")
        layout.addWidget(title)
        status = QLabel(self.analysis.display_status)
        status.setStyleSheet("color: palette(text); font-weight: 600;")
        status.setWordWrap(True)
        layout.addWidget(status)
        self.status_label = status
        self.tabs = QTabWidget()
        self.history_page = self._build_history_page()
        self.analysis_page = self._build_analysis_page()
        self.tabs.addTab(self.history_page, "Grouped History")
        self.tabs.addTab(self.analysis_page, "Comparative Analysis")
        layout.addWidget(self.tabs, 1)

    @staticmethod
    def _scroll_page(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build_history_page(self) -> QScrollArea:
        page = QWidget()
        layout = QVBoxLayout(page)
        metadata = QGroupBox("Experiment")
        form = QFormLayout(metadata)
        self._add_form_row(form, "Experiment ID", self.analysis.group.session_id)
        self._add_form_row(form, "Start requested", self.analysis.group.record_requested_at or "Not recorded")
        self._add_form_row(form, "Recording started", self.analysis.group.recording_started_at or "Not recorded")
        self._add_form_row(form, "Stop requested", self.analysis.group.stop_requested_at or "Not recorded")
        self._add_form_row(form, "Finalized", self.analysis.group.finalized_at or "Not finalized")
        self._add_form_row(form, "Group status", self.analysis.display_status)
        self._add_form_row(form, "Clock epoch", self.analysis.group.clock_epoch_id)
        if self.analysis.shared_start_offset_seconds is not None:
            self._add_form_row(
                form,
                "Host-observed first-arrival offset (B − A)",
                f"{self.analysis.shared_start_offset_seconds * 1000:+.3f} ms · not exposure synchronization",
            )
        layout.addWidget(metadata)
        open_analysis = QPushButton("Open in Grouped Analysis")
        self.open_analysis_button = open_analysis
        open_analysis.setToolTip("Show the two independently recorded streams and their timing comparisons.")
        open_analysis.setStyleSheet("color: palette(button-text);")
        open_analysis.clicked.connect(self._open_analysis)
        layout.addWidget(open_analysis)
        if self.analysis.warnings:
            warning = QLabel("\n".join(self.analysis.warnings))
            warning.setObjectName("groupedDataWarnings")
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "color: palette(text); background: palette(alternate-base); padding: 8px; border: 1px solid palette(mid);"
            )
            layout.addWidget(warning)
        self.stream_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.stream_splitter.setChildrenCollapsible(False)
        for stream in self.analysis.streams:
            self.stream_splitter.addWidget(self._stream_history_card(stream))
        layout.addWidget(self.stream_splitter)

        event_group = QGroupBox("Lifecycle events")
        event_layout = QVBoxLayout(event_group)
        events = sorted(
            self.analysis.group.events,
            key=lambda event: (event.elapsed_ns is None, event.elapsed_ns or 0, event.event_id),
        )
        self.events_table = QTableWidget(len(events), 4)
        self.events_table.setHorizontalHeaderLabels(("Event", "Experiment elapsed", "Observed UTC", "Reason"))
        self.events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.events_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.events_table.horizontalHeader().setStretchLastSection(True)
        for row, event in enumerate(events):
            values = (
                event.event_type.replace("_", " "),
                _format_seconds_ns(event.elapsed_ns),
                event.observed_at,
                event.reason_code or "—",
            )
            for column, value in enumerate(values):
                self.events_table.setItem(row, column, QTableWidgetItem(value))
        self.events_table.resizeColumnsToContents()
        event_layout.addWidget(self.events_table)
        layout.addWidget(event_group)
        self._add_methodology_details(layout)
        layout.addStretch(1)
        return self._scroll_page(page)

    def _stream_history_card(self, item: GroupedStreamAnalysis) -> QGroupBox:
        stream = item.stream
        identity = stream.identity_snapshot
        model = str(identity.get("model") or "Camera identity unavailable")
        manufacturer = str(identity.get("manufacturer") or "")
        card = QGroupBox(f"Stream {stream.slot} · {manufacturer} {model}".strip())
        form = QFormLayout(card)
        mode = item.requested_capture_mode or _mode_text(stream.configuration)
        self._add_form_row(form, "Camera identity", f"{manufacturer} {model}".strip())
        self._add_form_row(form, "Requested capture mode", mode)
        self._add_form_row(
            form,
            "Driver-reported / negotiated mode",
            item.driver_reported_capture_mode or "Not recorded",
        )
        self._add_form_row(form, "Requested FPS", _format_rate(item.requested_fps))
        self._add_form_row(form, "Driver-reported FPS", _format_rate(item.driver_reported_fps))
        self._add_form_row(form, "Observed capture FPS", _format_rate(item.observed_capture_fps))
        self._add_form_row(form, "Stream status", stream.stream_status)
        self._add_form_row(form, "Stream start (experiment elapsed)", _format_seconds_ns(item.recording_start_elapsed_ns))
        self._add_form_row(form, "Stream end (experiment elapsed)", _format_seconds_ns(item.recording_end_elapsed_ns))
        self._add_form_row(form, "Host-observed recording duration", _format_duration(item.recording_duration_seconds))
        self._add_form_row(form, "Captured frame events (counter)", f"{stream.capture_frame_count:,}")
        self._add_form_row(form, "Committed frame-arrival rows", f"{item.persisted_frame_samples:,}")
        self._add_form_row(form, "Committed read-failure rows", f"{item.persisted_read_failure_samples:,}")
        self._add_form_row(form, "Read failures observed (counter)", f"{stream.read_failure_count:,}")
        self._add_form_row(form, "Last persisted sequence index", _format_count(item.last_sequence_index))
        self._add_form_row(form, "Sequence gaps observed", f"{item.observed_sequence_gap_count:,}")
        self._add_form_row(form, "Queue rejections", f"{stream.timing_queue_loss_count:,}")
        self._add_form_row(form, "Persistence losses", f"{stream.persistence_loss_count:,}")
        self._add_form_row(form, "Data completeness", "Complete" if item.effective_data_complete else "Incomplete / flagged")
        self._add_form_row(form, "Frame intervals analyzed", f"{item.interval_stats.count:,}")
        self._add_form_row(form, "Mean / median interval", _format_pair(item.interval_stats.mean, item.interval_stats.median))
        self._add_form_row(form, "p95 / p99 interval", _format_pair(item.interval_stats.p95, item.interval_stats.p99))
        self._add_form_row(form, "Interval standard deviation", _format_ms(_interval_stddev(item)))
        self._add_form_row(form, "Preview FPS (GUI)", _format_rate(item.preview_fps))
        self._add_form_row(form, "Processing FPS", _format_rate(item.processing_fps))
        for warning_text in item.warnings:
            warning = QLabel(warning_text)
            warning.setWordWrap(True)
            warning.setStyleSheet("color: palette(text); padding: 2px;")
            form.addRow("Data note", warning)
        return card

    def _add_methodology_details(self, layout: QVBoxLayout) -> None:
        details = QGroupBox("Timing provenance and interpretation")
        details.setCheckable(True)
        details.setChecked(False)
        detail_layout = QVBoxLayout(details)
        lines = [
            "FPS curves are calculated from contiguous persisted frame-arrival intervals. Intervals crossing a missing sequence are excluded; no missing samples are interpolated.",
            "Per-stream elapsed time starts at each stream's first persisted arrival. Shared experiment elapsed time uses the coordinator's host monotonic epoch only when both streams provide compatible provenance.",
            "The host-observed first-arrival offset is relative to the common coordinator timeline. It is not hardware synchronization, sensor exposure timing, USB transfer latency, or sensor-to-display latency.",
            "GUI preview FPS and processing FPS are separate measurements. Read failures describe unsuccessful host reads and do not prove sensor frame drops.",
            "V4L2/source timestamp and camera sequence fields are unavailable for the synthetic OpenCV-style timing source unless explicitly stored.",
        ]
        for line in lines:
            label = QLabel(line)
            label.setWordWrap(True)
            label.setStyleSheet("color: palette(text);")
            detail_layout.addWidget(label)
        layout.addWidget(details)

    def _build_analysis_page(self) -> QScrollArea:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        from PySide6.QtWidgets import QComboBox, QLabel

        controls.addWidget(QLabel("Time alignment"))
        self.time_alignment = QComboBox()
        self.time_alignment.addItem("Shared experiment elapsed time", "shared")
        self.time_alignment.addItem("Per-stream elapsed time", "stream")
        if not self.analysis.shared_time_available:
            self.time_alignment.setItemText(0, "Shared experiment elapsed time · unavailable")
            self.time_alignment.setItemData(0, self.analysis.shared_time_reason or "Unavailable", Qt.ItemDataRole.ToolTipRole)
            self.time_alignment.setEnabled(False)
            self.time_alignment.setCurrentIndex(1)
        controls.addWidget(self.time_alignment)
        controls.addWidget(QLabel("Rate display"))
        self.rate_mode = QComboBox()
        self.rate_mode.addItem("Capture FPS (frames/s)", "fps")
        self.rate_mode.addItem("Deviation from each stream's own mean (%)", "deviation")
        controls.addWidget(self.rate_mode)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.alignment_note = QLabel()
        self.alignment_note.setWordWrap(True)
        self.alignment_note.setStyleSheet("color: palette(text);")
        layout.addWidget(self.alignment_note)

        self.rate_panel = _PlotPanel("group-rate", "A/B capture rate over time", y_suffix="", y_decimals=2)
        self._plot_panels[self.rate_panel.key] = self.rate_panel
        self.rate_panel.reset_requested.connect(self._reset_plot)
        self.rate_panel.expand_requested.connect(self._expand_plot)
        layout.addWidget(self.rate_panel)

        self.interval_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.interval_splitter.setChildrenCollapsible(False)
        self.interval_a = _PlotPanel("group-interval-a", "Stream A frame intervals", y_suffix=" ms", y_decimals=3)
        self.interval_b = _PlotPanel("group-interval-b", "Stream B frame intervals", y_suffix=" ms", y_decimals=3)
        for panel in (self.interval_a, self.interval_b):
            self._plot_panels[panel.key] = panel
            panel.reset_requested.connect(self._reset_plot)
            panel.expand_requested.connect(self._expand_plot)
            self.interval_splitter.addWidget(panel)
        layout.addWidget(self.interval_splitter)

        self.histogram_panel = _PlotPanel(
            "group-histogram", "Frame interval distribution", y_suffix="%", y_decimals=1
        )
        self._plot_panels[self.histogram_panel.key] = self.histogram_panel
        self.histogram_panel.reset_requested.connect(self._reset_plot)
        self.histogram_panel.expand_requested.connect(self._expand_plot)
        layout.addWidget(self.histogram_panel)

        self.stats_table = QTableWidget(0, 3)
        self.stats_table.setHorizontalHeaderLabels(("Metric", "Stream A", "Stream B"))
        self.stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.stats_table)
        self._populate_stats_table()
        self.time_alignment.currentIndexChanged.connect(self._draw_analysis)
        self.rate_mode.currentIndexChanged.connect(self._draw_analysis)
        layout.addStretch(1)
        return self._scroll_page(page)

    def _populate_stats_table(self) -> None:
        rows = (
            ("Camera model", lambda s: str(s.stream.identity_snapshot.get("model") or "Unavailable")),
            ("Requested capture mode", lambda s: s.requested_capture_mode or "Not recorded"),
            ("Driver-reported / negotiated mode", lambda s: s.driver_reported_capture_mode or "Not recorded"),
            ("Requested FPS", lambda s: _format_rate(s.requested_fps)),
            ("Driver-reported FPS", lambda s: _format_rate(s.driver_reported_fps)),
            ("Observed capture FPS", lambda s: _format_rate(s.observed_capture_fps)),
            ("Mean frame interval", lambda s: _format_ms(s.interval_stats.mean)),
            ("Median frame interval", lambda s: _format_ms(s.interval_stats.median)),
            ("p95 / p99 frame interval", lambda s: _format_pair(s.interval_stats.p95, s.interval_stats.p99)),
            ("Min / max frame interval", lambda s: _format_pair(s.interval_stats.minimum, s.interval_stats.maximum)),
            ("Frame interval SD / CV", lambda s: f"{_format_ms(_interval_stddev(s))} / {_format_percent(_interval_cv(s))}"),
            ("Captured event counter", lambda s: f"{s.stream.capture_frame_count:,}"),
            ("Committed frame-arrival rows", lambda s: f"{s.persisted_frame_samples:,}"),
            ("Committed read-failure rows", lambda s: f"{s.persisted_read_failure_samples:,}"),
            ("Read failures observed", lambda s: f"{s.stream.read_failure_count:,}"),
            ("Queue rejections / persistence loss", lambda s: f"{s.stream.timing_queue_loss_count:,} / {s.stream.persistence_loss_count:,}"),
            ("Last persisted sequence index", lambda s: _format_count(s.last_sequence_index)),
            ("Observed missing sequence values", lambda s: f"{s.observed_sequence_gap_count:,}"),
            ("Host-observed recording duration", lambda s: _format_duration(s.recording_duration_seconds)),
            ("Timing completeness", lambda s: "Complete" if s.effective_data_complete else "Incomplete / flagged"),
            ("Timing provenance", lambda s: f"{s.stream.timestamp_source} · {s.stream.clock_type} · {s.stream.collection_point}"),
        )
        self.stats_table.setRowCount(len(rows))
        for row_index, (name, accessor) in enumerate(rows):
            self.stats_table.setItem(row_index, 0, QTableWidgetItem(name))
            for column, stream in enumerate(self.analysis.streams, start=1):
                self.stats_table.setItem(row_index, column, QTableWidgetItem(accessor(stream)))
        self.stats_table.resizeColumnsToContents()

    def _draw_analysis(self, _index: int = 0) -> None:
        for dialog in tuple(self._expanded.values()):
            dialog.close()
        self._expanded.clear()
        shared = self.time_alignment.currentData() == "shared" and self.analysis.shared_time_available
        deviation = self.rate_mode.currentData() == "deviation"
        alignment = "Shared experiment elapsed time (s)" if shared else "Per-stream elapsed time (s)"
        if shared:
            if self.analysis.shared_start_offset_seconds is not None:
                self.alignment_note.setText(
                    f"Shared host monotonic experiment time. First persisted/observed host arrival offset (B − A): "
                    f"{self.analysis.shared_start_offset_seconds * 1000:+.3f} ms. This does not indicate exposure synchronization."
                )
            else:
                self.alignment_note.setText(
                    "Shared host monotonic experiment time; no first-arrival offset is available. The streams are not hardware synchronized."
                )
        else:
            self.alignment_note.setText(
                "Each stream starts at its own first persisted arrival; this view hides the start offset and does not pair frames."
            )

        colors = ("#167d9a", "#b66518")
        rate_series = []
        rate_warnings = []
        for stream, color in zip(self.analysis.streams, colors):
            points = stream.fps_points_shared if shared else stream.fps_points_stream
            if deviation:
                mean = stream.observed_capture_fps
                if mean is None or mean <= 0:
                    points = ()
                    rate_warnings.append(f"Stream {stream.stream.slot} has no positive mean capture rate for normalization.")
                else:
                    points = tuple((x, 100.0 * (fps - mean) / mean) for x, fps in points)
            name = _series_name(stream)
            if not points:
                rate_warnings.append(
                    f"Stream {stream.stream.slot} has fewer than two valid intervals in a rate window or no timing series."
                )
            rate_series.append(_Series(
                x=tuple(point[0] for point in points),
                y=tuple(point[1] for point in points),
                name=name,
                color=color,
                dashed=stream.stream.slot == "B",
                tooltip=_method_tooltip(stream),
            ))
        duration = self._display_duration(shared)
        duration = max(duration, max((_max_x(series.x) for series in rate_series), default=0.0))
        rate_range = (0.0, max(1.0, duration))
        rate_y = "Deviation from own mean (%)" if deviation else "Capture FPS from persisted arrivals (frames/s)"
        rate_title = "A/B capture-rate deviation" if deviation else "A/B capture rate over time"
        self.rate_panel.plot.getAxis("left").suffix = "%" if deviation else ""
        rate_note = (
            "Rate windows use contiguous persisted inter-arrival measurements and each stream's own saved mean; no interpolation is performed."
            if deviation else
            "Rate windows use contiguous persisted host frame-arrival intervals; rates are interval count ÷ summed interval time. Missing sequence gaps are excluded."
        )
        if rate_warnings:
            rate_note += " " + " ".join(dict.fromkeys(rate_warnings))
        self.rate_panel.set_series(
            rate_series,
            title=rate_title,
            x_label=alignment,
            y_label=rate_y,
            x_range=rate_range,
            empty_message="No capture-rate windows have enough valid persisted intervals. " + rate_note,
            zero_baseline=deviation,
        )
        for stream, panel, color in zip(self.analysis.streams, (self.interval_a, self.interval_b), colors):
            points = stream.interval_points_shared if shared else stream.interval_points_stream
            panel.set_series(
                (_Series(
                    x=tuple(point[0] for point in points),
                    y=tuple(point[1] for point in points),
                    name=_series_name(stream),
                    color=color,
                    dashed=stream.stream.slot == "B",
                    tooltip=_method_tooltip(stream),
                ),),
                title=f"Stream {stream.stream.slot} · frame intervals",
                x_label=alignment,
                y_label="Frame interval (ms)",
                x_range=(0.0, max(
                    1.0,
                    self._display_duration_for_stream(stream, shared),
                    _max_x(tuple(point[0] for point in points)),
                )),
                empty_message=(
                    "No valid interval series is available. Intervals crossing missing sequence values, zero-length intervals, and negative intervals are not plotted."
                ),
            )
        histogram_series = []
        for stream, color in zip(self.analysis.streams, colors):
            hist = stream.interval_histogram
            histogram_series.append(_Series(
                x=tuple(math.log10(value) for value in hist.centers_ms),
                y=tuple(value * 100.0 for value in hist.fractions),
                name=_series_name(stream),
                color=color,
                dashed=stream.stream.slot == "B",
                tooltip=(
                    f"Positive valid interval count: {hist.positive_sample_count:,}; "
                    f"zero intervals: {hist.zero_sample_count:,}."
                ),
            ))
        has_hist = any(series.x for series in histogram_series)
        self.histogram_panel.set_series(
            histogram_series,
            title="Frame-interval distribution",
            x_label="Frame interval (ms, logarithmic)",
            y_label="Share of valid positive intervals (%)",
            x_range=None,
            empty_message=(
                "Raw positive interval measurements are unavailable. The plot is empty by design; summary values remain in the table."
            ),
        )
        # Plot log10-transformed bin centers with a linear x axis so tick labels
        # can remain in milliseconds without relying on PyQtGraph SI prefixes.
        self.histogram_panel.plot.setLogMode(x=False, y=False)
        self.histogram_panel.plot.getAxis("bottom").setTicks([[
            (math.log10(value), _format_ms(value)) for value in (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)
        ]])
        if has_hist:
            x_values = [value for item in histogram_series for value in item.x]
            if x_values:
                low, high = min(x_values), max(x_values)
                self.histogram_panel._x_range = (low, high) if low < high else (low - 0.1, high + 0.1)
                self.histogram_panel.reset_view()
        self._apply_theme()

    def _display_duration(self, shared: bool) -> float:
        if shared:
            terminal = [
                event.elapsed_ns / 1e9
                for event in self.analysis.group.events
                if event.event_type in {"stream_completed", "stream_interrupted", "stream_failed"}
                and event.elapsed_ns is not None
            ]
            last_arrivals = [
                stream.last_arrival_elapsed_ns / 1e9
                for stream in self.analysis.streams
                if stream.last_arrival_elapsed_ns is not None
            ]
            return max(terminal + last_arrivals, default=0.0)
        return max((self._display_duration_for_stream(stream, False) for stream in self.analysis.streams), default=0.0)

    @staticmethod
    def _display_duration_for_stream(stream: GroupedStreamAnalysis, shared: bool) -> float:
        if shared:
            if stream.recording_end_elapsed_ns is not None:
                return stream.recording_end_elapsed_ns / 1e9
            values = [
                value / 1e9
                for value in (
                    stream.stream.first_host_arrival_elapsed_ns,
                    stream.stream.last_host_arrival_elapsed_ns,
                )
                if value is not None
            ]
            return max(values, default=0.0)
        if stream.recording_duration_seconds is not None:
            return stream.recording_duration_seconds
        first = stream.first_arrival_elapsed_ns
        last = stream.last_arrival_elapsed_ns
        return max(0.0, (last - first) / 1e9) if first is not None and last is not None else 0.0

    def _reset_plot(self, key: str) -> None:
        panel = self._plot_panels.get(key)
        if panel is not None:
            panel.reset_view()

    def _open_analysis(self) -> None:
        self.tabs.setCurrentWidget(self.analysis_page)

    def _expand_plot(self, key: str, expanded: bool) -> None:
        panel = self._plot_panels.get(key)
        if panel is None:
            return
        current = self._expanded.pop(key, None)
        if current is not None:
            current.close()
        if expanded:
            dialog = panel.expanded_dialog()
            if dialog is None:
                panel.controls.set_expanded(False)
                return
            self._expanded[key] = dialog
            dialog.finished.connect(lambda _result, k=key, p=panel: self._expanded_closed(k, p))
            dialog.show()

    def _expanded_closed(self, key: str, panel: _PlotPanel) -> None:
        self._expanded.pop(key, None)
        panel.controls.set_expanded(False)

    def _apply_theme(self) -> None:
        palette = self.palette()
        for widget in self.findChildren(QLabel):
            widget.setAutoFillBackground(False)
            widget.setPalette(palette)
        text_color = palette.color(QPalette.ColorRole.Text)
        for plot in self.findChildren(pg.PlotWidget):
            plot.setBackground(palette.color(QPalette.ColorRole.Base))
            for name in ("left", "bottom"):
                axis = plot.getAxis(name)
                axis.setTextPen(text_color)
                axis.setPen(palette.color(QPalette.ColorRole.Mid))
                axis.setLabel(axis.labelText, color=text_color.name())
                axis.label.setDefaultTextColor(text_color)
            title = plot.plotItem.titleLabel
            if title.text:
                title.setText(title.text, color=text_color.name())
            legend = plot.plotItem.legend
            if legend is not None:
                legend.opts["labelTextColor"] = text_color
                for _sample, label in legend.items:
                    label.setText(label.text, color=text_color.name())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 1160 else Qt.Orientation.Horizontal
        if self.stream_splitter.orientation() != orientation:
            self.stream_splitter.setOrientation(orientation)
        if self.interval_splitter.orientation() != orientation:
            self.interval_splitter.setOrientation(orientation)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_theme()
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        for dialog in tuple(self._expanded.values()):
            dialog.close()
        self._expanded.clear()
        super().closeEvent(event)

    @staticmethod
    def _add_form_row(form: QFormLayout, label: str, value: str) -> None:
        text = QLabel(value)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setStyleSheet("color: palette(text);")
        form.addRow(label, text)


def _mode_text(configuration: dict[str, object]) -> str:
    width = configuration.get("width")
    height = configuration.get("height")
    pixel = configuration.get("pixel_format") or configuration.get("format")
    if width and height and pixel:
        return f"{width}×{height} {pixel}"
    return "Capture mode unavailable"


def _series_name(stream: GroupedStreamAnalysis) -> str:
    camera = str(stream.stream.identity_snapshot.get("model") or "Camera identity unavailable")
    rate = _format_rate(stream.requested_fps)
    return f"Stream {stream.stream.slot} · {camera} · requested {rate}"


def _method_tooltip(stream: GroupedStreamAnalysis) -> str:
    record = stream.stream
    return (
        f"{record.timestamp_source}; {record.clock_type}; {record.collection_point}; "
        f"method {record.timing_method} v{record.timing_method_version}. Host frame-arrival timing; not sensor exposure time."
    )


def _interval_stddev(stream: GroupedStreamAnalysis) -> float | None:
    return stream.interval_stddev_ms


def _interval_cv(stream: GroupedStreamAnalysis) -> float | None:
    deviation = _interval_stddev(stream)
    mean = stream.interval_stats.mean
    if deviation is None or mean is None or mean <= 0:
        return None
    return 100.0 * deviation / mean


def _format_pair(left: float | None, right: float | None) -> str:
    return f"{_format_ms(left)} / {_format_ms(right)}"


def _format_ms(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.3f} ms"


def _format_rate(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.2f} FPS"


def _format_percent(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.2f}%"


def _format_duration(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "Not available"
    return f"{value:.3f} s"


def _format_seconds_ns(value: int | None) -> str:
    return "—" if value is None else f"{value / 1e9:.6f} s"


def _format_count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _max_x(values: tuple[float, ...]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return max(finite, default=0.0)
