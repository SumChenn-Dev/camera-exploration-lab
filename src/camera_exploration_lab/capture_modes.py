"""Known single-camera presets and read-only per-device V4L2 mode enumeration."""

from __future__ import annotations

import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from typing import Iterable

from .models import CameraCandidate


@dataclass(frozen=True, slots=True)
class CaptureMode:
    """Requested V4L2 stream mode; driver readback remains authoritative."""

    name: str
    width: int
    height: int
    fps: int | None
    pixel_format: str | None = None

    @property
    def label(self) -> str:
        pixel_format = f" · {self.pixel_format}" if self.pixel_format else ""
        rate = f"{self.fps} FPS" if self.fps is not None else "driver default FPS"
        return f"{self.width}×{self.height} · {rate}{pixel_format}"


PS3_EYE_MODES = (
    CaptureMode("480p_60", 640, 480, 60, "GRBG"),
    CaptureMode("240p_125", 320, 240, 125, "GRBG"),
    CaptureMode("240p_187", 320, 240, 187, "GRBG"),
)

AVERMEDIA_CAM_313_MODES = (
    CaptureMode("mjpg_640_480_30", 640, 480, 30, "MJPG"),
    CaptureMode("mjpg_720p_20", 1280, 720, 20, "MJPG"),
    CaptureMode("mjpg_720p_30", 1280, 720, 30, "MJPG"),
    CaptureMode("mjpg_1080p_30", 1920, 1080, 30, "MJPG"),
    CaptureMode("yuyv_640_480_30", 640, 480, 30, "YUYV"),
    CaptureMode("yuyv_1080p_5", 1920, 1080, 5, "YUYV"),
)

GENERIC_V4L2_MODES = (CaptureMode("default_640_480_30", 640, 480, 30),)


def modes_for_camera(candidate: CameraCandidate | None) -> tuple[CaptureMode, ...]:
    """Return the legacy single-camera presets used by the existing workbench."""
    if candidate is None:
        return GENERIC_V4L2_MODES

    identity = candidate.identity
    usb_id = (identity.vendor_id, identity.product_id)
    if usb_id == ("1415", "2000"):
        return PS3_EYE_MODES
    if usb_id == ("07ca", "313a"):
        return AVERMEDIA_CAM_313_MODES
    return GENERIC_V4L2_MODES


# V4L2 ioctl values use the Linux _IOC layout and native structure sizes.
def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


_FMT_SIZE = 64
_FRMSIZE_SIZE = 44
_FRMIVAL_SIZE = 52
_ENUM_FMT = _iowr(2, _FMT_SIZE)
_ENUM_FRAMESIZES = _iowr(74, _FRMSIZE_SIZE)
_ENUM_FRAMEINTERVALS = _iowr(75, _FRMIVAL_SIZE)
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
_MAX_ENUM_ITEMS = 256


def _capture_type(candidate: CameraCandidate) -> int:
    device_caps = candidate.identity.metadata.get("v4l2_device_caps")
    try:
        effective_caps = int(device_caps)
    except (TypeError, ValueError):
        effective_caps = _V4L2_CAP_VIDEO_CAPTURE
    if effective_caps & _V4L2_CAP_VIDEO_CAPTURE:
        return _V4L2_BUF_TYPE_VIDEO_CAPTURE
    if effective_caps & _V4L2_CAP_VIDEO_CAPTURE_MPLANE:
        return _V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
    return _V4L2_BUF_TYPE_VIDEO_CAPTURE


def _pixel_format_text(code: int) -> str | None:
    try:
        text = code.to_bytes(4, "little").decode("ascii").rstrip("\0 ")
    except (OverflowError, UnicodeDecodeError):
        return None
    return text or None


def _try_ioctl(fd: int, request: int, payload: bytearray) -> bool:
    try:
        fcntl.ioctl(fd, request, payload, True)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP):
            return False
        raise
    return True


