"""Known V4L2 mode profiles grounded in the existing benchmark records."""

from __future__ import annotations

from dataclasses import dataclass

from .models import CameraCandidate


@dataclass(frozen=True, slots=True)
class CaptureMode:
    """Requested V4L2 stream mode; the driver readback remains authoritative."""

    name: str
    width: int
    height: int
    fps: int
    pixel_format: str | None = None

    @property
    def label(self) -> str:
        pixel_format = f" · {self.pixel_format}" if self.pixel_format else ""
        return f"{self.width}×{self.height} · {self.fps} FPS{pixel_format}"


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
    """Return tested modes for recognized cameras and a conservative fallback."""
    if candidate is None:
        return GENERIC_V4L2_MODES

    identity = candidate.identity
    usb_id = (identity.vendor_id, identity.product_id)
    if usb_id == ("1415", "2000"):
        return PS3_EYE_MODES
    if usb_id == ("07ca", "313a"):
        return AVERMEDIA_CAM_313_MODES
    return GENERIC_V4L2_MODES
