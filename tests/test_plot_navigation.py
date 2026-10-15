from __future__ import annotations

import unittest

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.ui.plot_navigation import (
    PlotNavigationControls,
    fit_plot_to_data,
    reset_plot_view,
    visible_plot_bounds,
    zoom_plot,
)


class PlotNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_fit_uses_visible_finite_data_and_reset_preserves_full_duration(self) -> None:
        plot = pg.PlotWidget()
        visible = plot.plot([0.0, 15.0, 30.0], [60.0, 65.0, 70.0], name="visible")
        invalid = plot.plot([float("nan"), 5.0], [4.0, float("inf")], name="invalid")
        hidden = plot.plot([0.0, 30.0], [180.0, 190.0], name="hidden")
        hidden.hide()
        self.assertEqual(visible_plot_bounds(plot), (0.0, 30.0, 60.0, 70.0))
        self.assertTrue(fit_plot_to_data(plot))
        fitted = plot.getViewBox().viewRange()
        self.assertLess(fitted[0][0], 0.0)
        self.assertGreater(fitted[0][1], 30.0)
        self.assertLess(fitted[1][0], 60.0)
        self.assertGreater(fitted[1][1], 70.0)
        self.assertTrue(reset_plot_view(plot, (0.0, 30.0)))
        reset = plot.getViewBox().viewRange()
        self.assertEqual(reset[0], [0.0, 30.0])
        self.assertTrue(visible.isVisible())
        self.assertTrue(invalid.isVisible())
        self.assertFalse(hidden.isVisible())
        plot.close()

    def test_zoom_changes_only_the_view_and_empty_plot_reports_no_bounds(self) -> None:
        plot = pg.PlotWidget()
        plot.plot([0.0, 30.0], [60.0, 70.0])
        self.assertTrue(reset_plot_view(plot, (0.0, 30.0)))
        before = plot.getViewBox().viewRange()
        stored_before = tuple(tuple(values) for values in plot.plotItem.listDataItems()[0].getData())
        self.assertTrue(zoom_plot(plot, 0.5))
        zoomed = plot.getViewBox().viewRange()
        self.assertAlmostEqual(zoomed[0][1] - zoomed[0][0], (before[0][1] - before[0][0]) / 2)
        self.assertTrue(zoom_plot(plot, 2.0))
        restored = plot.getViewBox().viewRange()
        self.assertAlmostEqual(restored[0][1] - restored[0][0], before[0][1] - before[0][0])
        stored_after = tuple(tuple(values) for values in plot.plotItem.listDataItems()[0].getData())
        self.assertEqual(stored_after, stored_before)
        plot.clear()
        plot.plotItem.legend = None
        self.assertIsNone(visible_plot_bounds(plot))
        self.assertFalse(fit_plot_to_data(plot))
        self.assertFalse(reset_plot_view(plot))
        plot.close()

    def test_controls_expose_pan_zoom_fit_reset_and_expand_actions(self) -> None:
        plot = pg.PlotWidget()
        plot.plot([0.0, 30.0], [60.0, 70.0], name="capture")
        controls = PlotNavigationControls("fps", plot)
        self.assertEqual(plot.plotItem.vb.state["mouseMode"], pg.ViewBox.PanMode)
        self.assertEqual(controls.pan_hint.text(), "Drag to pan · wheel to zoom")
        reset_keys: list[str] = []
        expanded: list[tuple[str, bool]] = []
        controls.reset_requested.connect(reset_keys.append)
        controls.expand_requested.connect(lambda key, state: expanded.append((key, state)))
        controls.reset_button.click()
        controls.expand_button.click()
        controls.expand_button.click()
        self.assertEqual(reset_keys, ["fps"])
        self.assertEqual(expanded, [("fps", True), ("fps", False)])
        controls.set_data_available(False)
        self.assertFalse(controls.fit_button.isEnabled())
        self.assertFalse(controls.zoom_in_button.isEnabled())
        controls.set_data_available(True)
        self.assertTrue(controls.fit_button.isEnabled())
        controls.close()
        plot.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