def _discrete_sizes(fd: int, pixel_format: int) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    for index in range(_MAX_ENUM_ITEMS):
        payload = bytearray(_FRMSIZE_SIZE)
        struct.pack_into("=III", payload, 0, index, pixel_format, 0)
        if not _try_ioctl(fd, _ENUM_FRAMESIZES, payload):
            break
        size_type = struct.unpack_from("=I", payload, 8)[0]
        if size_type == 1:
            width, height = struct.unpack_from("=II", payload, 12)
            if width and height:
                sizes.append((width, height))
        elif size_type in (2, 3):
            values = struct.unpack_from("=6I", payload, 12)
            min_width, max_width, _step_width, min_height, max_height, _step_height = values
            for dimensions in ((min_width, min_height), (max_width, max_height)):
                if dimensions[0] and dimensions[1] and dimensions not in sizes:
                    sizes.append(dimensions)
        else:
            break
    return sizes


def _fps_values(fd: int, pixel_format: int, width: int, height: int) -> tuple[int, ...]:
    fps_values: set[int] = set()
    for index in range(_MAX_ENUM_ITEMS):
        payload = bytearray(_FRMIVAL_SIZE)
        struct.pack_into("=IIIII", payload, 0, index, pixel_format, width, height, 0)
        if not _try_ioctl(fd, _ENUM_FRAMEINTERVALS, payload):
            break
        interval_type = struct.unpack_from("=I", payload, 16)[0]
        if interval_type == 1:
            numerator, denominator = struct.unpack_from("=II", payload, 20)
            if numerator and denominator:
                fps_values.add(max(1, round(denominator / numerator)))
        elif interval_type in (2, 3):
            # Advertised stepwise/continuous boundaries, not inferred intermediate rates.
            values = struct.unpack_from("=6I", payload, 20)
            for numerator, denominator in (values[:2], values[2:4]):
                if numerator and denominator:
                    fps_values.add(max(1, round(denominator / numerator)))
        else:
            break
    return tuple(sorted(fps_values, reverse=True))


def enumerate_modes_for_device(
    candidate: CameraCandidate,
    *,
    device_path: str | None = None,
) -> tuple[CaptureMode, ...]:
    """Enumerate modes reported by this V4L2 capture endpoint using read-only ioctls.

    Only discrete rates and the endpoints of driver-advertised stepwise/continuous
    ranges are offered. If the driver does not enumerate a rate, the UI can request
    its default rate without inventing a numeric FPS value.
    """
    identity = candidate.identity
    if device_path is None:
        if identity.identity_source == "v4l_by_id" and identity.stable_identifier.startswith("/dev/v4l/by-id/"):
            device_path = identity.stable_identifier
        else:
            device_path = identity.device_path
    if not device_path:
        return ()

    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(device_path, flags)
    except OSError:
        return ()

    capture_type = _capture_type(candidate)
    modes: dict[tuple[int, int, str, int | None], CaptureMode] = {}
    try:
        for format_index in range(_MAX_ENUM_ITEMS):
            fmt = bytearray(_FMT_SIZE)
            struct.pack_into("=II", fmt, 0, format_index, capture_type)
            if not _try_ioctl(fd, _ENUM_FMT, fmt):
                break
            pixel_code = struct.unpack_from("=I", fmt, 44)[0]
            pixel_format = _pixel_format_text(pixel_code)
            if not pixel_format:
                continue
            for width, height in _discrete_sizes(fd, pixel_code):
                rates = _fps_values(fd, pixel_code, width, height)
                requested_rates: Iterable[int | None] = rates or (None,)
                for fps in requested_rates:
                    mode_name = f"{pixel_format}_{width}x{height}_{fps or 'default'}"
                    mode = CaptureMode(mode_name, width, height, fps, pixel_format)
                    modes[(width, height, pixel_format, fps)] = mode
    except OSError:
        return ()
    finally:
        os.close(fd)

    return tuple(sorted(
        modes.values(),
        key=lambda mode: (mode.width * mode.height, mode.pixel_format or "", -(mode.fps or 0)),
    ))
