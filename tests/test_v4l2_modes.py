from __future__ import annotations

import errno
import struct
import unittest
from unittest.mock import patch

from camera_exploration_lab.capture_modes import (
    _ENUM_FMT,
    _ENUM_FRAMEINTERVALS,
    _ENUM_FRAMESIZES,
    enumerate_modes_for_device,
)
from camera_exploration_lab.models import BackendKind, CameraCandidate, CameraIdentity


def make_candidate(*, caps: int = 1) -> CameraCandidate:
    return CameraCandidate(
        identity=CameraIdentity(
            stable_identifier="/dev/video-test",
            backend=BackendKind.V4L2,
            device_path="/dev/video-test",
            identity_source="sysfs_path",
            metadata={"v4l2_device_caps": caps},
        ),
        display_name="Synthetic V4L2 camera",
    )


class V4L2ModeEnumerationTests(unittest.TestCase):
    def test_only_driver_reported_formats_sizes_and_intervals_are_offered(self) -> None:
        pixel_code = int.from_bytes(b"MJPG", "little")

        def ioctl(fd: int, request: int, payload: bytearray, _mutate: bool) -> int:
            self.assertEqual(fd, 44)
            if request == _ENUM_FMT:
                index, capture_type = struct.unpack_from("=II", payload)
                self.assertEqual(capture_type, 1)
                if index:
                    raise OSError(errno.EINVAL, "end of formats")
                struct.pack_into("=I", payload, 44, pixel_code)
                return 0
            if request == _ENUM_FRAMESIZES:
                index, code = struct.unpack_from("=II", payload)
                self.assertEqual(code, pixel_code)
                if index:
                    raise OSError(errno.EINVAL, "end of sizes")
                struct.pack_into("=I", payload, 8, 1)
                struct.pack_into("=II", payload, 12, 1280, 720)
                return 0
            if request == _ENUM_FRAMEINTERVALS:
                index, code, width, height = struct.unpack_from("=IIII", payload)
                self.assertEqual((code, width, height), (pixel_code, 1280, 720))
                if index >= 2:
                    raise OSError(errno.EINVAL, "end of intervals")
                struct.pack_into("=I", payload, 16, 1)
                struct.pack_into("=II", payload, 20, 1, 30 if index == 0 else 25)
                return 0
            self.fail(f"unexpected ioctl request {request}")

        with patch("camera_exploration_lab.capture_modes.os.open", return_value=44) as open_device, \
                patch("camera_exploration_lab.capture_modes.os.close") as close_device, \
                patch("camera_exploration_lab.capture_modes.fcntl.ioctl", side_effect=ioctl):
            modes = enumerate_modes_for_device(make_candidate())

        self.assertEqual([(mode.width, mode.height, mode.fps, mode.pixel_format) for mode in modes], [
            (1280, 720, 30, "MJPG"),
            (1280, 720, 25, "MJPG"),
        ])
        self.assertEqual(open_device.call_args.args[1] & 3, 0)  # O_RDONLY, never O_RDWR.
        close_device.assert_called_once_with(44)

    def test_mplane_capture_type_comes_from_effective_device_capabilities(self) -> None:
        seen_type: list[int] = []

        def ioctl(_fd: int, request: int, payload: bytearray, _mutate: bool) -> int:
            if request == _ENUM_FMT:
                _index, capture_type = struct.unpack_from("=II", payload)
                seen_type.append(capture_type)
                raise OSError(errno.EINVAL, "no enumerated formats")
            self.fail("no later enumeration should occur")

        with patch("camera_exploration_lab.capture_modes.os.open", return_value=44), \
                patch("camera_exploration_lab.capture_modes.os.close"), \
                patch("camera_exploration_lab.capture_modes.fcntl.ioctl", side_effect=ioctl):
            modes = enumerate_modes_for_device(make_candidate(caps=0x1000))

        self.assertEqual(modes, ())
        self.assertEqual(seen_type, [9])

    def test_unavailable_or_unreadable_endpoint_has_no_invented_modes(self) -> None:
        with patch(
            "camera_exploration_lab.capture_modes.os.open",
            side_effect=PermissionError("device is busy"),
        ):
            self.assertEqual(enumerate_modes_for_device(make_candidate()), ())


if __name__ == "__main__":
    unittest.main()
