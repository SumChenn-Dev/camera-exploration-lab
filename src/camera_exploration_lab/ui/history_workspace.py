"""Searchable, read-only, asynchronous experiment history workspace."""

from __future__ import annotations

import json
import logging
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    Signal,
    QTimer,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..db import Database
from ..history import HistoryRow, make_history_rows
from ..identity import identity_fingerprint, identity_source_label
from .metrics_guide import MetricsGuideDialog

logger = logging.getLogger(__name__)
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class HistoryTableModel(QAbstractTableModel):
    HEADERS = ("Experiment / run", "Camera identity", "Source", "Status", "Started", "Capture FPS", "Preview FPS")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[HistoryRow] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.cells[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return row.selection
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 1:
            return f"{row.camera_group_label}\nStored identity group: {identity_fingerprint(row.camera_identity_key)}"
        if role == SORT_ROLE:
            return row.sort_values[index.column()]
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802 - Qt API
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def replace_rows(self, rows: list[HistoryRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class _HistoryProxy(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.query = ""
        self.camera_group_key: str | None = None
        self.source_filter: str | None = None
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802 - Qt API
        model = self.sourceModel()
        if not isinstance(model, HistoryTableModel) or source_row >= len(model.rows):
            return False
        row = model.rows[source_row]
        return (
            (not self.query or self.query in row.searchable_text)
            and (not self.camera_group_key or row.camera_group_key == self.camera_group_key)
            and (not self.source_filter or row.source_value == self.source_filter)
        )


class _TaskSignals(QObject):
    completed = Signal(int, str, object)
    failed = Signal(int, str)


class _HistoryTask(QRunnable):
    def __init__(self, request_id: int, operation: str, database: Database,
                 signals: _TaskSignals,
                 selection: tuple[str | None, str] | None = None) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.database = database
        self.signals = signals
        self.selection = selection

    def run(self) -> None:
        try:
            if self.operation == "list":
                result: object = make_history_rows(self.database.history_entries())
            else:
                assert self.selection is not None
                result = self.database.history_detail(self.selection[0], self.selection[1])
            self.signals.completed.emit(self.request_id, self.operation, result)
        except Exception as exc:
            logger.exception("History %s request failed", self.operation)
            self.signals.failed.emit(self.request_id, str(exc))


class HistoryWorkspace(QWidget):
    """Database-backed History with filterable rows and selection-safe navigation."""

    analyze_requested = Signal(str, object)

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._next_id = 0
        self._list_id = 0
        self._detail_id = 0
        self._shutting_down = False
        self._task_signals = _TaskSignals(self)
        self._task_signals.completed.connect(self._task_completed)
        self._task_signals.failed.connect(self._task_failed)
        self._metrics_dialog: MetricsGuideDialog | None = None
        self.model = HistoryTableModel(self)
        self.proxy = _HistoryProxy(self)
        self.proxy.setSourceModel(self.model)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        header = QHBoxLayout()
        title = QLabel("Recorded experiments and imported history")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.metrics_guide_button = QPushButton("Metrics Guide")
        self.metrics_guide_button.clicked.connect(self.open_metrics_guide)
        self.analyze_button = QPushButton("Analyze selected")
        self.analyze_button.setToolTip("Open this exact filtered and sorted history record in Analysis.")
        self.analyze_button.clicked.connect(self.analyze_selected)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.metrics_guide_button)
        header.addWidget(self.analyze_button)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search name, camera, source, notes, mode, FPS, date, or status…")
        self.search_edit.setClearButtonEnabled(True)
        self.camera_filter = QComboBox()
        self.camera_filter.setMinimumWidth(180)
        self.source_filter = QComboBox()
        self.source_filter.setMinimumWidth(140)
        self.clear_button = QPushButton("Clear filters")
        self.result_count = QLabel("Loading history…")
        self.result_count.setMinimumWidth(130)
        toolbar.addWidget(self.search_edit, 4)
        toolbar.addWidget(QLabel("Camera"))
        toolbar.addWidget(self.camera_filter, 2)
        toolbar.addWidget(QLabel("Source"))
        toolbar.addWidget(self.source_filter, 1)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.result_count)
        layout.addLayout(toolbar)

        self.history_table = QTableView()
        self.history_table.setModel(self.proxy)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSortingEnabled(True)
        self.history_table.sortByColumn(4, Qt.SortOrder.DescendingOrder)
        header_view = self.history_table.horizontalHeader()
        header_view.setSectionResizeMode(0, header_view.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, header_view.ResizeMode.ResizeToContents)
        for column in (2, 3, 4, 5, 6):
            header_view.setSectionResizeMode(column, header_view.ResizeMode.ResizeToContents)

        self.history_details = QPlainTextEdit()
        self.history_details.setReadOnly(True)
        self.history_details.setPlaceholderText("Select a history row to inspect its settings and measurements.")
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(4, 6, 4, 0)
        detail_heading = QLabel("Experiment details")
        detail_heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        detail_layout.addWidget(detail_heading)
        detail_layout.addWidget(self.history_details, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.history_table)
        splitter.addWidget(detail_panel)
        splitter.setSizes([430, 250])
        layout.addWidget(splitter, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._apply_filters)
        self.search_edit.textChanged.connect(self._search_changed)
        self.camera_filter.currentIndexChanged.connect(self._apply_filters)
        self.source_filter.currentIndexChanged.connect(self._apply_filters)
        self.clear_button.clicked.connect(self.clear_filters)
        self.history_table.selectionModel().currentChanged.connect(self._selection_changed)
        self.refresh()

    def refresh(self) -> None:
        if self._shutting_down:
            return
        self.result_count.setText("Loading history…")
        self._submit("list")

    def _search_changed(self, _text: str) -> None:
        self._debounce.start()

    def _submit(self, operation: str, selection: tuple[str | None, str] | None = None) -> None:
        if self._shutting_down:
            return
        self._next_id += 1
        request_id = self._next_id
        if operation == "list":
            self._list_id = request_id
            self._detail_id = request_id
        else:
            self._detail_id = request_id
        task = _HistoryTask(request_id, operation, self.database, self._task_signals, selection)
        self.pool.start(task)

    def _task_completed(self, request_id: int, operation: str, result: object) -> None:
        if self._shutting_down:
            return
        if operation == "list":
            if request_id != self._list_id:
                return
            rows = result if isinstance(result, list) else []
            self.model.replace_rows(rows)
            self._populate_filters()
            self._apply_filters()
            if rows and self.proxy.rowCount():
                self.history_table.selectRow(0)
            elif not rows:
                self.history_details.setPlainText(
                    "No sessions are stored in this database yet. New recordings and imported CSV history will appear here."
                )
            return
        if request_id != self._detail_id or not isinstance(result, dict):
            return
        self.history_details.setPlainText(self._format_detail(result))

    def _task_failed(self, request_id: int, message: str) -> None:
        if self._shutting_down:
            return
        if request_id == self._list_id:
            self.result_count.setText("History unavailable")
            self.history_details.setPlainText(f"History could not be read: {message}")
        elif request_id == self._detail_id:
            self.history_details.setPlainText(f"The selected history row could not be read: {message}")

    def _populate_filters(self) -> None:
        camera_current = self.camera_filter.currentData()
        source_current = self.source_filter.currentData()
        self.camera_filter.blockSignals(True)
        self.source_filter.blockSignals(True)
        self.camera_filter.clear()
        self.source_filter.clear()
        self.camera_filter.addItem("All cameras", None)
        cameras: dict[str, str] = {}
        for row in self.model.rows:
            cameras.setdefault(row.camera_group_key, row.camera_group_label)
        for key, name in sorted(cameras.items(), key=lambda item: item[1].casefold()):
            self.camera_filter.addItem(name, key)
        self.source_filter.addItem("All sources", None)
        sources = sorted({row.source_value for row in self.model.rows}, key=str.casefold)
        for source in sources:
            self.source_filter.addItem(_source_label(source), source)
        _restore_combo(self.camera_filter, camera_current)
        _restore_combo(self.source_filter, source_current)
        self.camera_filter.blockSignals(False)
        self.source_filter.blockSignals(False)

    def _apply_filters(self, *_args) -> None:
        self.proxy.query = self.search_edit.text().strip().casefold()
        self.proxy.camera_group_key = self.camera_filter.currentData()
        self.proxy.source_filter = self.source_filter.currentData()
        self.proxy.invalidate()
        self.result_count.setText(f"{self.proxy.rowCount():,} of {len(self.model.rows):,} experiments")
        if self.proxy.rowCount() == 0:
            self.history_details.setPlainText("No history entries match these filters. Clear filters to show all records.")

    def clear_filters(self) -> None:
        self.search_edit.clear()
        self.camera_filter.setCurrentIndex(0)
        self.source_filter.setCurrentIndex(0)
        self._apply_filters()

    def _selected_row(self) -> HistoryRow | None:
        index = self.history_table.currentIndex()
        if not index.isValid():
            return None
        source_index = self.proxy.mapToSource(index)
        if 0 <= source_index.row() < len(self.model.rows):
            return self.model.rows[source_index.row()]
        return None

    def analyze_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            self.history_details.setPlainText("Select a history row before opening Analysis.")
            return
        self.analyze_requested.emit(row.session_id, row.benchmark_id)

    def _selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._shutting_down or not current.isValid():
            return
        row = self._selected_row()
        if row is None:
            return
        self.history_details.setPlainText("Loading selected experiment details…")
        self._submit("detail", (row.benchmark_id, row.session_id))

    def open_metrics_guide(self) -> None:
        if self._metrics_dialog is None:
            self._metrics_dialog = MetricsGuideDialog(parent=self)
        dialog = self._metrics_dialog
        dialog.show()
        dialog.raise_()

    def stop_background_work(self) -> None:
        self._shutting_down = True
        self._debounce.stop()
        self.pool.waitForDone()

    @staticmethod
    def _format_detail(detail: dict[str, Any]) -> str:
        entry = detail["entry"]
        camera = str(entry.get("model") or entry.get("manufacturer") or "identity unavailable")
        stable_id = entry.get("stable_identifier")
        identity = (
            f"{identity_source_label(entry.get('identity_source'))} · {identity_fingerprint(str(stable_id))}"
            if stable_id else "not recorded"
        )
        lines = [
            str(entry.get("label") or "Unnamed session"),
            f"Source: {entry.get('source') or 'unknown'}",
            f"Camera: {camera}",
            f"Stored identity: {identity}",
            f"Started: {entry.get('benchmark_started_at') or entry.get('started_at') or 'unknown'}",
            f"Ended: {entry.get('benchmark_ended_at') or entry.get('ended_at') or 'unknown'}",
        ]
        if entry.get("status"):
            lines.append(f"Status: {entry['status']}")
        if entry.get("notes"):
            lines.extend(("", "Notes:", str(entry["notes"])))
        if entry.get("source_basename"):
            lines.extend((
                "", f"Imported source file: {entry['source_basename']}",
            ))
            lines.append(f"Original rows preserved: {entry.get('imported_row_count') or 0}")
            lines.append(f"Normalized measurements: {entry.get('imported_measurement_count') or 0}")
            if entry.get("raw_record_count") is not None:
                lines.append(f"Raw records: {entry['raw_record_count']}")
        if any(entry.get(key) is not None for key in (
            "duration_seconds", "capture_fps", "preview_fps", "processing_fps",
            "frame_count", "read_failures", "interval_count", "median_interval_ms",
            "p95_interval_ms", "p99_interval_ms",
        )):
            lines.extend((
                "", "Summary:", f"Duration: {_format_number(entry.get('duration_seconds'), 3, ' s')}",
                f"Capture FPS: {_format_number(entry.get('capture_fps'), 1)}",
                f"Preview FPS: {_format_number(entry.get('preview_fps'), 1)}",
                f"Processing FPS: {_format_number(entry.get('processing_fps'), 1)}",
                f"Frames: {entry.get('frame_count') or 0}",
                f"Read failures: {entry.get('read_failures') or 0}",
                f"Frame intervals: {entry.get('interval_count') or 0}",
                f"Median interval: {_format_number(entry.get('median_interval_ms'), 3, ' ms')}",
                f"p95 interval: {_format_number(entry.get('p95_interval_ms'), 3, ' ms')}",
                f"p99 interval: {_format_number(entry.get('p99_interval_ms'), 3, ' ms')}",
                "Host frame-arrival timing does not measure sensor-to-display latency.",
            ))
        if detail["settings"]:
            lines.extend(("", "Requested and driver-reported settings:"))
            for setting in detail["settings"]:
                requested = _parse_json_value(setting.get("requested_value_json"), "not requested")
                reported = _parse_json_value(setting.get("reported_value_json"), "not reported")
                lines.append(
                    f"  {setting['setting_key']}: requested {requested}; reported {reported} "
                    f"({setting.get('verification_status') or 'verification unavailable'}; "
                    f"{setting.get('verification_method') or 'method unavailable'})"
                )
        configuration = _parse_json_value(entry.get("configuration_json"), None)
        if configuration is not None:
            lines.extend(("", "Capture configuration:"))
            lines.append(json.dumps(_safe_configuration(configuration), ensure_ascii=False,
                                    indent=2, sort_keys=True))
        summary = _parse_json_value(entry.get("summary_json"), {})
        if isinstance(summary, dict) and summary.get("termination_reason"):
            lines.append(f"Termination reason: {summary['termination_reason']}")
        if detail["metrics"]:
            lines.extend(("", "Stored measurement streams:"))
            for metric in detail["metrics"]:
                lines.append(
                    f"  {metric['metric']} [{metric['stream_scope']}]: {metric['sample_count']} samples, "
                    f"avg {float(metric['average']):.4g} {metric['unit']}, "
                    f"range {float(metric['minimum']):.4g}–{float(metric['maximum']):.4g}"
                )
        return "\n".join(lines)


def _source_label(source: str) -> str:
    return {"imported": "Imported CSV", "live_experiment": "Live experiment"}.get(source, source)


def _restore_combo(combo: QComboBox, value: object) -> None:
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            combo.setCurrentIndex(index)
            return


def _format_number(value: object, precision: int, suffix: str = "") -> str:
    try:
        return f"{float(value):.{precision}f}{suffix}"
    except (TypeError, ValueError):
        return "not available"


def _parse_json_value(value: object, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _safe_configuration(value: Any) -> Any:
    hidden_keys = {"stable_identifier", "serial_number", "device_path", "source_path", "path"}
    if isinstance(value, dict):
        return {
            key: _safe_configuration(item)
            for key, item in value.items()
            if key.casefold() not in hidden_keys
        }
    if isinstance(value, list):
        return [_safe_configuration(item) for item in value]
    return value
