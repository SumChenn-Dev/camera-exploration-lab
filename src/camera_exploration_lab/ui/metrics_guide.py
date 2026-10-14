"""Contextual, plain-language explanations of stored camera measurements."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
)


@dataclass(frozen=True, slots=True)
class MetricGuideEntry:
    key: str
    title: str
    meaning: str
    units: str
    example: str
    interpretation: str
    misunderstanding: str
    method: str

    def formatted(self) -> str:
        return "\n\n".join((
            f"Plain-language meaning\n{self.meaning}",
            f"Units\n{self.units}",
            f"Example\n{self.example}",
            f"How to interpret it\n{self.interpretation}",
            f"Common misunderstanding\n{self.misunderstanding}",
            f"Definition or measurement method\n{self.method}",
        ))


METRIC_GUIDE: tuple[MetricGuideEntry, ...] = (
    MetricGuideEntry(
        "capture_fps", "Requested FPS and observed capture FPS",
        "Requested FPS is the rate passed to the camera driver. Observed capture FPS is the host-side rate at which capture frames arrive or are read.",
        "frames/s (FPS)",
        "SYNTHETIC: a request of 60 FPS with 1,174 frame intervals over 20 seconds gives about 58.7 observed FPS.",
        "Compare the request, driver readback, and observed rate separately. A difference is useful evidence about driver or host behavior.",
        "A requested or driver-reported 60 FPS does not prove that 60 frames arrived each second. A higher FPS is not automatically a better experiment.",
        "Live capture FPS uses successful host read timestamps. Recorded capture FPS is derived from capture-scoped intervals, or shown from an explicitly stored capture summary when raw intervals are unavailable.",
    ),
    MetricGuideEntry(
        "preview_fps", "GUI preview FPS",
        "The rate at which the application updates the displayed preview. The capture worker can deliver more frames than the GUI paints.",
        "preview updates/s (FPS)",
        "SYNTHETIC: a camera may deliver 187 frames/s while the desktop preview updates at 31 frames/s.",
        "Use it to understand display responsiveness and rendering load alongside capture FPS.",
        "Preview FPS is not camera capture FPS, sensor frame rate, or evidence that every captured frame was displayed.",
        "Count preview updates over the host monotonic time window used by the GUI. Preview intentionally reads the newest frame from a one-frame overwrite slot.",
    ),
    MetricGuideEntry(
        "processing_fps", "Processing FPS",
        "The rate at which an optional image-processing stage completes work on frames. This is separate from capture and preview.",
        "processed frames/s (FPS)",
        "SYNTHETIC: a future tracker might process 24 frames/s while capture delivers 60 frames/s.",
        "Use it to measure a named processing stage and compare it with that stage's input load.",
        "The current application has no general processing pipeline; an unavailable value does not mean zero processing capacity.",
        "When implemented, calculate completed processing items divided by elapsed monotonic time and save them with processing scope and method metadata.",
    ),
    MetricGuideEntry(
        "frame_count", "Frame count",
        "The number of frames or frame-arrival records represented by the stored source and session summary.",
        "frames (count)",
        "SYNTHETIC: a session summary reports 1,200 captured frames.",
        "Read the field's source and session boundaries before comparing counts. Counts are useful together with duration and failures.",
        "A count does not establish which sensor exposures occurred, whether the camera skipped an internal sequence number, or whether every frame was saved as an image.",
        "For new experiments the saved recorder summary reports the captured-frame count. Imported sources may provide a count field or only per-row evidence; the Analysis view identifies the source.",
    ),
    MetricGuideEntry(
        "frame_interval", "Frame interval",
        "The time between two host-observed frame arrivals. It describes spacing at the application capture boundary.",
        "milliseconds (ms)",
        "SYNTHETIC: host arrival times 16.0 ms apart produce a 16.0 ms interval.",
        "A smaller interval means closer host arrivals for those samples. Examine the timeline and percentiles for variation and spikes.",
        "Frame interval is not exposure duration, sensor readout time, or sensor-to-display latency. An interval can also cross a recording start boundary in some legacy data.",
        "For new recordings, subtract successive host monotonic timestamps. The first interval may be unavailable when the previous arrival preceded the recording boundary.",
    ),
    MetricGuideEntry(
        "mean_median", "Mean and median",
        "The mean is the arithmetic average. The median is the middle value after sorting and is less affected by a few extreme values.",
        "The unit of the measured series, such as ms or frames/s.",
        "SYNTHETIC: intervals [10, 10, 10, 70] ms have a mean of 25 ms and median of 10 ms.",
        "A large mean-to-median gap can indicate a long tail or outliers. Use the distribution and timeline to understand why.",
        "Neither statistic alone says whether a camera is better overall. A low interval can come with other tradeoffs such as resolution or image quality.",
        "Mean = sum(values) / n. The Analysis median averages the two central values when n is even; it uses the center value when n is odd.",
    ),
    MetricGuideEntry(
        "percentiles", "p95 and p99",
        "p95 is the value at or below which about 95% of samples fall; p99 uses 99%. They describe the slower tail of interval distributions.",
        "The unit of the measured series, usually ms for frame intervals.",
        "SYNTHETIC: p95 = 20 ms means 95% of the analyzed intervals are at or below 20 ms under this dataset's percentile rule.",
        "Compare p95 and p99 with median, sample count, and the plotted trace. Tail values can reveal occasional stalls hidden by a mean.",
        "Percentiles are not maximum latency guarantees, and p99 from a small sample may be determined by only one or two observations.",
        "For raw intervals, p95 and p99 use nearest rank: the value at sorted index ceil(p × n) − 1. Imported saved percentiles are shown as stored when raw intervals are absent.",
    ),
    MetricGuideEntry(
        "jitter", "Jitter and timing variability",
        "Jitter is a general term for variation in frame timing. It should always name the statistic used to quantify that variation.",
        "Often ms for interval spread, or percent for a coefficient of variation.",
        "SYNTHETIC: intervals that alternate between 15 and 17 ms vary more than a steady series of 16 ms intervals, even if their means match.",
        "Use the interval trace, percentiles, and a declared spread statistic together. The Lab does not currently present a dedicated jitter score.",
        "Calling a stream 'low jitter' without a formula, sample window, and timing method is not reproducible. A single percentile is not a complete stability score.",
        "Possible future definitions include standard deviation of intervals (ms) or coefficient of variation = standard deviation / mean (dimensionless, often shown as %). These are not currently saved Lab metrics.",
    ),
    MetricGuideEntry(
        "read_failures", "Read failures and dropped frames",
        "A read failure is an unsuccessful capture API read observed by the host. A dropped frame is a specific source frame known to be missing.",
        "failed read calls (count)",
        "SYNTHETIC: two failed read calls are counted, but the camera exposes no sequence number that identifies two particular sensor frames as dropped.",
        "Use failure counts with session duration and capture state. Investigate repeated failures or camera unavailability.",
        "A failed `read()` does not by itself prove a sensor dropped a frame; the API may not expose sequence numbers or the cause of the failure.",
        "The capture worker counts unsuccessful OpenCV `VideoCapture.read()` results. Imported failure fields may have source-specific semantics and are labeled accordingly.",
    ),
    MetricGuideEntry(
        "host_timestamps", "Host frame-arrival timestamps",
        "These timestamps record when the host application observes a frame after the capture read returns.",
        "UTC wall-clock timestamp for provenance; monotonic nanoseconds for intervals and elapsed time.",
        "SYNTHETIC: an arrival may be stamped 12:00:00.016 UTC and have a 16 ms monotonic interval from its predecessor.",
        "They support repeatable host-throughput analysis and ordering within the recording's timing method.",
        "They are not exposure timestamps, camera hardware clocks, or shared clocks across two cameras.",
        "New recording intervals and elapsed time use the host monotonic clock; UTC values are captured after OpenCV read returns. Separate sessions are not synchronized by this timestamp alone.",
    ),
    MetricGuideEntry(
        "latency", "Latency and current measurement limits",
        "Latency is a delay between explicitly defined events, such as an optical stimulus and its appearance in a captured frame.",
        "Usually milliseconds, with a stated start event, end event, and uncertainty.",
        "SYNTHETIC: a measured display-to-camera round trip of 40 ms includes display, camera, and host-path contributions; it is not a sensor-only delay.",
        "A valid latency experiment needs a defined stimulus/detection protocol and an uncertainty budget. Camera Exploration Lab currently does not directly measure absolute sensor latency.",
        "OpenCV `read()` duration and host frame intervals must not be called sensor latency or sensor-to-display latency.",
        "A future external optical/electrical loopback can measure an end-to-end path. It must identify the event boundaries and cannot be inferred from ordinary capture timing alone.",
    ),
    MetricGuideEntry(
        "methodology", "Comparing different timing methodologies",
        "Two runs can differ in scope, clock, sampling, recording boundary, or imported-field definition even when their displayed units match.",
        "No single unit; compare metadata and method labels before comparing numeric values.",
        "SYNTHETIC: a host-monotonic interval series and a CSV summary copied from a driver report are both shown in ms/FPS but do not have the same measurement basis.",
        "Check capture mode, requested and driver-reported values, interval scope, timestamp basis, duration source, and raw-data availability. The Lab warns when these differ.",
        "Matching camera names or units do not prove matching measurement methods. Separate recordings are not simultaneous tests.",
        "The Analysis workspace retains each curve's sample times, does not interpolate missing points, and overlays interval timelines only when timing basis, stream scope, and method match.",
    ),
)


class MetricsGuideDialog(QDialog):
    def __init__(self, topic_key: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Camera Exploration Lab — Metrics Guide")
        self.resize(980, 620)
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.topic_list = QListWidget()
        self.topic_list.setMinimumWidth(220)
        self.explanation = QPlainTextEdit()
        self.explanation.setReadOnly(True)
        self.explanation.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        splitter.addWidget(self.topic_list)
        splitter.addWidget(self.explanation)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._entries = {entry.key: entry for entry in METRIC_GUIDE}
        for entry in METRIC_GUIDE:
            item = QListWidgetItem(entry.title)
            item.setData(Qt.ItemDataRole.UserRole, entry.key)
            self.topic_list.addItem(item)
        self.topic_list.currentItemChanged.connect(self._show_topic)
        self.select_topic(topic_key)

    def select_topic(self, topic_key: str | None) -> None:
        target = topic_key if topic_key in self._entries else METRIC_GUIDE[0].key
        for index in range(self.topic_list.count()):
            if self.topic_list.item(index).data(Qt.ItemDataRole.UserRole) == target:
                self.topic_list.setCurrentRow(index)
                return

    def _show_topic(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.explanation.clear()
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        self.explanation.setPlainText(self._entries[key].formatted())
