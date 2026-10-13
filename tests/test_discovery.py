from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.discovery import V4L2Discovery


class V4L2DiscoveryTests(unittest.TestCase):
    def test_uses_by_id_link_and_reads_mock_usb_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev_root = root / "dev"
            sysfs_root = root / "sys/class/video4linux"
            usb_device = root / "sys/devices/usb1/1-7/1-7:1.0"
            video_node = dev_root / "video8"
            by_id = dev_root / "v4l/by-id"
            (sysfs_root / "video8").mkdir(parents=True)
            usb_device.mkdir(parents=True)
            video_node.parent.mkdir(parents=True, exist_ok=True)
            video_node.touch()
            (sysfs_root / "video8" / "name").write_text("Mock UVC Camera\n", encoding="utf-8")
            (sysfs_root / "video8" / "device").symlink_to(usb_device)
            for name, value in {
                "idVendor": "07ca",
                "idProduct": "313a",
                "manufacturer": "AVerMedia",
                "product": "Live Streamer CAM 313",
                "serial": "MOCK-001",
            }.items():
                (usb_device / name).write_text(value + "\n", encoding="utf-8")
            by_id.mkdir(parents=True)
            stable_link = by_id / "usb-MOCK-001-video-index0"
            stable_link.symlink_to(video_node)

            cameras = V4L2Discovery(
                dev_root=dev_root,
                sysfs_root=sysfs_root,
                by_id_root=by_id,
            ).discover()

            self.assertEqual(len(cameras), 1)
            camera = cameras[0]
            self.assertEqual(camera.identity.stable_identifier, str(stable_link))
            self.assertEqual(camera.identity.device_path, str(video_node))
            self.assertEqual(camera.identity.identity_source, "v4l_by_id")
            self.assertEqual(camera.identity.vendor_id, "07ca")
            self.assertEqual(camera.identity.product_id, "313a")
            self.assertEqual(camera.identity.serial_number, "MOCK-001")
            self.assertIn("AVerMedia", camera.display_name)
            self.assertEqual(camera.identity.model, "Live Streamer CAM 313")
            self.assertTrue(camera.node_present)

    def test_falls_back_to_sysfs_device_path_without_by_id_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs_root = root / "sys/class/video4linux"
            sysfs_entry = sysfs_root / "video2"
            device = root / "sys/devices/platform/mock-video"
            sysfs_entry.mkdir(parents=True)
            device.mkdir(parents=True)
            (sysfs_entry / "device").symlink_to(device)
            (sysfs_entry / "name").write_text("Mock Video Node\n", encoding="utf-8")

            camera = V4L2Discovery(
                dev_root=root / "dev",
                sysfs_root=sysfs_root,
                by_id_root=root / "missing-by-id",
            ).discover()[0]

            self.assertEqual(camera.identity.identity_source, "sysfs_path")
            self.assertIn("mock-video", camera.identity.stable_identifier)
            self.assertNotEqual(camera.identity.stable_identifier, "/dev/video2")
            self.assertFalse(camera.node_present)

    def test_usb_endpoints_with_the_same_serial_receive_distinct_stable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev_root = root / "dev"
            sysfs_root = root / "sys/class/video4linux"
            usb_device = root / "sys/devices/usb1/1-3/1-3:1.0"
            usb_device.mkdir(parents=True)
            for name, value in {
                "idVendor": "07ca",
                "idProduct": "313a",
                "serial": "MOCK-SHARED-001",
            }.items():
                (usb_device / name).write_text(value + "\n", encoding="utf-8")
            for node, index in (("video4", "0"), ("video5", "1")):
                entry = sysfs_root / node
                entry.mkdir(parents=True)
                (dev_root / node).parent.mkdir(parents=True, exist_ok=True)
                (dev_root / node).touch()
                (entry / "device").symlink_to(usb_device)
                (entry / "name").write_text("Mock AVerMedia\n", encoding="utf-8")
                (entry / "index").write_text(index + "\n", encoding="utf-8")

            cameras = V4L2Discovery(
                dev_root=dev_root,
                sysfs_root=sysfs_root,
                by_id_root=root / "missing-by-id",
            ).discover()

            self.assertEqual(len(cameras), 2)
            self.assertNotEqual(cameras[0].identity.stable_identifier, cameras[1].identity.stable_identifier)
            self.assertTrue(cameras[0].identity.stable_identifier.endswith("index-0"))
            self.assertTrue(cameras[1].identity.stable_identifier.endswith("index-1"))

    def test_missing_sysfs_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = V4L2Discovery(sysfs_root=Path(directory) / "absent").discover()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
