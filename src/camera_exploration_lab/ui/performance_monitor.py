"""Historical Canon replay and read-only live CEL/CHDKPTP telemetry view."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import nan
import time
from typing import Callable, Iterable

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..capture import OpenCVCaptureWorker
from ..canon_telemetry_ipc import CanonSource, CanonTelemetryConsumer, ConsumerState
from ..performance import Availability, Metric, TelemetrySample
from ..replay import ReplayController, ReplayDataset, load_builtin_replays


@dataclass(frozen=True, slots=True)
class LiveCaptureSource:
    key: str
    label: str
    worker: OpenCVCaptureWorker
    preview_update_fps: float


SourceProvider = Callable[[], Iterable[LiveCaptureSource]]


def _metric_value(sample: TelemetrySample, metric: Metric) -> tuple[float | int | str | None, Availability]:
    measurement = sample.get(metric)
    if measurement is None:
        return None, Availability.NOT_RECORDED
    return measurement.value, measurement.availability


def _display_value(sample: TelemetrySample, metric: Metric, decimals: int = 1) -> str:
    value, availability = _metric_value(sample, metric)
    if availability == Availability.NOT_RECORDED:
        return "Not recorded"
    if availability == Availability.UNAVAILABLE:
        return "Unavailable"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return "Unavailable"


class PerformanceMonitorWorkspace(QWidget):
    """Low-rate UI polling over a bounded worker ring; no per-frame Qt signals."""

    def __init__(
        self,
        *,
        source_provider: SourceProvider | None = None,
        canon_consumer: CanonTelemetryConsumer | None = None,
    ) -> None:
        super().__init__()
        self._source_provider = source_provider or (lambda: ())
        self._canon_consumer = canon_consumer or CanonTelemetryConsumer()
        self._datasets: tuple[ReplayDataset, ...] = ()
        self._controller: ReplayController | None = None
        self._live_sources: dict[str, LiveCaptureSource] = {}
        self._live_fingerprint: tuple[tuple[str, int], ...] = ()
        self._live_source_key: str | None = None
        self._live_cursor: int | None = None
        self._live_origin_ns: int | None = None
        self._live_times: deque[float] = deque(maxlen=4096)
        self._live_capture_rates: deque[float] = deque(maxlen=4096)
        self._live_intervals: deque[float] = deque(maxlen=4096)
        self._live_read_durations: deque[float] = deque(maxlen=4096)
        self._preview_times: deque[float] = deque(maxlen=600)
        self._preview_rates: deque[float] = deque(maxlen=600)
        self._latest_live_sample: TelemetrySample | None = None
        self._plot_mode: str | None = None
        self._live_curves: tuple[pg.PlotDataItem, pg.PlotDataItem, pg.PlotDataItem, pg.PlotDataItem] | None = None
        self._canon_source_key: str | None = None
        self._canon_fingerprint: tuple[tuple[str, str, bool], ...] = ()
        self._canon_cursor: int | None = None
        self._canon_origin_ns: int | None = None
        self._canon_last_host_ns: int | None = None
        self._canon_times: deque[float] = deque(maxlen=4096)
        self._canon_fetch_rates: deque[float] = deque(maxlen=4096)
        self._canon_reported_rates: deque[float] = deque(maxlen=4096)
        self._canon_content_rates: deque[float] = deque(maxlen=4096)
        self._canon_fetch_durations: deque[float] = deque(maxlen=4096)
        self._canon_receive_gaps: deque[float] = deque(maxlen=4096)
        self._canon_skips: deque[float] = deque(maxlen=4096)
        self._canon_polls: deque[float] = deque(maxlen=4096)
        self._canon_device_checks: deque[float] = deque(maxlen=4096)
        self._latest_canon_sample: TelemetrySample | None = None
        self._canon_curves: tuple[pg.PlotDataItem, ...] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(8)
        title = QLabel("Live Performance Monitor")
        title.setStyleSheet("color: palette(text); font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Source mode"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Historical Replay", "replay")
        self.mode_combo.addItem("Live CEL Capture", "live")
        self.mode_combo.addItem("Live Canon CHDKPTP", "canon")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_row.addWidget(self.mode_combo)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color: palette(text); padding: 4px 6px;")
        mode_row.addWidget(self.banner, 1)
        root.addLayout(mode_row)

        control_row = QHBoxLayout()
        self.dataset_combo = QComboBox()
        self.dataset_combo.currentIndexChanged.connect(self._dataset_changed)
        self.source_combo = QComboBox()
        self.source_combo.setMinimumContentsLength(22)
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setToolTip("Selected capture or telemetry source; hover a source for its full label.")
        self.source_combo.currentIndexChanged.connect(self._live_source_changed)
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.reset_button = QPushButton("Reset")
        self.play_button.clicked.connect(self.play_replay)
        self.pause_button.clicked.connect(self.pause_replay)
        self.reset_button.clicked.connect(self.reset_replay)
        self.pause_button.setEnabled(False)
        self.replay_position = QLabel("Historical Replay is ready.")
        self.replay_position.setStyleSheet("color: palette(text);")
        for widget in (self.dataset_combo, self.source_combo, self.play_button,
                       self.pause_button, self.reset_button, self.replay_position):
            control_row.addWidget(widget)
        control_row.addStretch(1)
        root.addLayout(control_row)

        self.canon_controls = QWidget()
        canon_control_layout = QHBoxLayout(self.canon_controls)
        canon_control_layout.setContentsMargins(0, 0, 0, 0)
        self.listen_button = QPushButton("Listen for CHDKPTP")
        self.listen_button.clicked.connect(self._toggle_canon_listener)
        self.socket_path_label = QLabel("Local AF_UNIX endpoint")
        self.socket_path_label.setToolTip(f"Local socket path: {self._canon_consumer.socket_path}")
        self.socket_path_label.setStyleSheet("color: palette(text);")
        self.canon_window_combo = QComboBox()
        self.canon_window_combo.addItem("Last 30 seconds", 30)
        self.canon_window_combo.addItem("Last 60 seconds", 60)
        self.canon_window_combo.addItem("Full session", None)
        self.canon_window_combo.setCurrentIndex(1)
        self.canon_window_combo.setToolTip(
            "Changes only the visible chart range. Retained host-receipt samples and full-session gaps are preserved."
        )
        self.canon_window_combo.currentIndexChanged.connect(self._update_canon_plots)
        self.bridge_status = QLabel("Listener stopped")
        self.bridge_status.setStyleSheet("color: palette(text); font-weight: 600;")
        canon_control_layout.addWidget(self.listen_button)
        canon_control_layout.addWidget(self.socket_path_label, 1)
        canon_control_layout.addWidget(QLabel("Chart range"))
        canon_control_layout.addWidget(self.canon_window_combo)
        canon_control_layout.addWidget(self.bridge_status)
        root.addWidget(self.canon_controls)

        self.metric_group = QGroupBox("Current sample")
        self.metric_grid = QGridLayout(self.metric_group)
        self.metric_values: dict[str, QLabel] = {}
        self.metric_labels: dict[str, QLabel] = {}
        for index, (key, label, tooltip) in enumerate(self._metric_definitions()):
            name = QLabel(label)
            name.setWordWrap(True)
            name.setStyleSheet("color: palette(text);")
            name.setToolTip(tooltip)
            value = QLabel("—")
            value.setWordWrap(True)
            value.setStyleSheet("color: palette(text); font-weight: 600;")
            value.setToolTip(tooltip)
            self.metric_labels[key] = name
            self.metric_values[key] = value
            self.metric_grid.addWidget(name, index // 4 * 2, index % 4)
            self.metric_grid.addWidget(value, index // 4 * 2 + 1, index % 4)
        root.addWidget(self.metric_group)

        self.status_detail = QLabel()
        self.status_detail.setWordWrap(True)
        self.status_detail.setStyleSheet("color: palette(text);")
        root.addWidget(self.status_detail)

        chart_container = QWidget()
        chart_layout = QVBoxLayout(chart_container)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        self.rate_plot = self._new_plot("Rate measurements", "Rate (as labeled in legend)")
        self.duration_plot = self._new_plot("Host call duration", "Duration (ms)")
        self.activity_plot = self._new_plot("Recorded activity counts", "Counts, as labeled in legend")
        self.activity_empty_label = QLabel("No polling, skip, or CHDKPTP counter data was recorded for this benchmark.")
        self.activity_empty_label.setWordWrap(True)
        self.activity_empty_label.setStyleSheet("color: palette(text); padding: 8px;")
        self.activity_empty_label.hide()
        chart_layout.addWidget(self.rate_plot)
        chart_layout.addWidget(self.duration_plot)
        chart_layout.addWidget(self.activity_empty_label)
        chart_layout.addWidget(self.activity_plot)
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setWidget(chart_container)
        root.addWidget(self.chart_scroll, 1)

        self._load_catalog()
        self._build_historical_plots()
        self._set_mode("replay")

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.canon_controls.hide()

    @staticmethod
    def _metric_definitions() -> tuple[tuple[str, str, str], ...]:
        return (
            ("requested", "Requested FPS", "Requested target; it is not an achieved rate."),
            ("main_rate", "Measured rate", "See the displayed metric name and provenance; payload fetches are not sensor exposures."),
            ("content", "Content changes/s", "Estimated threshold-classified changes where the CSV recorded them; not sensor FPS."),
            ("content_samples", "Content samples (count)", "Number of decoded content samples compared in this historical observation."),
            ("exact_repeats", "Exact image repeats (count)", "Reported exact repeated-content count; it does not identify repeated sensor exposures."),
            ("duration", "Host call duration (ms)", "CHDKPTP fetch-call timing or OpenCV read() call duration, depending on mode; not physical USB latency."),
            ("throughput", "Reported throughput (kbps)", "CSV-reported throughput in its published kbps unit; shown only where present."),
            ("usb_speed", "Reported USB speed (kbps)", "The dataset's reported USB speed in kbps; this is not CEL's measured transfer rate."),
            ("preview", "GUI preview updates/s", "CEL preview updates measured separately from capture; not physical display scanout."),
            ("skips", "Software skips (count)", "Reported software skip/counter value, not camera sensor dropped frames."),
            ("polls", "Polls / attempts (count)", "Stored CHDKPTP poll, fetch-attempt, or device-check counts for the selected sample."),
            ("state", "Connection / row state", "Live capture worker state, or historical benchmark row status. Historical status is not connection state."),
        )

    @staticmethod
    def _new_plot(title: str, y_label: str) -> pg.PlotWidget:
        plot = pg.PlotWidget()
        plot.setBackground(None)
        plot.setMinimumHeight(190)
        plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setTitle(title)
        plot.setLabel("bottom", "Replay timeline (s; stages concatenated)")
        plot.setLabel("left", y_label)
        for axis_name in ("bottom", "left"):
            plot.getAxis(axis_name).enableAutoSIPrefix(False)
        PerformanceMonitorWorkspace._place_legend_below_plot(plot)
        return plot

    @staticmethod
    def _place_legend_below_plot(plot: pg.PlotWidget) -> None:
        """Keep legend labels in their own layout row, outside plotted data."""
        legend = plot.plotItem.legend
        if legend is None:
            legend = plot.addLegend(offset=(0, 0), colCount=2)
        legend.setParentItem(plot.plotItem)
        plot.plotItem.layout.addItem(legend, 4, 1)

    def _load_catalog(self) -> None:
        try:
            self._datasets = load_builtin_replays()
        except Exception as exc:
            self._datasets = ()
            self.banner.setText(f"Historical replay data could not be loaded: {exc}")
            self.dataset_combo.setEnabled(False)
            self.play_button.setEnabled(False)
        blocker = QSignalBlocker(self.dataset_combo)
        self.dataset_combo.clear()
        for dataset in self._datasets:
            self.dataset_combo.addItem(dataset.title, dataset.filename)
        del blocker
        if self._datasets:
            self._controller = ReplayController(self._datasets[0])

    def _mode_changed(self, _index: int) -> None:
        self._set_mode(str(self.mode_combo.currentData()))

    def _set_mode(self, mode: str) -> None:
        historical = mode == "replay"
        canon = mode == "canon"
        self.dataset_combo.setVisible(historical)
        self.play_button.setVisible(historical)
        self.pause_button.setVisible(historical)
        self.reset_button.setVisible(historical)
        self.replay_position.setVisible(historical)
        self.source_combo.setVisible(not historical)
        self.canon_controls.setVisible(canon)
        self.metric_group.setTitle(
            "Current historical sample" if historical else
            "Current Canon CHDKPTP telemetry sample" if canon else
            "Current live CEL capture snapshot"
        )
        if historical:
            self.metric_labels["main_rate"].setText("Measured rate")
            self.metric_labels["duration"].setText("Host call duration (ms)")
            self._build_historical_plots()
            self.banner.setText(
                "HISTORICAL REPLAY · published Canon CSV measurements · no camera connection. "
                "Rows are played in CSV order; stage-local elapsed values are concatenated only for replay scheduling."
            )
            self._show_current_replay()
        elif canon:
            self.metric_labels["main_rate"].setText("Payload fetch rate / reported FPS")
            self.metric_labels["duration"].setText("CHDKPTP fetch-call duration (ms)")
            self._refresh_canon_sources(force=True)
            self._prepare_canon_plots()
            self._poll_canon()
        else:
            self.metric_labels["main_rate"].setText("Successful OpenCV reads/s")
            self.metric_labels["duration"].setText("OpenCV read() duration (ms)")
            self.banner.setText(
                "LIVE CEL CAPTURE · reads existing capture-worker telemetry rings; this tab never opens a camera. "
                "GUI preview updates are separate from capture and are not physical display presentations."
            )
            self._refresh_live_sources(force=True)
            self._prepare_live_plots()

    def _dataset_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._datasets):
            return
        self._controller = ReplayController(self._datasets[index])
        self._build_historical_plots()
        self.play_button.setText("Play")
        self.play_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self._show_current_replay()

    def _dataset(self) -> ReplayDataset | None:
        index = self.dataset_combo.currentIndex()
        return self._datasets[index] if 0 <= index < len(self._datasets) else None

    def _reading_series(self, dataset: ReplayDataset, metric: Metric) -> list[float]:
        result: list[float] = []
        for row in dataset.rows:
            measurement = row.sample.get(metric)
            if measurement is None or measurement.availability != Availability.AVAILABLE:
                result.append(nan)
            else:
                result.append(float(measurement.value))
        return result

    def _x_values(self, dataset: ReplayDataset) -> list[float]:
        origin = dataset.rows[0].replay_elapsed_s
        return [row.replay_elapsed_s - origin for row in dataset.rows]

    def _build_historical_plots(self) -> None:
        self._clear_plots()
        dataset = self._dataset()
        if dataset is None:
            return
        x = self._x_values(dataset)
        colors = ("#4da3ff", "#58c486", "#f2a65a", "#d675d0", "#e0d66c")
        rate_series = (
            (Metric.REQUESTED_FPS, "Requested target FPS"),
            (Metric.CHDKPTP_GUI_REPORTED_FPS, "CHDKPTP GUI/statistics FPS (reported)"),
            (Metric.CHDKPTP_FETCH_FPS, "Completed CHDKPTP payload fetches/s"),
            (Metric.CONTENT_CHANGE_RATE, "Threshold-classified content changes/s (estimate)"),
        )
        index = 0
        for metric, label in rate_series:
            y = self._reading_series(dataset, metric)
            if any(value == value for value in y):
                self.rate_plot.plot(x, y, pen=pg.mkPen(colors[index % len(colors)], width=1.7), name=label)
                index += 1
        transfer_ms = self._reading_series(dataset, Metric.CHDKPTP_FETCH_DURATION_MS)
        if any(value == value for value in transfer_ms):
            self.duration_plot.plot(
                x, transfer_ms, pen=pg.mkPen("#f2a65a", width=1.7),
                name="Host-observed CHDKPTP fetch-call duration",
            )
        activity = (
            (Metric.CHDKPTP_FETCH_ATTEMPTS, "Payload fetch attempts"),
            (Metric.CHDKPTP_POLL_ATTEMPTS, "CHDKPTP poll attempts"),
            (Metric.DEVICE_POLL_CHECKS, "Device-list checks"),
            (Metric.SOFTWARE_SKIPS, "Software skips"),
            (Metric.CHDKPTP_DROPPED_COUNTER, "CHDKPTP cumulative counter (not sensor drops)"),
        )
        index = 0
        activity_has_data = False
        for metric, label in activity:
            y = self._reading_series(dataset, metric)
            if any(value == value for value in y):
                self.activity_plot.plot(x, y, pen=pg.mkPen(colors[index % len(colors)], width=1.7), name=label)
                activity_has_data = True
                index += 1
        self.activity_plot.setVisible(activity_has_data)
        self.activity_empty_label.setVisible(not activity_has_data)
        for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
            plot.setLabel("bottom", "Replay timeline (s; stages concatenated)")
        self._replay_markers = []
        for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
            marker = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen("#aaaaaa", style=Qt.PenStyle.DashLine),
            )
            plot.addItem(marker)
            self._replay_markers.append(marker)
        self._plot_mode = "replay"
        self._apply_plot_palette()

    def _clear_plots(self) -> None:
        for plot, title, ylabel in (
            (self.rate_plot, "Rate measurements", "Rate (as labeled in legend)"),
            (self.duration_plot, "Host call duration", "Duration (ms)"),
            (self.activity_plot, "Recorded activity counts", "Count per sample / reported counter"),
        ):
            plot.clear()
            self._place_legend_below_plot(plot)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setTitle(title)
            plot.setLabel("left", ylabel)
            plot.setLabel("bottom", "Replay timeline (s; stages concatenated)")
        self._replay_markers: list[pg.InfiniteLine] = []
        self.activity_plot.show()
        self.activity_empty_label.hide()
        self._live_curves = None
        self._plot_mode = None
        self._apply_plot_palette()

    def play_replay(self) -> None:
        if self._controller is None:
            return
        self._controller.play(time.monotonic_ns())
        self.play_button.setText("Playing")
        self.play_button.setEnabled(False)
        self.pause_button.setEnabled(True)

    def pause_replay(self) -> None:
        if self._controller is None:
            return
        self._controller.pause(time.monotonic_ns())
        self.play_button.setText("Resume")
        self.play_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.play_button.setText("Resume")
        self._show_current_replay()

    def reset_replay(self) -> None:
        if self._controller is None:
            return
        self._controller.reset()
        self.play_button.setText("Play")
        self.play_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self._show_current_replay()

    def _show_current_replay(self) -> None:
        if self._controller is None:
            return
        row = self._controller.current
        sample = row.sample
        values = {
            "requested": _display_value(sample, Metric.REQUESTED_FPS),
            "main_rate": self._preferred_rate(sample),
            "content": _display_value(sample, Metric.CONTENT_CHANGE_RATE),
            "content_samples": _display_value(sample, Metric.CONTENT_SAMPLE_COUNT),
            "exact_repeats": _display_value(sample, Metric.EXACT_REPEAT_COUNT),
            "duration": _display_value(sample, Metric.CHDKPTP_FETCH_DURATION_MS, 2),
            "throughput": _display_value(sample, Metric.CHDKPTP_REPORTED_THROUGHPUT_KBPS, 2),
            "usb_speed": _display_value(sample, Metric.CHDKPTP_REPORTED_USB_SPEED_KBPS, 2),
            "preview": _display_value(sample, Metric.GUI_PREVIEW_UPDATE_FPS),
            "skips": self._skip_label(sample),
            "polls": self._poll_label(sample),
            "state": f"{row.status} (benchmark row; not connection state)",
        }
        self._set_values(values)
        location = []
        if row.condition:
            location.append(row.condition.replace("_", " "))
        if row.stage:
            location.append(f"stage {row.stage}")
        detail = " · ".join(location) or "recorded sequence"
        self.status_detail.setText(
            f"{detail} · source elapsed {row.elapsed_s:.2f} s · replay position "
            f"{self._controller.position_seconds:.2f} s of {self._controller.dataset.duration_seconds:.2f} s. "
            "Playback time concatenates stage-local segments and is not a common wall-clock timeline."
        )
        position = self._controller.index + 1
        self.replay_position.setText(f"Sample {position}/{len(self._controller.dataset.rows)} · {row.status}")
        if self._replay_markers:
            x = row.replay_elapsed_s - self._controller.dataset.rows[0].replay_elapsed_s
            for marker in self._replay_markers:
                marker.setValue(x)
        if not self._controller.playing and position == len(self._controller.dataset.rows):
            self.play_button.setText("Play")
            self.play_button.setEnabled(True)
        self.pause_button.setEnabled(self._controller.playing)

    @staticmethod
    def _preferred_rate(sample: TelemetrySample) -> str:
        for metric in (Metric.CHDKPTP_FETCH_FPS, Metric.CHDKPTP_GUI_REPORTED_FPS):
            measurement = sample.get(metric)
            if measurement and measurement.availability == Availability.AVAILABLE:
                label = "payload fetches/s" if metric == Metric.CHDKPTP_FETCH_FPS else "CHDKPTP-reported FPS"
                return f"{measurement.value:.1f} · {label}"
        return "Not recorded"

    @staticmethod
    def _skip_label(sample: TelemetrySample) -> str:
        skips = sample.get(Metric.SOFTWARE_SKIPS)
        if skips and skips.availability == Availability.AVAILABLE:
            return f"{skips.value} · software skips/sample"
        dropped = sample.get(Metric.CHDKPTP_DROPPED_COUNTER)
        if dropped and dropped.availability == Availability.AVAILABLE:
            return f"{dropped.value} · CHDKPTP cumulative counter"
        return "Not recorded"

    @staticmethod
    def _poll_label(sample: TelemetrySample) -> str:
        parts = []
        for metric, name in (
            (Metric.CHDKPTP_FETCH_ATTEMPTS, "fetch attempts"),
            (Metric.CHDKPTP_POLL_ATTEMPTS, "poll attempts"),
            (Metric.DEVICE_POLL_CHECKS, "device checks"),
        ):
            reading = sample.get(metric)
            if reading and reading.availability == Availability.AVAILABLE:
                parts.append(f"{reading.value} {name}")
        return ", ".join(parts) if parts else "Not recorded"

    def _apply_plot_palette(self) -> None:
        color = self.palette().color(QPalette.ColorRole.Text)
        for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
            title = plot.plotItem.titleLabel
            title.setText(title.text, color=color)
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setPen(color)
                axis.setTextPen(color)
                axis.label.setDefaultTextColor(color)
                axis.enableAutoSIPrefix(False)
            if plot.plotItem.legend is not None:
                plot.plotItem.legend.setLabelTextColor(color)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            if hasattr(self, "rate_plot"):
                self._apply_plot_palette()

    def _set_values(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            self.metric_values[key].setText(value)

    def _tick(self) -> None:
        mode = self.mode_combo.currentData()
        if mode == "replay":
            if self._controller is not None and self._controller.playing:
                self._controller.advance(time.monotonic_ns())
                self._show_current_replay()
                if not self._controller.playing:
                    self.play_button.setText("Play")
                    self.play_button.setEnabled(True)
            return
        if mode == "canon":
            self._poll_canon()
            return
        self._poll_live()

    def _refresh_live_sources(self, *, force: bool = False) -> None:
        try:
            sources = tuple(self._source_provider())
        except Exception as exc:
            self.status_detail.setText(f"Could not read CEL capture sources: {exc}")
            sources = ()
        fingerprint = tuple((source.key, id(source.worker)) for source in sources)
        if not force and fingerprint == self._live_fingerprint:
            self._live_sources = {source.key: source for source in sources}
            return
        previous = self._live_source_key or str(self.source_combo.currentData() or "")
        blocker = QSignalBlocker(self.source_combo)
        self.source_combo.clear()
        self._live_sources = {}
        for source in sources:
            self._live_sources[source.key] = source
            row = self.source_combo.count()
            self.source_combo.addItem(source.label, source.key)
            self.source_combo.setItemData(row, source.label, Qt.ItemDataRole.ToolTipRole)
        index = self.source_combo.findData(previous)
        if index < 0 and self.source_combo.count():
            index = 0
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
            self._live_source_key = str(self.source_combo.currentData())
        else:
            self._live_source_key = None
        del blocker
        self.source_combo.setEnabled(bool(sources))
        self._reset_live_history()
        self._live_fingerprint = fingerprint
        if self.mode_combo.currentData() == "live":
            self._prepare_live_plots()

    def _live_source_changed(self, _index: int) -> None:
        self._live_source_key = str(self.source_combo.currentData()) if self.source_combo.currentData() else None
        self._reset_live_history()
        self._canon_source_key = self._live_source_key
        self._reset_canon_history()
        self._clear_live_plot_data()
        self._clear_canon_plot_data()

    def _reset_live_history(self) -> None:
        self._live_cursor = None
        self._live_origin_ns = None
        self._latest_live_sample = None
        for values in (self._live_times, self._live_capture_rates, self._live_intervals,
                       self._live_read_durations, self._preview_times, self._preview_rates):
            values.clear()

    def _reset_canon_history(self) -> None:
        self._canon_cursor = None
        self._canon_origin_ns = None
        self._canon_last_host_ns = None
        self._latest_canon_sample = None
        for values in (
            self._canon_times, self._canon_fetch_rates, self._canon_reported_rates,
            self._canon_content_rates, self._canon_fetch_durations,
            self._canon_receive_gaps, self._canon_skips, self._canon_polls,
            self._canon_device_checks,
        ):
            values.clear()

    def _toggle_canon_listener(self) -> None:
        state = self._canon_consumer.snapshot().state
        if state in (ConsumerState.STOPPED, ConsumerState.ERROR):
            try:
                self._canon_consumer.start()
                self.bridge_status.setText("Starting listener…")
            except Exception as exc:
                self.bridge_status.setText(f"Listener could not start: {exc}")
        else:
            self._canon_consumer.request_stop()
            self.bridge_status.setText("Stopping listener…")

    def _refresh_canon_sources(self, *, force: bool = False) -> None:
        sources = self._canon_consumer.sources
        fingerprint = tuple((source.key, source.label, source.simulated) for source in sources)
        if not force and fingerprint == self._canon_fingerprint:
            return
        previous = self._canon_source_key or str(self.source_combo.currentData() or "")
        blocker = QSignalBlocker(self.source_combo)
        self.source_combo.clear()
        for source in sources:
            suffix = " · SIMULATED" if source.simulated else ""
            row = self.source_combo.count()
            label = f"{source.label}{suffix}"
            self.source_combo.addItem(label, source.key)
            self.source_combo.setItemData(row, label, Qt.ItemDataRole.ToolTipRole)
        index = self.source_combo.findData(previous)
        if index < 0 and self.source_combo.count():
            index = 0
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
            self._canon_source_key = str(self.source_combo.currentData())
            self._live_source_key = self._canon_source_key
        else:
            self._canon_source_key = None
            self._live_source_key = None
        del blocker
        self.source_combo.setEnabled(bool(sources))
        self.source_combo.setToolTip(
            self.source_combo.currentText() or "Waiting for a CHDKPTP telemetry producer."
        )
        self._canon_fingerprint = fingerprint
        self._reset_canon_history()
        if self.mode_combo.currentData() == "canon":
            self._prepare_canon_plots()

    def _poll_canon(self) -> None:
        self._refresh_canon_sources()
        snapshot = self._canon_consumer.snapshot()
        self.bridge_status.setText(self._canon_state_label(snapshot.state))
        self.listen_button.setText(
            "Stop Listener" if snapshot.state not in (ConsumerState.STOPPED, ConsumerState.ERROR) else "Listen for CHDKPTP"
        )
        self.listen_button.setEnabled(snapshot.state != ConsumerState.STARTING)
        read = self._canon_consumer.telemetry_buffer.read_since(self._canon_cursor)
        self._canon_cursor = read.next_cursor
        selected = self._canon_source_key
        for _ring_sequence, sample in read.entries:
            if sample.source != selected:
                continue
            host_time = sample.received_timestamp_ns
            if host_time is None:
                continue
            if self._canon_origin_ns is None:
                self._canon_origin_ns = host_time
            elapsed_s = (host_time - self._canon_origin_ns) / 1_000_000_000
            if (
                self._canon_last_host_ns is not None
                and host_time - self._canon_last_host_ns >= self._canon_consumer.stale_after_ns
            ):
                # A NaN vertex breaks the plotted segment across a producer gap.
                # It preserves the full host-receipt timeline without inventing continuity.
                self._canon_times.append(elapsed_s)
                for series in (
                    self._canon_fetch_rates, self._canon_reported_rates,
                    self._canon_content_rates, self._canon_fetch_durations,
                    self._canon_receive_gaps, self._canon_skips,
                    self._canon_polls, self._canon_device_checks,
                ):
                    series.append(nan)
            self._canon_last_host_ns = host_time
            self._latest_canon_sample = sample
            self._canon_times.append(elapsed_s)
            self._canon_fetch_rates.append(self._number_value(sample, Metric.CHDKPTP_FETCH_FPS))
            self._canon_reported_rates.append(self._number_value(sample, Metric.CHDKPTP_GUI_REPORTED_FPS))
            self._canon_content_rates.append(self._number_value(sample, Metric.CONTENT_CHANGE_RATE))
            self._canon_fetch_durations.append(self._number_value(sample, Metric.CHDKPTP_FETCH_DURATION_MS))
            self._canon_receive_gaps.append(self._number_value(sample, Metric.HOST_TELEMETRY_RECEIVE_GAP_MS))
            self._canon_skips.append(self._number_value(sample, Metric.SOFTWARE_SKIPS))
            self._canon_polls.append(self._number_value(sample, Metric.CHDKPTP_POLL_ATTEMPTS))
            self._canon_device_checks.append(self._number_value(sample, Metric.DEVICE_POLL_CHECKS))
        self._update_canon_plots()
        source = next((item for item in self._canon_consumer.sources if item.key == selected), None)
        selected_sample_age_s = None
        if self._latest_canon_sample is None:
            unavailable = "Unavailable · no sample received"
            self._set_values({
                "requested": unavailable, "main_rate": unavailable, "content": unavailable,
                "content_samples": unavailable, "exact_repeats": unavailable,
                "duration": unavailable, "throughput": unavailable, "usb_speed": unavailable,
                "preview": "Unavailable · CHDKPTP producer does not measure CEL presentation",
                "skips": unavailable, "polls": unavailable,
                "state": "Waiting for source" if source is None else "Waiting for first sample",
            })
        else:
            sample = self._latest_canon_sample
            state_reading = sample.get(Metric.CONNECTION_STATE)
            camera_state = (
                str(state_reading.value)
                if state_reading and state_reading.availability == Availability.AVAILABLE
                else None
            )
            state_text = self._canon_state_label(snapshot.state)
            if sample.received_timestamp_ns is not None:
                selected_sample_age_s = max(0, time.monotonic_ns() - sample.received_timestamp_ns) / 1_000_000_000
                if selected_sample_age_s * 1_000_000_000 >= self._canon_consumer.stale_after_ns:
                    state_text += " · selected source stale"
            if camera_state:
                state_text += f" · last camera state: {camera_state}"
            self._set_values({
                "requested": _display_value(sample, Metric.REQUESTED_FPS),
                "main_rate": self._preferred_canon_rate(sample),
                "content": _display_value(sample, Metric.CONTENT_CHANGE_RATE),
                "content_samples": _display_value(sample, Metric.CONTENT_SAMPLE_COUNT),
                "exact_repeats": _display_value(sample, Metric.EXACT_REPEAT_COUNT),
                "duration": _display_value(sample, Metric.CHDKPTP_FETCH_DURATION_MS, 2),
                "throughput": _display_value(sample, Metric.CHDKPTP_REPORTED_THROUGHPUT_KBPS, 2),
                "usb_speed": _display_value(sample, Metric.CHDKPTP_REPORTED_USB_SPEED_KBPS, 2),
                "preview": "Unavailable · producer does not measure CEL GUI presentation",
                "skips": self._skip_label(sample),
                "polls": self._poll_label(sample),
                "state": state_text,
            })
        simulation_text = "SIMULATED source" if source and source.simulated else "CHDKPTP source"
        warnings = (
            f"Rejected messages: {snapshot.rejected_messages}; oversized: {snapshot.oversized_messages}; "
            f"sequence gaps: {snapshot.sequence_gaps}; ring overwrites: {snapshot.ring_overwrites}."
        )
        source_clock = (
            self._latest_canon_sample.clock_domain.value
            if self._latest_canon_sample else "producer"
        )
        details = (
            f"{simulation_text}. Source timestamps use {source_clock}; charts use CEL host-monotonic receipt time. "
            "Producer and CEL clocks are not subtracted. CHDKPTP fetch-call duration is producer-reported; "
            "IPC receive gaps are arrival spacing, not camera transfer latency. "
            f"{warnings}"
        )
        if snapshot.state != ConsumerState.CONNECTED:
            details += " Displayed metric values are the last valid sample; transport is not currently receiving."
        if selected_sample_age_s is not None and selected_sample_age_s * 1_000_000_000 >= self._canon_consumer.stale_after_ns:
            details += f" Selected source is stale; its last valid sample arrived {selected_sample_age_s:.1f} s ago."
        if read.missed_entries:
            details += f" The UI ring cursor missed {read.missed_entries} retained sample(s)."
        if snapshot.error:
            details += f" {snapshot.error}"
        visible = (
            f"{simulation_text} · source clock unsynchronized · receipt-time chart · "
            f"rejected {snapshot.rejected_messages}, oversized {snapshot.oversized_messages}, "
            f"gaps {snapshot.sequence_gaps}, overwrites {snapshot.ring_overwrites}"
        )
        if selected_sample_age_s is not None and selected_sample_age_s * 1_000_000_000 >= self._canon_consumer.stale_after_ns:
            visible += " · source stale"
        if snapshot.error:
            visible += " · listener error"
        self.status_detail.setText(visible)
        self.status_detail.setToolTip(details)
        label = source.label if source else "waiting for producer"
        self.banner.setText(
            f"LIVE CANON CHDKPTP · {simulation_text.upper()} · {label} · local telemetry only; CEL does not open USB/PTP."
        )

    @staticmethod
    def _canon_state_label(state: ConsumerState) -> str:
        return {
            ConsumerState.STOPPED: "Listener stopped",
            ConsumerState.STARTING: "Listener starting",
            ConsumerState.LISTENING: "Listening · waiting for producer",
            ConsumerState.CONNECTED: "Producer connected",
            ConsumerState.STALE: "Stale · no recent telemetry",
            ConsumerState.ERROR: "Listener error",
        }[state]

    @staticmethod
    def _preferred_canon_rate(sample: TelemetrySample) -> str:
        fetch = sample.get(Metric.CHDKPTP_FETCH_FPS)
        if fetch and fetch.availability == Availability.AVAILABLE:
            return f"{fetch.value:.1f} · completed payload fetches/s"
        reported = sample.get(Metric.CHDKPTP_GUI_REPORTED_FPS)
        if reported and reported.availability == Availability.AVAILABLE:
            return f"{reported.value:.1f} · CHDKPTP-reported FPS"
        return "Unavailable / not recorded"

    def _update_canon_plots(self) -> None:
        if self._plot_mode != "canon" or self._canon_curves is None:
            self._prepare_canon_plots()
        assert self._canon_curves is not None
        fetch, reported, content, duration, receive_gap, skips, polls, device_checks = self._canon_curves
        x = list(self._canon_times)
        for curve, values in (
            (fetch, self._canon_fetch_rates), (reported, self._canon_reported_rates),
            (content, self._canon_content_rates), (duration, self._canon_fetch_durations),
            (receive_gap, self._canon_receive_gaps), (skips, self._canon_skips),
            (polls, self._canon_polls), (device_checks, self._canon_device_checks),
        ):
            curve.setData(x, list(values))
        if x:
            right = x[-1]
            window = self.canon_window_combo.currentData()
            left = x[0] if window is None else max(x[0], right - float(window))
            if right <= left:
                right = left + 1.0
            for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
                plot.setXRange(left, right, padding=0)

    def _prepare_canon_plots(self) -> None:
        if self._plot_mode == "canon" and self._canon_curves is not None:
            self._clear_canon_plot_data()
            return
        self._clear_plots()
        self.activity_plot.show()
        self.activity_empty_label.hide()
        self._plot_mode = "canon"
        for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
            plot.setLabel("bottom", "Seconds since first CEL host receipt")
        self.rate_plot.setTitle("Canon CHDKPTP measurements · source rates are not sensor FPS")
        self.duration_plot.setTitle("Producer fetch-call duration and CEL IPC arrival gap")
        self.activity_plot.setTitle("CHDKPTP polling and software-skip counters")
        curves = (
            self.rate_plot.plot([], [], pen=pg.mkPen("#4da3ff", width=1.6), name="Completed payload fetches/s"),
            self.rate_plot.plot([], [], pen=pg.mkPen("#58c486", width=1.6), name="CHDKPTP-reported FPS"),
            self.rate_plot.plot([], [], pen=pg.mkPen("#d675d0", width=1.6), name="Content changes/s (estimate)"),
            self.duration_plot.plot([], [], pen=pg.mkPen("#f2a65a", width=1.6), name="CHDKPTP fetch-call duration (ms)"),
            self.duration_plot.plot([], [], pen=pg.mkPen("#e0d66c", width=1.6), name="CEL IPC receive gap (ms)"),
            self.activity_plot.plot([], [], pen=pg.mkPen("#d675d0", width=1.6), name="Software skips"),
            self.activity_plot.plot([], [], pen=pg.mkPen("#4da3ff", width=1.6), name="CHDKPTP poll attempts"),
            self.activity_plot.plot([], [], pen=pg.mkPen("#58c486", width=1.6), name="Device-list checks"),
        )
        self._canon_curves = curves
        self._apply_plot_palette()

    def _clear_canon_plot_data(self) -> None:
        if self._canon_curves is not None:
            for curve in self._canon_curves:
                curve.setData([], [])

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Request the listener to stop without joining from the GUI thread."""
        self._canon_consumer.shutdown()

    def _poll_live(self) -> None:
        self._refresh_live_sources()
        source = self._live_sources.get(self._live_source_key or "")
        if source is None:
            if self._plot_mode != "live":
                self._prepare_live_plots()
            self._clear_live_plot_data()
            self.banner.setText(
                "LIVE CEL CAPTURE · no existing capture worker is available. Start preview in a workbench; "
                "this monitor will not discover or open cameras."
            )
            self._set_values({
                "requested": "Unavailable", "main_rate": "—", "content": "Unavailable",
                "content_samples": "Unavailable", "exact_repeats": "Unavailable",
                "duration": "Unavailable", "throughput": "Unavailable", "usb_speed": "Unavailable",
                "preview": "Unavailable", "skips": "Unavailable",
                "polls": "Unavailable", "state": "No active source",
            })
            self.status_detail.setText("No camera has been opened by the Performance Monitor.")
            return

        worker = source.worker
        snapshot = worker.snapshot()
        read = worker.telemetry_buffer.read_since(self._live_cursor)
        self._live_cursor = read.next_cursor
        for _buffer_sequence, sample in read.entries:
            self._latest_live_sample = sample
            if self._live_origin_ns is None:
                self._live_origin_ns = sample.timestamp_ns
            x = (sample.timestamp_ns - self._live_origin_ns) / 1_000_000_000
            self._live_times.append(x)
            self._live_capture_rates.append(self._number_value(sample, Metric.OPENCV_CAPTURE_FPS))
            self._live_intervals.append(self._number_value(sample, Metric.FRAME_INTERVAL_MS))
            self._live_read_durations.append(self._number_value(sample, Metric.OPENCV_READ_CALL_DURATION_MS))

        now_ns = time.monotonic_ns()
        if self._live_origin_ns is None:
            self._live_origin_ns = now_ns
        self._preview_times.append((now_ns - self._live_origin_ns) / 1_000_000_000)
        self._preview_rates.append(max(0.0, source.preview_update_fps))
        self._update_live_plots()
        requested = snapshot.requested.fps
        self._set_values({
            "requested": f"{requested:g} · requested" if requested is not None else "No requested FPS",
            "main_rate": f"{snapshot.capture_fps:.1f} · successful OpenCV reads/s",
            "content": "Unavailable · no content classifier",
            "content_samples": "Unavailable · no image comparison",
            "exact_repeats": "Unavailable · no image comparison",
            "duration": _display_value(self._latest_live_sample, Metric.OPENCV_READ_CALL_DURATION_MS, 2)
            if self._latest_live_sample else "—",
            "throughput": "Unavailable · OpenCV read count only",
            "usb_speed": "Unavailable · no CHDKPTP USB field",
            "preview": f"{source.preview_update_fps:.1f} · GUI updates/s",
            "skips": "Unavailable · no software skip counter",
            "polls": "Unavailable · no CHDKPTP polling",
            "state": snapshot.state.value.replace("_", " ").title(),
        })
        note = (
            "OpenCV read() duration includes backend/driver waiting and is not USB-only latency. "
            "Successful reads are not verified sensor exposures. Preview FPS counts CEL GUI preview updates, "
            "not physical display scanout."
        )
        if read.missed_entries:
            note += f" Monitor ring overwrote {read.missed_entries} unread telemetry sample(s); capture was not paused."
        self.status_detail.setText(note)
        self.banner.setText(
            f"LIVE CEL CAPTURE · {source.label} · existing worker only · no competing camera connection."
        )

    @staticmethod
    def _number_value(sample: TelemetrySample, metric: Metric) -> float:
        measurement = sample.get(metric)
        if measurement is None or measurement.availability != Availability.AVAILABLE:
            return nan
        return float(measurement.value)

    def _update_live_plots(self) -> None:
        if self._plot_mode != "live" or self._live_curves is None:
            self._prepare_live_plots()
        assert self._live_curves is not None
        capture_curve, preview_curve, duration_curve, interval_curve = self._live_curves
        capture_curve.setData(list(self._live_times), list(self._live_capture_rates))
        preview_curve.setData(list(self._preview_times), list(self._preview_rates))
        duration_curve.setData(list(self._live_times), list(self._live_read_durations))
        interval_curve.setData(list(self._live_times), list(self._live_intervals))

    def _prepare_live_plots(self) -> None:
        if self._plot_mode == "live" and self._live_curves is not None:
            self._clear_live_plot_data()
            return
        self._clear_plots()
        self.activity_plot.show()
        self.activity_empty_label.hide()
        self._plot_mode = "live"
        for plot in (self.rate_plot, self.duration_plot, self.activity_plot):
            plot.setLabel("bottom", "Seconds since first host sample")
        self.duration_plot.setTitle("Host-observed OpenCV read() call duration; not USB latency")
        self.activity_plot.setTitle(
            "Host-observed successful read-return interval; not a sensor exposure interval"
        )
        capture_curve = self.rate_plot.plot(
            [], [], pen=pg.mkPen("#4da3ff", width=1.6), name="Successful OpenCV reads/s"
        )
        preview_curve = self.rate_plot.plot(
            [], [], pen=pg.mkPen("#58c486", width=1.6), name="GUI preview updates/s"
        )
        duration_curve = self.duration_plot.plot(
            [], [], pen=pg.mkPen("#f2a65a", width=1.6), name="OpenCV read() call duration"
        )
        interval_curve = self.activity_plot.plot(
            [], [], pen=pg.mkPen("#d675d0", width=1.6), name="Successful read-return interval"
        )
        self._live_curves = (capture_curve, preview_curve, duration_curve, interval_curve)
        self._apply_plot_palette()

    def _clear_live_plot_data(self) -> None:
        if self._live_curves is not None:
            for curve in self._live_curves:
                curve.setData([], [])
