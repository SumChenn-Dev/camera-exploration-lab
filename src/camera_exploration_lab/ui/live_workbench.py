"""Preview-first live V4L2 workbench (Phase 1 vertical slice)."""

from __future__ import annotations

import logging
import json
import time
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..capture import CaptureState, OpenCVCaptureWorker, RollingFrameRate, preferred_device_path
from ..capture_modes import CaptureMode, modes_for_camera
from ..db import Database
from ..discovery import V4L2Discovery
from ..models import CameraCandidate, utc_now_iso
from ..recording import ExperimentRecorder, RecordingSnapshot, RecordingState
from .analysis_workspace import AnalysisWorkspace

logger = logging.getLogger(__name__)


def _mode_for_combo(combo: QComboBox) -> CaptureMode | None:
    mode = combo.currentData(Qt.ItemDataRole.UserRole)
    return mode if isinstance(mode, CaptureMode) else None


def _candidate_for_combo(combo: QComboBox) -> CameraCandidate | None:
    candidate = combo.currentData(Qt.ItemDataRole.UserRole)
    return candidate if isinstance(candidate, CameraCandidate) else None


def _frame_to_image(frame: Any) -> QImage:
    """Wrap an OpenCV frame and copy its pixels before the buffer is reused."""
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        raise ValueError("Camera returned a frame without image dimensions")
    height, width = int(shape[0]), int(shape[1])
    if height < 1 or width < 1:
        raise ValueError("Camera returned an empty frame")

    flags = getattr(frame, "flags", None)
    if flags is not None and not flags.c_contiguous:
        frame = frame.copy()
    data = frame.data
    strides = getattr(frame, "strides", None)
    bytes_per_line = int(strides[0]) if strides else width * (int(shape[2]) if len(shape) > 2 else 1)

    if len(shape) == 2:
        image_format = QImage.Format.Format_Grayscale8
    else:
        channels = int(shape[2])
        if channels == 3:
            image_format = QImage.Format.Format_BGR888
        elif channels == 4:
            image_format = QImage.Format.Format_BGRA8888
        else:
            raise ValueError(f"Unsupported camera frame with {channels} channels")
    image = QImage(data, width, height, bytes_per_line, image_format)
    if image.isNull():
        raise ValueError("Could not convert the camera frame for preview")
    return image.copy()


