"""Two independently managed V4L2 live previews for CEL-013 Pass 1."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from typing import Any, Callable, Iterable

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..capture import (
    CaptureSnapshot,
    CaptureState,
    OpenCVCaptureWorker,
    RollingFrameRate,
    preferred_device_path,
)
from ..capture_modes import CaptureMode, enumerate_modes_for_device
from ..models import CameraCandidate
from .preview import frame_to_image

logger = logging.getLogger(__name__)


def capture_endpoint_key(candidate: CameraCandidate) -> str:
    """Canonicalize a V4L2 endpoint so symlink aliases cannot be selected twice."""
    path = preferred_device_path(candidate) or candidate.identity.device_path
    if path:
        return os.path.normcase(os.path.realpath(path))
    return candidate.identity.stable_identifier


def same_capture_endpoint(first: CameraCandidate | None, second: CameraCandidate | None) -> bool:
    return bool(first and second and capture_endpoint_key(first) == capture_endpoint_key(second))


def _camera_priority(candidate: CameraCandidate) -> tuple[int, str, str]:
    pair = (candidate.identity.vendor_id, candidate.identity.product_id)
    rank = {("1415", "2000"): 0, ("07ca", "313a"): 1}.get(pair, 2)
    return rank, candidate.display_name.casefold(), candidate.identity.stable_identifier


def _preferred_mode(candidate: CameraCandidate, modes: tuple[CaptureMode, ...]) -> int:
    """Select a familiar mode only when this device actually advertised it."""
    pair = (candidate.identity.vendor_id, candidate.identity.product_id)
    targets = {
        ("1415", "2000"): (320, 240, 187, "GRBG"),
        ("07ca", "313a"): (1280, 720, 30, "MJPG"),
    }
    target = targets.get(pair)
    if target is not None:
        for index, mode in enumerate(modes):
            if (mode.width, mode.height, mode.fps, mode.pixel_format) == target:
                return index
    return 0


class CameraSlotPanel(QGroupBox):
    """One camera's controls, worker reference, bounded preview and diagnostics."""

    def __init__(
        self,
        slot_name: str,
        owner: MultiCameraWorkspace,
        capture_factory: Any = None,
    ) -> None:
        super().__init__(slot_name)
        self.slot_name = slot_name
        self.owner = owner
        self.capture_factory = capture_factory
        self._worker: OpenCVCaptureWorker | None = None
        self._selected_id: str | None = None
        self._active_id: str | None = None
        self._preview_version = 0
        self._preview_rate = RollingFrameRate()
        self._preview_fps = 0.0
        self._last_image: QImage | None = None
        self._disconnect_notice = False
        self._disconnect_message: str | None = None
        self._modes_need_refresh = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        selection = QFormLayout()
        self.camera_combo = QComboBox()
        self.camera_combo.setMinimumContentsLength(18)
        self.camera_combo.currentIndexChanged.connect(self._selection_changed)
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumContentsLength(16)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        selection.addRow("Camera", self.camera_combo)
        selection.addRow("Reported capture mode", self.mode_combo)
        layout.addLayout(selection)

        buttons = QHBoxLayout()
        self.start_button = QPushButton(f"Start {slot_name[-1]}")
        self.start_button.clicked.connect(lambda _checked=False: self.start_capture())
        self.stop_button = QPushButton(f"Stop {slot_name[-1]}")
        self.stop_button.clicked.connect(self.stop_capture)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.state_label = QLabel("Stopped")
        self.state_label.setStyleSheet("color: palette(text); font-weight: 600;")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.preview = QLabel("Select a camera and an enumerated mode, then start capture.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(150)
        self.preview.setMinimumWidth(180)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet(
            "QLabel { background: #15181c; color: #f0f3f6; border: 1px solid #535a63; }"
        )
        layout.addWidget(self.preview, 1)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(3)
        self.metric_values: dict[str, QLabel] = {}
        for index, title in enumerate(
            ("Capture FPS", "Preview FPS", "Frames", "Read failures", "Elapsed capture")
        ):
            label = QLabel(title)
            label.setStyleSheet("color: palette(text);")
            label.setToolTip(self._metric_tooltip(title))
            value = QLabel("0.0" if "FPS" in title else "0")
            value.setStyleSheet("color: palette(text); font-weight: 600;")
            value.setToolTip(label.toolTip())
            self.metric_values[title] = value
            metrics.addWidget(label, index // 2, (index % 2) * 2)
            metrics.addWidget(value, index // 2, (index % 2) * 2 + 1)
        layout.addLayout(metrics)

        self.requested_label = QLabel("Requested: —")
        self.reported_label = QLabel("Driver readback: —")
        self.requested_label.setStyleSheet("color: palette(text);")
        self.reported_label.setStyleSheet("color: palette(text);")
        for label in (self.requested_label, self.reported_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.requested_label)
        layout.addWidget(self.reported_label)

        self._refresh_mode_label()

    @staticmethod
    def _metric_tooltip(title: str) -> str:
        explanations = {
            "Capture FPS": "Successful frames returned by this camera's OpenCV read loop per second.",
            "Preview FPS": "New frames converted and presented by this GUI pane per second; capture can run faster.",
            "Frames": "Successful frames returned in this capture run.",
            "Read failures": "Unsuccessful frame reads in this capture run; this does not prove the driver dropped a frame.",
            "Elapsed capture": "Host monotonic elapsed time since this stream's driver configuration was ready.",
        }
        return explanations[title]

    @property
    def worker(self) -> OpenCVCaptureWorker | None:
        return self._worker

    @property
    def candidate(self) -> CameraCandidate | None:
        value = self.camera_combo.currentData(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, CameraCandidate) else None

    @property
    def mode(self) -> CaptureMode | None:
        value = self.mode_combo.currentData(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, CaptureMode) else None

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def set_candidates(
        self,
        candidates: Iterable[CameraCandidate],
        preferred_id: str | None = None,
    ) -> None:
        candidates = list(candidates)
        old_candidate = self.candidate
        old_id = old_candidate.identity.stable_identifier if old_candidate else self._selected_id
        old_path = old_candidate.identity.device_path if old_candidate else None
        old_open_path = preferred_device_path(old_candidate) if old_candidate else None
        by_id = {candidate.identity.stable_identifier: candidate for candidate in candidates}
        selected_id = old_id if old_id in by_id else preferred_id
        if old_candidate is not None and old_id is not None:
            current_candidate = by_id.get(old_id)
            disappeared = current_candidate is None or not current_candidate.node_present
            if disappeared:
                active = bool(self._worker and self._worker.is_alive() and self._active_id == old_id)
                if active and not self._disconnect_notice:
                    self._worker.request_stop()
                    self._disconnect_notice = True
                    self._disconnect_message = (
                        "Device disappeared; reconnect, refresh, and press Start again."
                    )
                    self.state_label.setText(
                        "Device disappeared; stopping this stream. Reconnect, refresh, then start again."
                    )
                unavailable = replace(current_candidate or old_candidate, node_present=False)
                if current_candidate is None:
                    candidates.append(unavailable)
                else:
                    candidates = [
                        unavailable if item.identity.stable_identifier == old_id else item
                        for item in candidates
                    ]
                by_id[old_id] = unavailable
                selected_id = old_id

        blocker = QSignalBlocker(self.camera_combo)
        self.camera_combo.clear()
        selected_index = -1
        for candidate in candidates:
            endpoint = candidate.identity.device_path or candidate.identity.stable_identifier
            suffix = " · unavailable" if not candidate.node_present else ""
            self.camera_combo.addItem(f"{candidate.display_name} · {endpoint}{suffix}", candidate)
            index = self.camera_combo.count() - 1
            if not candidate.node_present:
                item = self.camera_combo.model().item(index)
                if item is not None:
                    item.setEnabled(False)
            if candidate.identity.stable_identifier == selected_id:
                selected_index = index
        if selected_index < 0:
            self.camera_combo.addItem("No camera selected", None)
            selected_index = self.camera_combo.count() - 1
        self.camera_combo.setCurrentIndex(selected_index)
        del blocker

        current = self.candidate
        current_id = current.identity.stable_identifier if current else None
        path_changed = bool(current and old_path and current.identity.device_path != old_path)
        open_path_changed = bool(
            current and old_open_path
            and preferred_device_path(current) != old_open_path
        )
        self._selected_id = current_id
        if open_path_changed and self.busy and self._active_id == old_id:
            self._disconnect_notice = True
            self._disconnect_message = (
                "Device node changed; the previous stream stopped. Press Start to use the current endpoint."
            )
            if self._worker:
                self._worker.request_stop()
            self.state_label.setText(
                "Device node changed; stopping the old endpoint. Press Start again after this stream stops."
            )
        modes_changed = current_id != old_id or path_changed or self.mode_combo.count() == 0
        if modes_changed:
            if self.busy:
                self._modes_need_refresh = True
            else:
                self._modes_need_refresh = False
                self.load_modes()
        elif current is not None and current.node_present and self._disconnect_notice and not self.busy:
            self._disconnect_message = "Device is available again; press Start to resume."
            self.state_label.setText(self._disconnect_message)
        self.update_metrics()
        self.update_controls()

    def select_stable_id(self, stable_id: str | None) -> None:
        for index in range(self.camera_combo.count()):
            candidate = self.camera_combo.itemData(index, Qt.ItemDataRole.UserRole)
            candidate_id = candidate.identity.stable_identifier if isinstance(candidate, CameraCandidate) else None
            if candidate_id == stable_id:
                blocker = QSignalBlocker(self.camera_combo)
                self.camera_combo.setCurrentIndex(index)
                del blocker
                self._selected_id = candidate_id
                return

    def _selection_changed(self, _index: int) -> None:
        candidate = self.candidate
        candidate_id = candidate.identity.stable_identifier if candidate else None
        previous_id = self._selected_id
        if candidate is not None and not self.owner.selection_is_allowed(self, candidate):
            self.select_stable_id(previous_id)
            self.state_label.setText(
                "Selection rejected: this physical capture endpoint is already selected in the other slot."
            )
            return
        self._selected_id = candidate_id
        self._disconnect_notice = False
        self._disconnect_message = None
        self.load_modes()
        self.owner.slot_selection_changed(self)

    def load_modes(self) -> None:
        self.mode_combo.clear()
        candidate = self.candidate
        if candidate is None or not candidate.node_present:
            self.mode_combo.addItem("No available camera mode", None)
            self.mode_combo.setEnabled(False)
            self._refresh_mode_label()
            return
        modes = enumerate_modes_for_device(
            candidate, device_path=preferred_device_path(candidate)
        )
        if not modes:
            self.mode_combo.addItem("No modes reported by this device", None)
            item = self.mode_combo.model().item(0)
            if item is not None:
                item.setEnabled(False)
            self.mode_combo.setEnabled(False)
            self.state_label.setText(
                "The camera is present, but no capture modes were enumerated. Refresh or reconnect to retry."
            )
            self._refresh_mode_label()
            return
        for mode in modes:
            self.mode_combo.addItem(mode.label, mode)
        self.mode_combo.setEnabled(not self.busy)
        preferred_index = _preferred_mode(candidate, modes)
        self.mode_combo.setCurrentIndex(preferred_index)
        self._refresh_mode_label()

    def _mode_changed(self, _index: int) -> None:
        self._refresh_mode_label()
        self.update_controls()

    def _refresh_mode_label(self) -> None:
        mode = self.mode
        if mode is None:
            self.requested_label.setText("Requested: —")
        else:
            rate = f"{mode.fps} FPS" if mode.fps is not None else "driver default rate"
            self.requested_label.setText(f"Requested mode: {mode.width}×{mode.height} · {mode.pixel_format or 'driver format'} · {rate}")

    def start_capture(self, *, from_batch: bool = False) -> bool:
        candidate = self.candidate
        mode = self.mode
        if candidate is None or not candidate.node_present:
            if not from_batch:
                self.state_label.setText("Select an available camera before starting this slot.")
            return False
        if mode is None:
            if not from_batch:
                self.state_label.setText("This device has no enumerated mode available to request.")
            return False
        if self.owner.is_duplicate(self):
            self.state_label.setText("Start blocked: Camera A and Camera B must use different capture endpoints.")
            return False
        if self._worker and self._worker.is_alive():
            state = self._worker.snapshot().state
            return state in (CaptureState.STARTING, CaptureState.RUNNING)

        device = preferred_device_path(candidate)
        if not device:
            self.state_label.setText("This camera has no usable V4L2 device path.")
            return False

        self._preview_version = 0
        self._preview_rate.reset()
        self._preview_fps = 0.0
        self._last_image = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Opening camera…")
        self._active_id = candidate.identity.stable_identifier
        self._disconnect_notice = False
        self._disconnect_message = None
        self._worker = OpenCVCaptureWorker(
            device,
            mode,
            **({"capture_factory": self.capture_factory} if self.capture_factory is not None else {}),
            worker_label=self.slot_name.lower().replace(" ", "-"),
        )
        self._worker.start()
        self.state_label.setText("Starting; waiting for this camera's independent initialization.")
        self.update_metrics()
        self.update_controls()
        return True

    def stop_capture(self) -> None:
        if self._worker and self._worker.is_alive():
            self._worker.request_stop()
            self.state_label.setText("Stopping after the active frame read returns…")
        self.owner.slot_stopping(self)
        self.update_controls()

    def poll(self) -> CaptureSnapshot | None:
        if self._modes_need_refresh and not self.busy:
            self._modes_need_refresh = False
            self.load_modes()
        worker = self._worker
        if worker is None:
            self.update_metrics()
            self.update_controls()
            return None
        snapshot = worker.snapshot()
        latest = worker.frame_buffer.read_after(self._preview_version)
        if latest is not None and snapshot.state in (CaptureState.STARTING, CaptureState.RUNNING, CaptureState.STOPPING):
            frame, self._preview_version, _host_arrival_ns = latest
            try:
                self._last_image = frame_to_image(frame)
                self._paint_preview()
                self._preview_fps = self._preview_rate.add(time.monotonic_ns())
            except Exception as exc:
                logger.exception("%s preview conversion failed", self.slot_name)
                self.preview.setText(f"Preview error: {exc}")
        now_ns = time.monotonic_ns()
        if snapshot.state == CaptureState.RUNNING:
            self._preview_fps = self._preview_rate.value_at(now_ns)
        else:
            self._preview_fps = 0.0

        if snapshot.state == CaptureState.STARTING:
            state_text = "Starting"
            if not self._last_image:
                self.preview.setText("Opening camera…")
        elif snapshot.state == CaptureState.RUNNING:
            state_text = "Streaming"
            if self._last_image is None:
                self.preview.setText("Waiting for the first decoded frame…")
        elif snapshot.state == CaptureState.STOPPING:
            state_text = "Stopping"
        elif snapshot.state == CaptureState.UNAVAILABLE:
            state_text = "Unavailable"
            self.preview.setText("Camera stream ended. Reconnect or select a camera, then start again.")
        elif snapshot.state == CaptureState.FAILED:
            state_text = "Error"
            self.preview.setText(snapshot.message)
        else:
            state_text = "Stopped"
            if self._last_image is None:
                self.preview.setText("Stopped")
        state_message = snapshot.message
        if self._disconnect_notice:
            if snapshot.state in (CaptureState.STOPPED, CaptureState.UNAVAILABLE):
                state_text = "Stopped"
            if self._disconnect_message:
                state_message = self._disconnect_message
        self.state_label.setText(f"{state_text} · {state_message}")
        self._set_reported(snapshot)
        self.update_metrics(snapshot)
        self.update_controls()
        return snapshot

    def _set_reported(self, snapshot: CaptureSnapshot) -> None:
        settings = snapshot.negotiated
        if settings is None:
            self.reported_label.setText("Driver readback: waiting for camera initialization")
            return
        size = f"{settings.width}×{settings.height}" if settings.width and settings.height else "size unknown"
        pixel_format = settings.pixel_format or "format unknown"
        fps = f"{settings.fps:g} FPS" if settings.fps is not None else "rate unknown"
        self.reported_label.setText(f"Driver readback: {size} · {pixel_format} · {fps}")

    def update_metrics(self, snapshot: CaptureSnapshot | None = None) -> None:
        if snapshot is None and self._worker is not None:
            snapshot = self._worker.snapshot()
        running = bool(snapshot and snapshot.state == CaptureState.RUNNING)
        values = {
            "Capture FPS": f"{snapshot.capture_fps:.1f}" if snapshot and running else "0.0",
            "Preview FPS": f"{self._preview_fps:.1f}" if running else "0.0",
            "Frames": str(snapshot.frame_count if snapshot else 0),
            "Read failures": str(snapshot.read_failures if snapshot else 0),
            "Elapsed capture": f"{snapshot.elapsed_seconds:.1f} s" if snapshot else "0.0 s",
        }
        for key, value in values.items():
            self.metric_values[key].setText(value)

    def update_controls(self) -> None:
        busy = self.busy
        candidate = self.candidate
        valid = bool(candidate and candidate.node_present and self.mode is not None)
        self.start_button.setEnabled(valid and not busy and not self.owner.is_duplicate(self))
        self.stop_button.setEnabled(bool(busy and self._worker and self._worker.snapshot().state != CaptureState.STOPPING))
        self.camera_combo.setEnabled(not busy)
        self.mode_combo.setEnabled(valid and not busy)

    def _paint_preview(self) -> None:
        if self._last_image is None or self.preview.width() < 2 or self.preview.height() < 2:
            return
        pixmap = QPixmap.fromImage(self._last_image).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._paint_preview()

    def wait_for_stop(self, deadline: float) -> bool:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return True
        worker.request_stop()
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        return not worker.is_alive()


class MultiCameraWorkspace(QWidget):
    """Composition of two camera-owning capture workers; no paired recorder in Pass 1."""

    def __init__(
        self,
        *,
        refresh_requested: Callable[[], None] | None = None,
        capture_factory: Any = None,
    ) -> None:
        super().__init__()
        self.capture_factory = capture_factory
        self._refresh_requested = refresh_requested
        self._candidates: list[CameraCandidate] = []
        self._batch_results: dict[str, str | None] | None = None
        self._duplicate = False

        page = QVBoxLayout(self)
        page.setContentsMargins(12, 10, 12, 12)
        page.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("Multi-Camera Live Workbench")
        title.setStyleSheet("color: palette(text); font-size: 18px; font-weight: 600;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.refresh_button = QPushButton("Refresh cameras")
        self.refresh_button.clicked.connect(self.refresh_cameras)
        title_row.addWidget(self.refresh_button)
        page.addLayout(title_row)

        explanation = QLabel(
            "Each preview has its own V4L2 capture worker and latest-frame buffer. "
            "The streams are not hardware-synchronized. Grouped experiment recording is planned for CEL-013 Pass 2; "
            "use the Single Camera Workbench for recording in this pass."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: palette(text);")
        page.addWidget(explanation)

        controls = QHBoxLayout()
        self.start_both_button = QPushButton("Start Both")
        self.stop_both_button = QPushButton("Stop Both")
        self.stop_both_button.setEnabled(False)
        self.start_both_button.clicked.connect(self.start_both)
        self.stop_both_button.clicked.connect(self.stop_both)
        controls.addWidget(self.start_both_button)
        controls.addWidget(self.stop_both_button)
        controls.addStretch(1)
        page.addLayout(controls)

        self.status_label = QLabel("Discover cameras to choose independent capture modes.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: palette(text); font-weight: 600;")
        page.addWidget(self.status_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.slot_a = CameraSlotPanel("Camera A", self, capture_factory)
        self.slot_b = CameraSlotPanel("Camera B", self, capture_factory)
        self.splitter.addWidget(self.slot_a)
        self.splitter.addWidget(self.slot_b)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.splitter)
        page.addWidget(self.scroll, 1)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(33)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start()
        self._set_orientation()

    def refresh_cameras(self) -> None:
        if self._refresh_requested is not None:
            self._refresh_requested()
            for slot in (self.slot_a, self.slot_b):
                if slot.candidate and slot.candidate.node_present and slot.mode is None and not slot.busy:
                    slot.load_modes()
        else:
            self.status_label.setText("Refresh is unavailable because no discovery callback was configured.")

    def set_candidates(self, candidates: Iterable[CameraCandidate]) -> None:
        self._candidates = list(candidates)
        ordered = sorted((candidate for candidate in self._candidates if candidate.node_present), key=_camera_priority)
        unique: list[CameraCandidate] = []
        for candidate in ordered:
            if not any(same_capture_endpoint(candidate, prior) for prior in unique):
                unique.append(candidate)
        defaults = [candidate.identity.stable_identifier for candidate in unique[:2]]
        self.slot_a.set_candidates(self._candidates, defaults[0] if defaults else None)
        self.slot_b.set_candidates(self._candidates, defaults[1] if len(defaults) > 1 else None)
        self._update_duplicate_status()
        self._update_batch_status()
        if self._duplicate:
            return
        if not self._candidates:
            self.status_label.setText("No V4L2 image-capture cameras found. Connect a camera, then refresh.")
        elif len(unique) < 2:
            self.status_label.setText("One camera endpoint is available. Select a second distinct endpoint for dual preview.")

    def discovery_failed(self, message: str) -> None:
        self.status_label.setText(f"Camera discovery failed: {message}")

    def selection_is_allowed(self, panel: CameraSlotPanel, candidate: CameraCandidate) -> bool:
        other = self.slot_b if panel is self.slot_a else self.slot_a
        return not same_capture_endpoint(candidate, other.candidate)

    def slot_selection_changed(self, panel: CameraSlotPanel) -> None:
        self._batch_results = None
        self._update_duplicate_status()
        for slot in (self.slot_a, self.slot_b):
            slot.update_controls()
        if not self._duplicate:
            self.status_label.setText(f"{panel.slot_name} selection updated. Capture modes were enumerated from that endpoint.")

    def slot_stopping(self, _panel: CameraSlotPanel) -> None:
        if self._batch_results is not None:
            self._batch_results = None

    def is_duplicate(self, _panel: CameraSlotPanel | None = None) -> bool:
        return self._duplicate

    def _update_duplicate_status(self) -> None:
        self._duplicate = same_capture_endpoint(self.slot_a.candidate, self.slot_b.candidate)
        if self._duplicate:
            self.status_label.setText("Duplicate endpoint selected. Camera A and Camera B must use different physical capture endpoints.")
        for slot in (self.slot_a, self.slot_b):
            slot.update_controls()

    def start_both(self) -> None:
        self._batch_results = {}
        for slot in (self.slot_a, self.slot_b):
            if slot.start_capture(from_batch=True):
                self._batch_results[slot.slot_name] = None
            else:
                candidate = slot.candidate
                mode = slot.mode
                reason = (
                    "camera unavailable" if candidate is None or not candidate.node_present
                    else "previous capture worker is still stopping" if slot.busy
                    else "no enumerated mode" if mode is None
                    else "same capture endpoint selected in both slots"
                )
                self._batch_results[slot.slot_name] = reason
        self._update_batch_status()
        self.poll()

    def _update_batch_status(self) -> None:
        if self._batch_results is None:
            return
        for slot in (self.slot_a, self.slot_b):
            result = self._batch_results.get(slot.slot_name)
            if result is not None:
                continue
            worker = slot.worker
            if worker is None:
                self._batch_results[slot.slot_name] = "not started"
                continue
            snapshot = worker.snapshot()
            if snapshot.state == CaptureState.RUNNING:
                self._batch_results[slot.slot_name] = "running"
            elif snapshot.state in (CaptureState.FAILED, CaptureState.UNAVAILABLE, CaptureState.STOPPED) and not worker.is_alive():
                self._batch_results[slot.slot_name] = snapshot.message or snapshot.state.value

        if any(value is None for value in self._batch_results.values()):
            self.status_label.setText("Start Both requested; waiting for each camera to confirm its own capture state.")
            return
        running = [name for name, value in self._batch_results.items() if value == "running"]
        if len(running) == 2:
            self.status_label.setText("Both camera streams independently confirmed as running. They are not hardware-synchronized.")
        elif running:
            failures = [f"{name}: {value}" for name, value in self._batch_results.items() if value != "running"]
            self.status_label.setText(f"Partial start: {', '.join(running)} streaming; {'; '.join(failures)}. The healthy stream remains active.")
        else:
            failures = [f"{name}: {value}" for name, value in self._batch_results.items()]
            self.status_label.setText(f"No dual stream confirmed. {'; '.join(failures)}.")

    def stop_both(self) -> None:
        self._batch_results = None
        self.slot_a.stop_capture()
        self.slot_b.stop_capture()
        self.status_label.setText("Stop requested independently for both active capture workers.")

    def poll(self) -> None:
        self.slot_a.poll()
        self.slot_b.poll()
        if self._batch_results is not None:
            self._update_batch_status()
        self.stop_both_button.setEnabled(self.slot_a.busy or self.slot_b.busy)
        self.start_both_button.setEnabled(not self._duplicate and (
            (self.slot_a.candidate is not None and self.slot_a.mode is not None and not self.slot_a.busy)
            or (self.slot_b.candidate is not None and self.slot_b.mode is not None and not self.slot_b.busy)
            or self.slot_a.busy or self.slot_b.busy
        ))

    def _set_orientation(self) -> None:
        width = self.width()
        orientation = (
            Qt.Orientation.Horizontal if width >= 1160 else Qt.Orientation.Vertical
        )
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._set_orientation()

    def shutdown(self, timeout_seconds: float = 0.75) -> list[str]:
        self._poll_timer.stop()
        self.slot_a.stop_capture()
        self.slot_b.stop_capture()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        unfinished = []
        for slot in (self.slot_a, self.slot_b):
            if not slot.wait_for_stop(deadline):
                unfinished.append(slot.slot_name)
        return unfinished
