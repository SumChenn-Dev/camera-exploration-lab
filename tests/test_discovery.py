from __future__ import annotations

import tempfile
import struct
import os
import unittest
from pathlib import Path
from unittest.mock import patch

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

            with patch(
                "camera_exploration_lab.discovery._query_v4l2_capabilities",
                return_value=(0xC4200001, 0x04200001),
            ):
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

            with patch(
                "camera_exploration_lab.discovery._query_v4l2_capabilities",
                return_value=(0x04200001, 0x04200001),
            ):
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

            with patch(
                "camera_exploration_lab.discovery._query_v4l2_capabilities",
                return_value=(0x04200001, 0x04200001),
            ):
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

    def test_device_caps_exclude_metadata_nodes_and_keep_virtual_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev_root = root / "dev"
            sysfs_root = root / "sys/class/video4linux"
            usb_device = root / "sys/devices/usb1/mock-camera"
            usb_device.mkdir(parents=True)
            for node, name in (
                ("video17", "Mock UVC Camera"),
                ("video18", "Mock UVC Camera Metadata"),
                ("video4", "OBS Virtual Camera"),
            ):
                entry = sysfs_root / node
                entry.mkdir(parents=True)
                device_node = dev_root / node
                device_node.parent.mkdir(parents=True, exist_ok=True)
                device_node.touch()
                (entry / "name").write_text(name, encoding="utf-8")
                (entry / "device").symlink_to(usb_device if node.startswith("video1") else entry)

            broad_caps = 0x80000000 | 0x04200001 | 0x00800000
            image_caps = 0x04200001
            metadata_caps = 0x04A00000
            queried = {
                "video17": (broad_caps, image_caps),
                "video18": (broad_caps, metadata_caps),
                "video4": (0x04200001, 0x04200001),
            }

            def query(path: Path):
                return queried[path.name]

            with patch(
                "camera_exploration_lab.discovery._query_v4l2_capabilities",
                side_effect=query,
            ):
                cameras = V4L2Discovery(
                    dev_root=dev_root,
                    sysfs_root=sysfs_root,
                    by_id_root=root / "missing-by-id",
                ).discover()

        paths = {Path(camera.identity.device_path).name for camera in cameras}
        self.assertEqual(paths, {"video17", "video4"})
        virtual = next(camera for camera in cameras if camera.display_name == "OBS Virtual Camera")
        self.assertEqual(virtual.identity.backend.value, "v4l2")
        self.assertEqual(virtual.identity.metadata["v4l2_device_caps"], 0x04200001)

    def test_device_caps_query_uses_device_specific_mask_when_flag_is_set(self) -> None:
        from camera_exploration_lab.discovery import (
            _V4L2_CAP_DEVICE_CAPS,
            _VIDIOC_QUERYCAP,
            _V4L2_CAP_VIDEO_CAPTURE,
            _query_v4l2_capabilities,
            _supports_image_capture,
        )

        advertised = _V4L2_CAP_DEVICE_CAPS | 0x04200001 | 0x00800000
        image_device_caps = 0x04200001
        metadata_device_caps = 0x04A00000
        self.assertTrue(_supports_image_capture(image_device_caps))
        self.assertFalse(_supports_image_capture(metadata_device_caps))
        self.assertTrue(advertised & _V4L2_CAP_DEVICE_CAPS)
        self.assertTrue(image_device_caps & _V4L2_CAP_VIDEO_CAPTURE)

        def ioctl(_fd: int, request: int, payload: bytearray, _mutate: bool) -> int:
            self.assertEqual(request, _VIDIOC_QUERYCAP)
            struct.pack_into("=II", payload, 84, advertised, metadata_device_caps)
            return 0

        def open_read_only(_path: Path, flags: int) -> int:
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR), 0)
            return 123

        with patch("camera_exploration_lab.discovery.os.open", side_effect=open_read_only), \
                patch("camera_exploration_lab.discovery.os.close"), \
                patch("camera_exploration_lab.discovery.fcntl.ioctl", side_effect=ioctl):
            result = _query_v4l2_capabilities(Path("/dev/synthetic-node"))
        self.assertEqual(result, (advertised, metadata_device_caps))
        self.assertFalse(_supports_image_capture(result[1]))

        advertised_without_device_flag = 0x04200001

        def ioctl_without_device_flag(_fd: int, request: int, payload: bytearray, _mutate: bool) -> int:
            self.assertEqual(request, _VIDIOC_QUERYCAP)
            struct.pack_into("=II", payload, 84, advertised_without_device_flag, metadata_device_caps)
            return 0

        with patch("camera_exploration_lab.discovery.os.open", side_effect=open_read_only), \
                patch("camera_exploration_lab.discovery.os.close"), \
                patch("camera_exploration_lab.discovery.fcntl.ioctl", side_effect=ioctl_without_device_flag):
            result = _query_v4l2_capabilities(Path("/dev/synthetic-virtual"))
        self.assertEqual(result, (advertised_without_device_flag, advertised_without_device_flag))
        self.assertTrue(_supports_image_capture(result[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