class MetricCard(QFrame):
    def __init__(self, title: str, tooltip: str, initial: str = "—") -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName(title)
        self.setToolTip(tooltip)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("metricTitle")
        self.title_label.setStyleSheet("color: palette(text); font-size: 12px; font-weight: 600;")
        self.title_label.setToolTip(tooltip)
        self.value = QLabel(initial)
        self.value.setToolTip(tooltip)
        self.value.setStyleSheet("font-size: 21px; font-weight: 600;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.value)


class LiveWorkbenchWindow(QMainWindow):
    """Camera preview and a persistent host-side performance panel."""

    def __init__(
        self,
        discovery: V4L2Discovery | None = None,
        database: Database | None = None,
        *,
        capture_factory: Any = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Camera Exploration Lab — Live Workbench")
        self.resize(1440, 900)
        self._discovery = discovery or V4L2Discovery()
        self._database = database or Database()
        self._database.initialize()
        recovered = self._database.recover_interrupted_experiments()
        if recovered:
            logger.warning("Recovered %d experiment(s) interrupted by an earlier application shutdown", recovered)
        self._capture_factory = capture_factory
        self._candidates: list[CameraCandidate] = []
        self._worker: OpenCVCaptureWorker | None = None
        self._selected_stable_id: str | None = None
        self._active_stable_id: str | None = None
        self._last_frame_version = 0
        self._last_image: QImage | None = None
        self._preview_rate = RollingFrameRate()
        self._preview_fps = 0.0
        self._last_recorded_preview_ns = 0
        self._recorder: ExperimentRecorder | None = None
        self._last_recording_snapshot: RecordingSnapshot | None = None

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_live_tab(), "Live Workbench")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.analysis_workspace = AnalysisWorkspace(self._database)
        self.tabs.addTab(self.analysis_workspace, "Analysis")
        self.tabs.addTab(self._build_placeholder_tab(
            "Exports", "Session summaries and raw CSV/JSON exports will be added here."
        ), "Exports")
        self.tabs.currentChanged.connect(self._tab_changed)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(33)
        self._poll_timer.timeout.connect(self._poll_capture)
        self._poll_timer.start()
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(1500)
        self._discovery_timer.timeout.connect(self.refresh_cameras)
        self._discovery_timer.start()
        self.refresh_cameras()
        self.refresh_history()

    def _build_live_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(14, 12, 14, 14)
        page_layout.setSpacing(10)

        controls = QHBoxLayout()
        self.camera_combo = QComboBox()
        self.camera_combo.setMinimumWidth(300)
        self.camera_combo.currentIndexChanged.connect(self._camera_changed)
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(245)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_cameras)
        self.start_button = QPushButton("Start preview")
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_capture)

        controls.addWidget(QLabel("Camera"))
        controls.addWidget(self.camera_combo, 3)
        controls.addWidget(self.refresh_button)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Capture mode"))
        controls.addWidget(self.mode_combo, 2)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        page_layout.addLayout(controls)

        self.top_status = QLabel("Discovering V4L2 cameras…")
        self.top_status.setWordWrap(True)
        self.top_status.setStyleSheet("color: palette(mid); padding: 2px 1px;")
        page_layout.addWidget(self.top_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 8, 0)
        preview_header = QHBoxLayout()
        preview_title = QLabel("Live camera feed")
        preview_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.preview_resolution = QLabel("No frame yet")
        self.preview_resolution.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        preview_header.addWidget(self.preview_resolution)
        preview_layout.addLayout(preview_header)

        self.preview = QLabel("Select a camera and start preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(480, 320)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet(
            "QLabel { background: #15181c; color: #d5dbe2; border: 1px solid #353a41; }"
        )
        preview_layout.addWidget(self.preview, 1)
        note = QLabel("Capture and preview run separately. The preview drops stale display frames when rendering falls behind.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        preview_layout.addWidget(note)
        splitter.addWidget(preview_panel)

        diagnostics = QWidget()
        diagnostics.setMinimumWidth(310)
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_layout.setContentsMargins(8, 0, 0, 0)
        diagnostics_layout.setSpacing(10)

        self.capture_state = QLabel("Idle")
        self.capture_state.setStyleSheet("font-size: 15px; font-weight: 600;")
        diagnostics_layout.addWidget(self.capture_state)

        metrics = QGridLayout()
        metrics.setSpacing(8)
        self.capture_fps_card = MetricCard(
            "Capture FPS",
            "Frames returned by OpenCV per second. Capture can continue faster than the preview updates.",
            "0.0",
        )
        self.preview_fps_card = MetricCard(
            "Preview FPS",
            "Frames displayed by the GUI per second. This is measured separately from capture FPS.",
            "0.0",
        )
        self.interval_card = MetricCard(
            "Frame Interval (ms)",
            "Time between successful host-side frame reads, in milliseconds. This is not sensor-to-display latency.",
            "—",
        )
        self.failures_card = MetricCard(
            "Read Failures",
            "Number of unsuccessful frame reads during this capture run.",
            "0",
        )
        metrics.addWidget(self.capture_fps_card, 0, 0)
        metrics.addWidget(self.preview_fps_card, 0, 1)
        metrics.addWidget(self.interval_card, 1, 0)
        metrics.addWidget(self.failures_card, 1, 1)
        diagnostics_layout.addLayout(metrics)

        settings_box = QGroupBox("Camera settings")
        settings = QFormLayout(settings_box)
        self.requested_settings = QLabel("—")
        self.reported_settings = QLabel("—")
        self.observed_settings = QLabel("—")
        self.camera_identity = QLabel("—")
        for label in (self.requested_settings, self.reported_settings, self.observed_settings, self.camera_identity):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        settings.addRow("Requested", self.requested_settings)
        settings.addRow("Driver readback", self.reported_settings)
        settings.addRow("Frame output", self.observed_settings)
        settings.addRow("Device identity", self.camera_identity)
        diagnostics_layout.addWidget(settings_box)

        recording_box = QGroupBox("Experiment recording")
        recording_layout = QVBoxLayout(recording_box)
        self.experiment_name = QLineEdit()
        self.experiment_name.setPlaceholderText("Experiment name (required)")
        self.experiment_name.setMaxLength(120)
        self.experiment_notes = QPlainTextEdit()
        self.experiment_notes.setPlaceholderText("Notes: lighting, lens position, or test conditions")
        self.experiment_notes.setMaximumHeight(68)
        self.experiment_notes.setTabChangesFocus(True)
        self.duration_combo = QComboBox()
        for seconds in (10, 30, 60, 120):
            self.duration_combo.addItem(f"{seconds} seconds", seconds)
        self.duration_combo.addItem("Manual stop", None)
        record_controls = QHBoxLayout()
        self.record_start_button = QPushButton("Start recording")
        self.record_start_button.setToolTip("Record timing samples without stopping the live preview.")
        self.record_start_button.clicked.connect(self.start_recording)
        self.record_stop_button = QPushButton("Stop recording")
        self.record_stop_button.setEnabled(False)
        self.record_stop_button.setToolTip("Stop early and save the experiment as interrupted.")
        self.record_stop_button.clicked.connect(self.stop_recording)
        record_controls.addWidget(self.record_start_button)
        record_controls.addWidget(self.record_stop_button)
        self.recording_status = QLabel("Preview must be running before recording can start.")
        self.recording_status.setWordWrap(True)
        self.recording_progress = QProgressBar()
        self.recording_progress.setRange(0, 1000)
        self.recording_progress.setValue(0)
        self.recording_progress.setFormat("Not recording")
        recording_layout.addWidget(self.experiment_name)
        recording_layout.addWidget(self.experiment_notes)
        recording_layout.addWidget(self.duration_combo)
        recording_layout.addLayout(record_controls)
        recording_layout.addWidget(self.recording_status)
        recording_layout.addWidget(self.recording_progress)
        diagnostics_layout.addWidget(recording_box)
        self.experiment_name.textChanged.connect(self._update_buttons)

        self.capture_message = QLabel("Capture FPS counts frames returned by OpenCV. Frame interval is host observed; neither is sensor latency.")
        self.capture_message.setWordWrap(True)
        self.capture_message.setStyleSheet("color: palette(mid); font-size: 11px;")
        diagnostics_layout.addWidget(self.capture_message)
        diagnostics_layout.addStretch(1)
        splitter.addWidget(diagnostics)
        splitter.setSizes([920, 440])
        page_layout.addWidget(splitter, 1)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 14)
        header = QHBoxLayout()
        title = QLabel("Recorded experiments and imported history")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.history_refresh_button = QPushButton("Refresh")
        self.history_refresh_button.clicked.connect(self.refresh_history)
        self.analyze_history_button = QPushButton("Analyze selected")
        self.analyze_history_button.setToolTip("Open the selected history run in the Analysis workspace.")
        self.analyze_history_button.clicked.connect(self._analyze_selected_history)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.analyze_history_button)
        header.addWidget(self.history_refresh_button)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ("Experiment / run", "Camera", "Source", "Status", "Started", "Capture FPS", "Preview FPS")
        )
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 7):
            self.history_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.currentCellChanged.connect(self._history_selection_changed)
        splitter.addWidget(self.history_table)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(4, 6, 4, 0)
        detail_heading = QLabel("Experiment details")
        detail_heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.history_details = QPlainTextEdit()
        self.history_details.setReadOnly(True)
        self.history_details.setPlaceholderText("Select a history row to inspect its settings and measurements.")
        detail_layout.addWidget(detail_heading)
        detail_layout.addWidget(self.history_details, 1)
        splitter.addWidget(detail_panel)
        splitter.setSizes([400, 260])
        layout.addWidget(splitter, 1)
        return page

    @staticmethod
    def _build_placeholder_tab(title: str, message: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        body = QLabel(message)
        body.setWordWrap(True)
        body.setStyleSheet("color: palette(mid); font-size: 14px;")
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch(1)
        return page

    def _tab_changed(self, index: int) -> None:
        if self.tabs.tabText(index) == "History":
            self.refresh_history()
        elif self.tabs.tabText(index) == "Analysis":
            self.analysis_workspace.refresh_experiments()

    def _analyze_selected_history(self) -> None:
        row = self.history_table.currentRow()
        first_cell = self.history_table.item(row, 0) if row >= 0 else None
        selection = first_cell.data(Qt.ItemDataRole.UserRole) if first_cell else None
        if not selection:
            self.history_details.setPlainText("Select a history row before opening Analysis.")
            return
        session_id, benchmark_id = selection
        self.analysis_workspace.select_experiment(session_id, benchmark_id)
        self.tabs.setCurrentWidget(self.analysis_workspace)

    def refresh_history(self) -> None:
        if not hasattr(self, "history_table"):
            return
        try:
            entries = self._database.history_entries()
        except Exception as exc:
            logger.exception("Could not load experiment history")
            self.history_details.setPlainText(f"History could not be read: {exc}")
            return

        self.history_table.setRowCount(0)
        for row_index, entry in enumerate(entries):
            self.history_table.insertRow(row_index)
            source = str(entry.get("source") or "unknown")
            run_label = str(entry.get("session_label") or "Unnamed session")
            benchmark_label = entry.get("benchmark_label")
            if benchmark_label and source == "imported":
                run_label = f"{run_label} · {benchmark_label}"
            elif benchmark_label and run_label != benchmark_label:
                run_label = f"{run_label} · {benchmark_label}"
            camera_name = entry.get("model") or entry.get("stable_identifier") or "Identity unavailable"
            source_label = "Imported CSV" if source == "imported" else "Live experiment" if source == "live_experiment" else source
            status = entry.get("status") or ("imported" if source == "imported" else "session")
            started = entry.get("benchmark_started_at") or entry.get("session_started_at") or "—"
            values = (
                run_label,
                str(camera_name),
                source_label,
                str(status),
                str(started),
                self._format_optional_fps(entry.get("capture_fps")),
                self._format_optional_fps(entry.get("preview_fps")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (entry.get("session_id"), entry.get("benchmark_id")))
                self.history_table.setItem(row_index, column, item)

        if entries:
            self.history_table.selectRow(0)
        else:
            self.history_details.setPlainText(
                "No sessions are stored in this database yet. New recordings and imported CSV history will appear here."
            )

    @staticmethod
    def _format_optional_fps(value: object) -> str:
        return f"{float(value):.1f}" if value is not None else "—"

    def _history_selection_changed(self, current_row: int, _current_column: int,
                                   _previous_row: int, _previous_column: int) -> None:
        if current_row < 0:
            return
        first_cell = self.history_table.item(current_row, 0)
        if first_cell is None:
            return
        selection = first_cell.data(Qt.ItemDataRole.UserRole)
        if not selection:
            return
        session_id, benchmark_id = selection
        try:
            detail = self._database.history_detail(benchmark_id, session_id)
        except Exception as exc:
            logger.exception("Could not load history details")
            self.history_details.setPlainText(f"The selected history row could not be read: {exc}")
            return

        entry = detail["entry"]
        lines = [
            str(entry.get("label") or "Unnamed session"),
            f"Source: {entry.get('source') or 'unknown'}",
            f"Camera: {entry.get('model') or entry.get('stable_identifier') or 'identity unavailable'}",
        ]
        if entry.get("manufacturer"):
            lines.append(f"Manufacturer: {entry['manufacturer']}")
        lines.extend((
            f"Stable identity: {entry.get('stable_identifier') or 'not recorded'}",
            f"Started: {entry.get('benchmark_started_at') or entry.get('started_at') or 'unknown'}",
            f"Ended: {entry.get('benchmark_ended_at') or entry.get('ended_at') or 'unknown'}",
        ))
        if entry.get("status"):
            lines.append(f"Status: {entry['status']}")
        if entry.get("notes"):
            lines.extend(("", "Notes:", str(entry["notes"])))
        if entry.get("source_basename"):
            lines.extend((
                "",
                f"Imported source file: {entry['source_basename']}",
                f"Original rows preserved: {entry.get('imported_row_count') or 0}",
                f"Normalized measurements: {entry.get('imported_measurement_count') or 0}",
            ))
            if entry.get("raw_record_count") is not None:
                lines.append(f"Raw records: {entry['raw_record_count']}")

        if entry.get("duration_seconds") is not None:
            lines.extend((
                "",
                "Summary:",
                f"Duration: {float(entry['duration_seconds']):.3f} s",
                f"Capture FPS: {self._format_optional_fps(entry.get('capture_fps'))}",
                f"Preview FPS: {self._format_optional_fps(entry.get('preview_fps'))}",
                f"Processing FPS: {self._format_optional_fps(entry.get('processing_fps'))}",
                f"Frames: {entry.get('frame_count') or 0}",
                f"Read failures: {entry.get('read_failures') or 0}",
                f"Frame intervals: {entry.get('interval_count') or 0}",
                f"Median interval: {self._format_optional_ms(entry.get('median_interval_ms'))}",
                f"p95 interval: {self._format_optional_ms(entry.get('p95_interval_ms'))}",
                f"p99 interval: {self._format_optional_ms(entry.get('p99_interval_ms'))}",
                "Frame timing uses host timestamps after OpenCV read returns; it is not sensor-to-display latency.",
            ))

        settings = detail["settings"]
        if settings:
            lines.extend(("", "Requested and driver-reported settings:"))
            for setting in settings:
                requested = setting.get("requested_value_json")
                reported = setting.get("reported_value_json")
                try:
                    requested = json.loads(requested) if requested is not None else "not requested"
                    reported = json.loads(reported) if reported is not None else "not reported"
                except (TypeError, json.JSONDecodeError):
                    pass
                lines.append(
                    f"  {setting['setting_key']}: requested {requested}; reported {reported} "
                    f"({setting.get('verification_status') or 'unknown'})"
                )

        configuration_json = entry.get("configuration_json")
        if configuration_json:
            lines.extend(("", "Capture configuration:"))
            try:
                lines.append(json.dumps(json.loads(configuration_json), ensure_ascii=False, indent=2, sort_keys=True))
            except (TypeError, json.JSONDecodeError):
                lines.append(str(configuration_json))

        summary_json = entry.get("summary_json")
        if summary_json:
            try:
                summary_data = json.loads(summary_json)
                reason = summary_data.get("termination_reason")
                if reason:
                    lines.append(f"Termination reason: {reason}")
            except (TypeError, json.JSONDecodeError):
                pass

        if detail["metrics"]:
            lines.extend(("", "Stored measurement streams:"))
            for metric in detail["metrics"]:
                lines.append(
                    f"  {metric['metric']} [{metric['stream_scope']}]: {metric['sample_count']} samples, "
                    f"avg {float(metric['average']):.4g} {metric['unit']}, "
                    f"range {float(metric['minimum']):.4g}–{float(metric['maximum']):.4g}"
                )
        self.history_details.setPlainText("\n".join(lines))

    @staticmethod
    def _format_optional_ms(value: object) -> str:
        return f"{float(value):.3f} ms" if value is not None else "not available"

    def refresh_cameras(self) -> None:
        prior_candidate = _candidate_for_combo(self.camera_combo)
        prior_camera_id = prior_candidate.identity.stable_identifier if prior_candidate else None
        try:
            candidates = self._discovery.discover()
        except Exception as exc:
            logger.exception("V4L2 discovery failed")
            self.top_status.setText(f"Camera discovery failed: {exc}")
            return

        self._candidates = candidates
        existing_ids = {candidate.identity.stable_identifier for candidate in candidates}
        if (self._active_stable_id and self._active_stable_id not in existing_ids
                and self._worker and self._worker.is_alive()):
            self._finish_recording("camera_disconnected")
            self._worker.request_stop()
            self.top_status.setText("The active camera disappeared from V4L2. Capture is stopping; reconnect it and refresh.")

        blocker = QSignalBlocker(self.camera_combo)
        self.camera_combo.clear()
        selected_index = -1
        for candidate in candidates:
            path = candidate.identity.device_path or "unknown node"
            interface = candidate.identity.metadata.get("video_index")
            endpoint = f"{path}" + (f" · interface {interface}" if interface is not None else "")
            suffix = "" if candidate.node_present else " · node unavailable"
            self.camera_combo.addItem(f"{candidate.display_name} · {endpoint}{suffix}", candidate)
            index = self.camera_combo.count() - 1
            if not candidate.node_present:
                item = self.camera_combo.model().item(index)
                if item is not None:
                    item.setEnabled(False)
            if candidate.identity.stable_identifier == self._selected_stable_id:
                selected_index = index

        if self._selected_stable_id and selected_index < 0:
            self.camera_combo.addItem(f"{self._selected_stable_id} · disconnected", None)
            selected_index = self.camera_combo.count() - 1
        self.camera_combo.setCurrentIndex(selected_index)
        del blocker

        if not candidates:
            self.top_status.setText("No V4L2 cameras found. Connect a camera, then refresh.")
        elif not any(candidate.node_present for candidate in candidates):
            self.top_status.setText("V4L2 cameras are listed, but their /dev nodes are unavailable to this process.")
        elif (
            (not self._worker or self._worker.snapshot().state not in (CaptureState.FAILED, CaptureState.UNAVAILABLE))
            and not self.top_status.text().startswith("The active camera disappeared")
        ):
            self.top_status.setText(f"Found {sum(candidate.node_present for candidate in candidates)} available V4L2 endpoint(s).")

        if self._active_stable_id and self._active_stable_id not in existing_ids and self._worker:
            self.capture_state.setText("Camera unavailable")
        selected_candidate = _candidate_for_combo(self.camera_combo)
        selected_camera_id = selected_candidate.identity.stable_identifier if selected_candidate else None
        if selected_camera_id != prior_camera_id:
            self._camera_changed(self.camera_combo.currentIndex())
        self._update_buttons()

    def _camera_changed(self, _index: int) -> None:
        candidate = _candidate_for_combo(self.camera_combo)
        self._selected_stable_id = candidate.identity.stable_identifier if candidate else self._selected_stable_id
        self.camera_identity.setText(self._selected_stable_id or "—")
        modes = modes_for_camera(candidate)
        blocker = QSignalBlocker(self.mode_combo)
        self.mode_combo.clear()
        for mode in modes:
            self.mode_combo.addItem(mode.label, mode)
        self.mode_combo.setCurrentIndex(0 if modes else -1)
        del blocker
        self._mode_changed(self.mode_combo.currentIndex())
        self._update_buttons()

    def _mode_changed(self, _index: int) -> None:
        mode = _mode_for_combo(self.mode_combo)
        self.requested_settings.setText(mode.label if mode else "—")

    def start_capture(self) -> None:
        candidate = _candidate_for_combo(self.camera_combo)
        mode = _mode_for_combo(self.mode_combo)
        if candidate is None or not candidate.node_present or mode is None:
            self.top_status.setText("Select an available camera and capture mode first.")
            return
        if self._worker and self._worker.is_alive():
            return

        device = preferred_device_path(candidate)
        if not device:
            self.top_status.setText("This camera has no usable V4L2 path.")
            return
        self._last_frame_version = 0
        self._preview_rate.reset()
        self._preview_fps = 0.0
        self._last_image = None
        self.preview.clear()
        self.preview.setText("Opening camera…")
        self.preview_resolution.setText("Waiting for the first frame")
        self.capture_fps_card.value.setText("0.0")
        self.preview_fps_card.value.setText("0.0")
        self.interval_card.value.setText("—")
        self.failures_card.value.setText("0")
        self.reported_settings.setText("Waiting for driver readback…")
        self.observed_settings.setText("Waiting for first frame…")
        self.camera_identity.setText(candidate.identity.stable_identifier)
        self._active_stable_id = candidate.identity.stable_identifier
        worker_options = {"capture_factory": self._capture_factory} if self._capture_factory is not None else {}
        self._worker = OpenCVCaptureWorker(device, mode, **worker_options)
        self._worker.start()
        self.top_status.setText(f"Opening {candidate.display_name} using {device}.")
        self._update_buttons()

    def stop_capture(self) -> None:
        self._finish_recording("capture_stopped")
        if self._worker and self._worker.is_alive():
            self._worker.request_stop()
            self.top_status.setText("Stopping capture after the active camera read returns…")
        self._update_buttons()

    def start_recording(self) -> None:
        candidate = _candidate_for_combo(self.camera_combo)
        worker = self._worker
        mode = _mode_for_combo(self.mode_combo)
        if candidate is None or mode is None or worker is None:
            self.recording_status.setText("Select a camera and start its preview before recording.")
            return
        snapshot = worker.snapshot()
        if snapshot.state != CaptureState.RUNNING or snapshot.negotiated is None:
            self.recording_status.setText("Wait for camera readback and a running preview before recording.")
            return
        if not self.experiment_name.text().strip():
            self.recording_status.setText("Enter a short experiment name first.")
            return
        try:
            duration = self.duration_combo.currentData(Qt.ItemDataRole.UserRole)
            self._recorder = ExperimentRecorder(
                self._database,
                candidate.identity,
                mode,
                snapshot.negotiated,
                name=self.experiment_name.text(),
                notes=self.experiment_notes.toPlainText(),
                duration_seconds=duration,
            )
            worker.set_timing_listener(self._recorder.record_capture_event)
            self._last_recording_snapshot = None
            self._last_recorded_preview_ns = time.monotonic_ns()
            self.recording_status.setText("Recording started; live preview continues independently.")
            self.top_status.setText("Experiment recording started. Capture and preview continue on separate loops.")
        except Exception as exc:
            logger.exception("Could not start experiment recording")
            self._recorder = None
            self.recording_status.setText(f"Could not start recording: {exc}")
        self._update_buttons()

    def stop_recording(self) -> None:
        self._finish_recording("manual_stop")

    def _finish_recording(self, reason: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        snapshot = recorder.snapshot()
        if snapshot.state == RecordingState.RECORDING:
            snapshot = recorder.stop(reason)
        if snapshot.state == RecordingState.RECORDING:
            self.recording_status.setText("Recorder is still flushing queued timing samples; please wait.")
            self.record_stop_button.setEnabled(True)
            return
        if self._worker is not None:
            self._worker.set_timing_listener(None)
        self._last_recording_snapshot = snapshot
        self._recorder = None
        self._render_recording_snapshot(snapshot)
        self.refresh_history()
        self._update_buttons()

    def _render_recording_snapshot(self, snapshot: RecordingSnapshot) -> None:
        if snapshot.state == RecordingState.RECORDING:
            duration = snapshot.requested_duration_seconds
            elapsed = snapshot.elapsed_seconds
            if duration is None:
                self.recording_progress.setRange(0, 0)
                self.recording_progress.setFormat(f"Recording — {elapsed:.1f} s")
            else:
                self.recording_progress.setRange(0, duration * 1000)
                self.recording_progress.setValue(min(duration * 1000, int(elapsed * 1000)))
                self.recording_progress.setFormat(f"Recording — {elapsed:.1f} / {duration} s")
            self.recording_status.setText(
                f"Recording · {snapshot.frame_count} frames · {snapshot.read_failures} read failures"
            )
            return

        elapsed = snapshot.elapsed_seconds
        if snapshot.state == RecordingState.COMPLETED:
            self.recording_progress.setRange(0, 1000)
            self.recording_progress.setValue(1000)
            self.recording_progress.setFormat("Completed")
            text = f"Completed after {elapsed:.1f} s · {snapshot.frame_count} frames · {snapshot.read_failures} read failures"
        elif snapshot.state == RecordingState.INTERRUPTED:
            self.recording_progress.setRange(0, 1000)
            denominator = snapshot.requested_duration_seconds or max(elapsed, 1.0)
            self.recording_progress.setValue(min(1000, int(1000 * elapsed / denominator)))
            self.recording_progress.setFormat("Interrupted")
            text = f"Interrupted after {elapsed:.1f} s ({snapshot.reason}) · {snapshot.frame_count} frames"
        else:
            self.recording_progress.setRange(0, 1000)
            self.recording_progress.setValue(0)
            self.recording_progress.setFormat("Recording failed")
            text = f"Recording failed: {snapshot.reason}"
        self.recording_status.setText(text)

    def _poll_recording(self, capture_state: CaptureState) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        if capture_state in (CaptureState.FAILED, CaptureState.UNAVAILABLE, CaptureState.STOPPED):
            reason = "camera_disconnected" if capture_state == CaptureState.UNAVAILABLE else "capture_ended"
            self._finish_recording(reason)
            return
        snapshot = recorder.snapshot()
        if snapshot.state == RecordingState.RECORDING:
            self._render_recording_snapshot(snapshot)
        else:
            self._finish_recording(snapshot.reason or snapshot.state.value)

    def _poll_capture(self) -> None:
        worker = self._worker
        if worker is None:
            self._update_buttons()
            return
        snapshot = worker.snapshot()
        self.capture_state.setText(snapshot.state.value.replace("_", " ").title())
        self.capture_fps_card.value.setText(f"{snapshot.capture_fps:.1f}")
        self.interval_card.value.setText(
            f"{snapshot.frame_interval_ms:.2f}" if snapshot.frame_interval_ms is not None else "—"
        )
        self.failures_card.value.setText(str(snapshot.read_failures))
        if snapshot.negotiated is not None:
            self.reported_settings.setText(self._format_negotiated(snapshot))
        if snapshot.observed_frame_size:
            width, height = snapshot.observed_frame_size
            self.observed_settings.setText(f"{width}×{height} decoded frame")

        latest = worker.frame_buffer.read_after(self._last_frame_version)
        if latest is not None:
            frame, self._last_frame_version, timestamp_ns = latest
            try:
                self._last_image = _frame_to_image(frame)
                self._paint_preview()
                self.preview_resolution.setText(f"{self._last_image.width()}×{self._last_image.height()}")
                preview_ns = time.monotonic_ns()
                self._preview_fps = self._preview_rate.add(preview_ns)
            except Exception as exc:
                logger.exception("Could not render camera frame")
                self.preview.setText(f"Preview error: {exc}")

        self._preview_fps = self._preview_rate.value_at(time.monotonic_ns())
        self.preview_fps_card.value.setText(f"{self._preview_fps:.1f}")

        if self._recorder is not None:
            now_ns = time.monotonic_ns()
            if now_ns - self._last_recorded_preview_ns >= 1_000_000_000:
                self._recorder.record_preview_fps(self._preview_fps, monotonic_ns=now_ns, observed_at=utc_now_iso())
                self._last_recorded_preview_ns = now_ns

        if snapshot.state == CaptureState.RUNNING:
            self.top_status.setText(snapshot.message)
        elif snapshot.state == CaptureState.FAILED:
            self.top_status.setText(snapshot.message)
            if "OpenCV is not installed" in snapshot.message:
                self.top_status.setText(
                    "OpenCV is not installed in this Python environment. Install the desktop extra in the per-user venv."
                )
        elif snapshot.state == CaptureState.UNAVAILABLE:
            self.top_status.setText(snapshot.message)
        elif snapshot.state == CaptureState.STOPPED:
            self.top_status.setText("Capture stopped. Reconnect or select a camera to start again.")
        self._poll_recording(snapshot.state)
        self._update_buttons()

    def _format_negotiated(self, snapshot: Any) -> str:
        settings = snapshot.negotiated
        if settings is None:
            return "—"
        parts = []
        if settings.width is not None and settings.height is not None:
            parts.append(f"{settings.width}×{settings.height}")
        else:
            parts.append("size not reported")
        parts.append(settings.pixel_format or "format not reported")
        parts.append(f"{settings.fps:g} FPS" if settings.fps is not None else "FPS not reported")

        requested = snapshot.requested
        comparisons: list[bool] = []
        missing_readback = False
        for field_name, asked, reported in (
            ("width", requested.width, settings.width),
            ("height", requested.height, settings.height),
            ("fps", requested.fps, settings.fps),
            ("pixel_format", requested.pixel_format, settings.pixel_format),
        ):
            if asked is None:
                continue
            if reported is None:
                missing_readback = True
            elif field_name == "fps":
                comparisons.append(abs(reported - asked) <= max(1.0, asked * 0.02))
            elif field_name in ("width", "height"):
                comparisons.append(reported == asked)
            else:
                comparisons.append(reported.casefold() == asked.casefold())
        if any(not matches for matches in comparisons):
            state = "driver reported a different mode"
        elif missing_readback:
            state = "readback incomplete"
        else:
            state = "reported values match request"
        if (
            snapshot.observed_frame_size is not None
            and snapshot.observed_frame_size != (requested.width, requested.height)
        ):
            state = "decoded frame size differs from request"
        return f"{', '.join(parts)} · {state}"

    def _paint_preview(self) -> None:
        if self._last_image is None or self.preview.width() < 2 or self.preview.height() < 2:
            return
        pixmap = QPixmap.fromImage(self._last_image).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(pixmap)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._paint_preview()

    def _update_buttons(self) -> None:
        candidate = _candidate_for_combo(self.camera_combo)
        worker_alive = bool(self._worker and self._worker.is_alive())
        self.start_button.setEnabled(bool(candidate and candidate.node_present and _mode_for_combo(self.mode_combo)) and not worker_alive)
        self.stop_button.setEnabled(worker_alive and self._worker.snapshot().state not in (CaptureState.STOPPING,))
        self.camera_combo.setEnabled(not worker_alive)
        self.mode_combo.setEnabled(not worker_alive)
        recording_active = bool(
            self._recorder and self._recorder.snapshot().state == RecordingState.RECORDING
        )
        capture_ready = bool(
            worker_alive and self._worker and self._worker.snapshot().state == CaptureState.RUNNING
        )
        self.record_start_button.setEnabled(capture_ready and not recording_active and bool(self.experiment_name.text().strip()))
        self.record_stop_button.setEnabled(recording_active)
        self.experiment_name.setEnabled(not recording_active)
        self.experiment_notes.setEnabled(not recording_active)
        self.duration_combo.setEnabled(not recording_active)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override name
        self._poll_timer.stop()
        self._discovery_timer.stop()
        self._finish_recording("application_shutdown")
        if self._worker and self._worker.is_alive():
            self._worker.request_stop()
            self._worker.join(timeout=0.75)
        event.accept()


def run(database: Database | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    window = LiveWorkbenchWindow(database=database)
    window.show()
    return app.exec()
