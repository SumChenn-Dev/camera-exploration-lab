"""Small, explicit navigation controls for interactive PyQtGraph plots."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget


def visible_plot_bounds(plot: pg.PlotWidget) -> tuple[float, float, float, float] | None:
    """Return finite bounds from visible data items, or None when none are usable."""
    x_min = y_min = math.inf
    x_max = y_max = -math.inf
    for item in plot.plotItem.listDataItems():
        if not item.isVisible():
            continue
        x_values, y_values = item.getData()
        if x_values is None or y_values is None:
            continue
        for x_value, y_value in zip(x_values, y_values):
            x, y = float(x_value), float(y_value)
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            x_min, x_max = min(x_min, x), max(x_max, x)
            y_min, y_max = min(y_min, y), max(y_max, y)
    if not math.isfinite(x_min):
        return None
    return x_min, x_max, y_min, y_max


def _padded(low: float, high: float, fraction: float = 0.04) -> tuple[float, float]:
    span = high - low
    if span <= 1e-12:
        span = max(abs(low) * 0.02, 0.1)
        low -= span / 2.0
        high += span / 2.0
    padding = (high - low) * fraction
    return low - padding, high + padding


def reset_plot_view(
    plot: pg.PlotWidget,
    x_range: tuple[float, float] | None = None,
) -> bool:
    """Restore a complete data view; x_range may preserve a recording's full duration."""
    bounds = visible_plot_bounds(plot)
    if bounds is None:
        return False
    x_min, x_max, y_min, y_max = bounds
    if x_range is None:
        x_limits = _padded(x_min, x_max)
    else:
        low, high = sorted((float(x_range[0]), float(x_range[1])))
        if not math.isfinite(low) or not math.isfinite(high):
            return False
        if high - low <= 1e-12:
            low, high = _padded(low, high, 0.0)
        x_limits = (low, high)
    y_limits = _padded(y_min, y_max)
    view = plot.plotItem.vb
    view.enableAutoRange(x=False, y=False)
    view.setRange(xRange=x_limits, yRange=y_limits, padding=0.0)
    return True


def fit_plot_to_data(plot: pg.PlotWidget) -> bool:
    """Fit both axes to finite points in currently visible plot items."""
    return reset_plot_view(plot)


def zoom_plot(plot: pg.PlotWidget, factor: float) -> bool:
    """Scale the current view around its center; factor below one zooms in."""
    if not math.isfinite(factor) or factor <= 0:
        return False
    view = plot.plotItem.vb
    x_range, y_range = view.viewRange()
    if not all(math.isfinite(value) for value in (*x_range, *y_range)):
        return False
    x_center = sum(x_range) / 2.0
    y_center = sum(y_range) / 2.0
    half_x = (x_range[1] - x_range[0]) * factor / 2.0
    half_y = (y_range[1] - y_range[0]) * factor / 2.0
    if half_x <= 0 or half_y <= 0:
        return False
    view.enableAutoRange(x=False, y=False)
    view.setRange(
        xRange=(x_center - half_x, x_center + half_x),
        yRange=(y_center - half_y, y_center + half_y),
        padding=0.0,
    )
    return True


class PlotNavigationControls(QWidget):
    """Compact labeled navigation controls for one PlotWidget."""

    reset_requested = Signal(str)
    expand_requested = Signal(str, bool)

    def __init__(self, key: str, plot: pg.PlotWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.plot = plot
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.fit_button = self._button("Fit to Data", "Fit visible valid points to both axes.")
        self.reset_button = self._button("Reset View", "Restore the selected recording's full elapsed-time view.")
        self.zoom_in_button = self._button("Zoom In", "Zoom toward the current plot center.")
        self.zoom_out_button = self._button("Zoom Out", "Zoom away from the current plot center.")
        self.expand_button = self._button("Expand Plot", "Focus this plot; activate again to restore the two-plot layout.")
        self.expand_button.setCheckable(True)
        for button in (
            self.fit_button,
            self.reset_button,
            self.zoom_in_button,
            self.zoom_out_button,
            self.expand_button,
        ):
            layout.addWidget(button)
        self.pan_hint = QLabel("Drag to pan · wheel to zoom")
        self.pan_hint.setToolTip("Drag inside the graph to pan. Use the mouse wheel to zoom around the pointer.")
        self.pan_hint.setProperty("celThemeText", True)
        self.pan_hint.setProperty("celThemeBaseStyle", "font-size: 10px;")
        layout.addWidget(self.pan_hint)
        layout.addStretch(1)
        self.fit_button.clicked.connect(self._fit)
        self.reset_button.clicked.connect(self._reset)
        self.zoom_in_button.clicked.connect(self._zoom_in)
        self.zoom_out_button.clicked.connect(self._zoom_out)
        self.expand_button.toggled.connect(self._expand)
        plot.plotItem.vb.setMouseMode(pg.ViewBox.PanMode)
        plot.plotItem.vb.setMenuEnabled(False)
        plot.hideButtons()

    @staticmethod
    def _button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    def _fit(self, _checked: bool = False) -> None:
        fit_plot_to_data(self.plot)

    def _reset(self, _checked: bool = False) -> None:
        self.reset_requested.emit(self.key)

    def _zoom_in(self, _checked: bool = False) -> None:
        zoom_plot(self.plot, 0.75)

    def _zoom_out(self, _checked: bool = False) -> None:
        zoom_plot(self.plot, 1.35)

    def _expand(self, expanded: bool) -> None:
        self.expand_button.setText("Restore Layout" if expanded else "Expand Plot")
        self.expand_requested.emit(self.key, expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.expand_button.blockSignals(True)
        self.expand_button.setChecked(expanded)
        self.expand_button.setText("Restore Layout" if expanded else "Expand Plot")
        self.expand_button.blockSignals(False)

    def set_data_available(self, available: bool) -> None:
        for button in (
            self.fit_button,
            self.reset_button,
            self.zoom_in_button,
            self.zoom_out_button,
        ):
            button.setEnabled(available)
