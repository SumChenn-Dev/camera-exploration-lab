from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from camera_exploration_lab.capture import CaptureState
from camera_exploration_lab.capture_modes import CaptureMode
from camera_exploration_lab.models import BackendKind, CameraCandidate, CameraIdentity
from camera_exploration_lab.ui.multi_camera_workbench import (
    MultiCameraWorkspace,
    capture_endpoint_key,
    same_capture_endpoint,
)

APP = QApplication.instance() or QApplication([])
TEST_MODES = (
    CaptureMode("low", 320, 240, 30, "MJPG"),
    CaptureMode("high", 640, 480, 60, "MJPG"),
)


def make_candidate(name: str, path: str, *, stable: str | None = None, vendor: str = "ffff",
                   product: str = "0001", node_present: bool = True) -> CameraCandidate:
    return CameraCandidate(
        identity=CameraIdentity(
            stable_identifier=stable or path,
            backend=BackendKind.V4L2,
            device_path=path,
            manufacturer="Synthetic",
            model=name,
            vendor_id=vendor,
            product_id=product,
            identity_source="v4l_by_id" if stable and stable.startswith("/dev/v4l/by-id/") else "sysfs_path",
        ),
        display_name=name,
        node_present=node_present,
    )


class FakeCapture:
    def __init__(self, *, opened: bool = True, delay: float = 0.002) -> None:
        self.opened = opened
        self.delay = delay
        self.properties: dict[int, float] = {}
        self.released = threading.Event()
        self.disconnected = threading.Event()
        self.read_count = 0

    def isOpened(self) -> bool:
        return self.opened

    def set(self, prop: int, value: float) -> bool:
        self.properties[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.properties.get(prop, 0.0)

    def read(self):
        if self.delay:
            time.sleep(self.delay)
        if self.disconnected.is_set():
            return False, None
        self.read_count += 1
        return True, np.zeros((12, 20, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released.set()


class MultiCameraWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enumerator = patch(
            "camera_exploration_lab.ui.multi_camera_workbench.enumerate_modes_for_device",
            return_value=TEST_MODES,
        )
        self.enumerator.start()
        self.addCleanup(self.enumerator.stop)
        self.captures: dict[str, list[FakeCapture]] = {}
        self.workspace: MultiCameraWorkspace | None = None

    def tearDown(self) -> None:
        if self.workspace is not None:
            self.workspace.shutdown(timeout_seconds=0.5)
            self.workspace.close()
            APP.processEvents()

    def factory(
        self, *, failed: set[str] | None = None, delay: float = 0.002,
        delays: dict[str, float] | None = None,
    ):
        failed = failed or set()
        delays = delays or {}

        def create(device: str) -> FakeCapture:
            capture = FakeCapture(opened=device not in failed, delay=delays.get(device, delay))
            self.captures.setdefault(device, []).append(capture)
            return capture

        return create

    def create_workspace(
        self,
        candidates: list[CameraCandidate] | None = None,
        *,
        factory=None,
    ) -> MultiCameraWorkspace:
        self.workspace = MultiCameraWorkspace(capture_factory=factory or self.factory())
        self.workspace.resize(1366, 768)
        self.workspace.show()
        if candidates is None:
            candidates = [
                make_candidate("Sony PlayStation Eye", "/dev/synthetic-eye", vendor="1415", product="2000"),
                make_candidate("AVerMedia CAM 313", "/dev/synthetic-avermedia", vendor="07ca", product="313a"),
            ]
        self.workspace.set_candidates(candidates)
        APP.processEvents()
        return self.workspace

    def wait_until(self, predicate, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        APP.processEvents()
        return bool(predicate())

    def test_start_both_runs_independent_rates_and_stopping_a_leaves_b_active(self) -> None:
        workspace = self.create_workspace(factory=self.factory(
            delay=0.008, delays={"/dev/synthetic-eye": 0.001, "/dev/synthetic-avermedia": 0.012}
        ))
        workspace.start_both_button.click()
        self.assertTrue(self.wait_until(lambda: all(
            slot.worker is not None and slot.worker.snapshot().state == CaptureState.RUNNING
            for slot in (workspace.slot_a, workspace.slot_b)
        )))
        self.assertTrue(self.wait_until(lambda: (
            workspace.slot_a.worker.snapshot().frame_count >= 5
            and workspace.slot_b.worker.snapshot().frame_count >= 5
        )))
        worker_a, worker_b = workspace.slot_a.worker, workspace.slot_b.worker
        self.assertIsNotNone(worker_a)
        self.assertIsNotNone(worker_b)
        assert worker_a is not None and worker_b is not None
        count_a = worker_a.snapshot().frame_count
        count_b = worker_b.snapshot().frame_count
        self.assertGreater(count_a, count_b)
        self.assertGreater(count_b, 0)
        self.assertTrue(self.wait_until(lambda: workspace.status_label.text().startswith("Both camera streams")))
        self.assertIn("not hardware-synchronized", workspace.status_label.text().lower())

        workspace.slot_a.stop_capture()
        self.assertTrue(self.wait_until(lambda: not worker_a.is_alive()))
        count_b_after_stop = worker_b.snapshot().frame_count
        self.assertTrue(worker_b.is_alive())
        self.assertTrue(self.wait_until(lambda: worker_b.snapshot().frame_count > count_b_after_stop))
        self.assertEqual(worker_a.snapshot().capture_fps, 0.0)
        workspace.slot_a.poll()
        self.assertEqual(workspace.slot_a.metric_values["Frames"].text(), str(worker_a.snapshot().frame_count))
        workspace.stop_both_button.click()
        self.assertTrue(self.wait_until(lambda: not worker_b.is_alive()))
        self.assertTrue(self.captures["/dev/synthetic-eye"][0].released.is_set())

    def test_camera_b_can_start_independently_while_camera_a_is_idle(self) -> None:
        workspace = self.create_workspace()
        workspace.slot_b.start_button.click()
        self.assertTrue(self.wait_until(lambda: workspace.slot_b.worker
                                       and workspace.slot_b.worker.snapshot().frame_count > 2))
        self.assertFalse(workspace.slot_a.busy)
        self.assertTrue(workspace.slot_b.busy)
        workspace.slot_b.stop_capture()
        self.assertTrue(self.wait_until(lambda: not workspace.slot_b.busy))

    def test_partial_start_failure_keeps_healthy_camera_running_and_reports_partial_success(self) -> None:
        candidates = [
            make_candidate("Healthy", "/dev/healthy"),
            make_candidate("Unavailable", "/dev/unavailable"),
        ]
        workspace = self.create_workspace(candidates, factory=self.factory(failed={"/dev/unavailable"}))
        workspace.start_both()
        self.assertTrue(self.wait_until(lambda: workspace.status_label.text().startswith("Partial start:"), timeout=1.0))
        self.assertTrue(workspace.slot_a.worker and workspace.slot_a.worker.is_alive())
        self.assertTrue(workspace.slot_b.worker and workspace.slot_b.worker.snapshot().state == CaptureState.FAILED)
        self.assertIn("healthy stream remains active", workspace.status_label.text())
        workspace.stop_both()
        self.assertTrue(self.wait_until(lambda: not workspace.slot_a.busy))

    def test_startup_is_asynchronous_and_one_slot_restart_does_not_reset_the_other(self) -> None:
        candidates = [make_candidate("A", "/dev/a"), make_candidate("B", "/dev/b")]
        entered = {path: threading.Event() for path in ("/dev/a", "/dev/b")}
        release = {path: threading.Event() for path in ("/dev/a", "/dev/b")}
        self.captures = {}

        def blocking_factory(device: str) -> FakeCapture:
            entered[device].set()
            release[device].wait(timeout=1.0)
            capture = FakeCapture(delay=0.002)
            self.captures.setdefault(device, []).append(capture)
            return capture

        workspace = self.create_workspace(candidates, factory=blocking_factory)
        workspace.start_both()
        self.assertTrue(entered["/dev/a"].wait(timeout=0.5))
        self.assertTrue(entered["/dev/b"].wait(timeout=0.5))
        self.assertIn("waiting for each camera", workspace.status_label.text().lower())
        # Both camera opens are blocked in their own workers while the Qt event loop still runs.
        APP.processEvents()
        for event in release.values():
            event.set()
        self.assertTrue(self.wait_until(lambda: all(slot.worker and slot.worker.snapshot().state == CaptureState.RUNNING
                                                   for slot in (workspace.slot_a, workspace.slot_b))))
        worker_b = workspace.slot_b.worker
        assert worker_b is not None
        before = worker_b.snapshot().frame_count
        workspace.slot_a.stop_capture()
        self.assertTrue(self.wait_until(lambda: not workspace.slot_a.busy))
        workspace.slot_a.start_capture()
        self.assertTrue(self.wait_until(lambda: len(self.captures["/dev/a"]) == 2))
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker and workspace.slot_a.worker.snapshot().frame_count > 0))
        self.assertGreaterEqual(worker_b.snapshot().frame_count, before)
        self.assertTrue(worker_b.is_alive())
        workspace.stop_both()
        self.assertTrue(self.wait_until(lambda: not workspace.slot_a.busy and not workspace.slot_b.busy))

    def test_each_slot_keeps_its_requested_mode_and_driver_readback_separate(self) -> None:
        workspace = self.create_workspace()
        workspace.slot_a.mode_combo.setCurrentIndex(0)
        workspace.slot_b.mode_combo.setCurrentIndex(1)
        self.assertEqual(workspace.slot_a.mode.fps, 30)
        self.assertEqual(workspace.slot_b.mode.fps, 60)
        self.assertIn("30 FPS", workspace.slot_a.requested_label.text())
        self.assertIn("60 FPS", workspace.slot_b.requested_label.text())
        workspace.start_both()
        self.assertTrue(self.wait_until(lambda: all(slot.worker
                                                   and slot.worker.snapshot().state == CaptureState.RUNNING
                                                   for slot in (workspace.slot_a, workspace.slot_b))))
        workspace.slot_a.poll()
        workspace.slot_b.poll()
        self.assertIn("30 FPS", workspace.slot_a.reported_label.text())
        self.assertIn("60 FPS", workspace.slot_b.reported_label.text())
        self.assertEqual(workspace.slot_a.worker.snapshot().read_failures, 0)
        self.assertEqual(workspace.slot_b.worker.snapshot().read_failures, 0)
        workspace.stop_both()
        self.assertTrue(self.wait_until(lambda: not workspace.slot_a.busy and not workspace.slot_b.busy))

    def test_shutdown_stops_both_active_workers_with_bounded_wait(self) -> None:
        workspace = self.create_workspace(factory=self.factory(delay=0.005))
        workspace.start_both()
        self.assertTrue(self.wait_until(lambda: all(slot.worker and slot.worker.snapshot().frame_count > 2
                                                   for slot in (workspace.slot_a, workspace.slot_b))))
        started = time.monotonic()
        unfinished = workspace.shutdown(timeout_seconds=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(unfinished, [])
        self.assertLess(elapsed, 0.6)
        self.assertFalse(workspace.slot_a.busy)
        self.assertFalse(workspace.slot_b.busy)
        self.assertTrue(all(captures[-1].released.is_set() for captures in self.captures.values()))

    def test_disconnect_marks_only_that_slot_unavailable(self) -> None:
        workspace = self.create_workspace()
        workspace.start_both()
        self.assertTrue(self.wait_until(lambda: all(slot.worker and slot.worker.snapshot().frame_count >= 3
                                                   for slot in (workspace.slot_a, workspace.slot_b))))
        eye_capture = self.captures["/dev/synthetic-eye"][0]
        eye_capture.disconnected.set()
        self.assertTrue(self.wait_until(
            lambda: workspace.slot_a.worker.snapshot().state == CaptureState.UNAVAILABLE
            and "Unavailable" in workspace.slot_a.state_label.text()
        ))
        self.assertTrue(workspace.slot_b.worker and workspace.slot_b.worker.is_alive())
        workspace.stop_both()

    def test_device_node_loss_and_reappearance_require_manual_restart(self) -> None:
        eye = make_candidate("PlayStation Eye", "/dev/video1", stable="/dev/v4l/by-id/eye",
                             vendor="1415", product="2000")
        aver = make_candidate("AVerMedia", "/dev/video2", stable="/dev/v4l/by-id/aver",
                              vendor="07ca", product="313a")
        workspace = self.create_workspace([eye, aver])
        self.assertTrue(workspace.slot_a.start_capture())
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker
                                       and workspace.slot_a.worker.snapshot().frame_count > 2))
        worker = workspace.slot_a.worker
        eye.node_present = False
        workspace.set_candidates([eye, aver])
        self.assertIn("disappeared", workspace.slot_a.state_label.text().lower())
        self.assertTrue(self.wait_until(lambda: not worker.is_alive()))
        self.assertFalse(workspace.slot_a.start_button.isEnabled())

        eye.node_present = True
        workspace.set_candidates([eye, aver])
        self.assertIn("available again", workspace.slot_a.state_label.text().lower())
        self.assertFalse(workspace.slot_a.busy)
        self.assertTrue(workspace.slot_a.start_button.isEnabled())
        self.assertTrue(workspace.slot_a.start_capture())
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker
                                       and workspace.slot_a.worker.snapshot().frame_count > 0))
        workspace.stop_both()

    def test_fallback_node_renumbering_stops_old_endpoint_and_uses_new_path_on_manual_restart(self) -> None:
        stable = "v4l2:usb:synthetic-port:index-0"
        old = make_candidate("PlayStation Eye", "/dev/video1", stable=stable,
                             vendor="1415", product="2000")
        aver = make_candidate("AVerMedia", "/dev/video2", stable="/dev/aver",
                              vendor="07ca", product="313a")
        workspace = self.create_workspace([old, aver])
        self.assertTrue(workspace.slot_a.start_capture())
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker
                                       and workspace.slot_a.worker.snapshot().frame_count > 2))
        worker = workspace.slot_a.worker
        moved = make_candidate("PlayStation Eye", "/dev/video42", stable=stable,
                               vendor="1415", product="2000")
        workspace.set_candidates([moved, aver])
        self.assertTrue(self.wait_until(lambda: not worker.is_alive()))
        self.assertIn("node changed", workspace.slot_a.state_label.text().lower())
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.start_button.isEnabled()))
        self.assertTrue(workspace.slot_a.start_capture())
        self.assertTrue(self.wait_until(lambda: len(self.captures.get("/dev/video42", [])) == 1))
        self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker
                                       and workspace.slot_a.worker.snapshot().frame_count > 0))
        workspace.stop_both()

    def test_duplicate_symlink_alias_selection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            endpoint = root / "video7"
            alias = root / "camera-alias"
            endpoint.touch()
            alias.symlink_to(endpoint)
            first = make_candidate("endpoint", str(endpoint), stable="stable-endpoint")
            second = make_candidate("alias", str(alias), stable="stable-alias")
            self.assertEqual(capture_endpoint_key(first), capture_endpoint_key(second))
            self.assertTrue(same_capture_endpoint(first, second))

            workspace = self.create_workspace([first, second])
            self.assertIsNotNone(workspace.slot_a.candidate)
            self.assertIsNone(workspace.slot_b.candidate)
            duplicate_index = next(
                index for index in range(workspace.slot_b.camera_combo.count())
                if (candidate := workspace.slot_b.camera_combo.itemData(index)) is second
            )
            workspace.slot_b.camera_combo.setCurrentIndex(duplicate_index)
            APP.processEvents()
            self.assertIsNone(workspace.slot_b.candidate)
            self.assertIn("Selection rejected", workspace.slot_b.state_label.text())
            self.assertFalse(workspace._duplicate)
            self.assertTrue(workspace.start_both_button.isEnabled())

    def test_stable_selection_survives_ephemeral_device_number_change(self) -> None:
        stable = "/dev/v4l/by-id/usb-synthetic-camera-video-index0"
        old = make_candidate("PlayStation Eye", "/dev/video1", stable=stable, vendor="1415", product="2000")
        other = make_candidate("AVerMedia", "/dev/video2", stable="/dev/v4l/by-id/usb-other-video-index0",
                               vendor="07ca", product="313a")
        workspace = self.create_workspace([old, other])
        self.assertEqual(workspace.slot_a.candidate.identity.device_path, "/dev/video1")
        moved = make_candidate("PlayStation Eye", "/dev/video42", stable=stable, vendor="1415", product="2000")
        workspace.set_candidates([moved, other])
        self.assertEqual(workspace.slot_a.candidate.identity.stable_identifier, stable)
        self.assertEqual(workspace.slot_a.candidate.identity.device_path, "/dev/video42")

    def test_empty_and_enumeration_failure_states_are_explicit(self) -> None:
        workspace = self.create_workspace([])
        self.assertIn("No V4L2", workspace.status_label.text())
        self.assertFalse(workspace.slot_a.start_button.isEnabled())
        self.enumerator.stop()
        failed_enumerator = patch(
            "camera_exploration_lab.ui.multi_camera_workbench.enumerate_modes_for_device",
            return_value=(),
        )
        failed_enumerator.start()
        self.addCleanup(failed_enumerator.stop)
        workspace.set_candidates([make_candidate("Undisclosed", "/dev/unsupported")])
        self.assertIn("no modes reported", workspace.slot_a.mode_combo.currentText().lower())
        self.assertFalse(workspace.slot_a.start_button.isEnabled())

    def test_splitter_adapts_to_narrow_and_wide_window_sizes(self) -> None:
        workspace = self.create_workspace()
        for size, expected in (
            ((1024, 768), Qt.Orientation.Vertical),
            ((1366, 768), Qt.Orientation.Horizontal),
            ((1920, 1080), Qt.Orientation.Horizontal),
        ):
            workspace.resize(*size)
            APP.processEvents()
            self.assertEqual(workspace.splitter.orientation(), expected)
            self.assertEqual(workspace.scroll.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_repeated_start_stop_releases_each_worker_without_affecting_idle_slot(self) -> None:
        workspace = self.create_workspace()
        captures: list[FakeCapture] = []
        for _ in range(3):
            self.assertTrue(workspace.slot_a.start_capture())
            self.assertTrue(self.wait_until(lambda: workspace.slot_a.worker and workspace.slot_a.worker.snapshot().frame_count > 2))
            worker = workspace.slot_a.worker
            self.assertIsNotNone(worker)
            workspace.slot_a.stop_capture()
            self.assertTrue(self.wait_until(lambda: not worker.is_alive()))
            self.assertTrue(self.captures["/dev/synthetic-eye"][-1].released.is_set())
            captures.append(self.captures["/dev/synthetic-eye"][-1])
            self.assertFalse(workspace.slot_b.busy)
        self.assertEqual(len(captures), 3)
        self.assertTrue(all(capture.released.is_set() for capture in captures))


if __name__ == "__main__":
    unittest.main()
