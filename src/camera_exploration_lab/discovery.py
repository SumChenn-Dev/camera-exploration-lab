"""Read-only V4L2 discovery using stable by-id links and Linux sysfs."""

from __future__ import annotations

import os
import fcntl
import logging
import struct
from pathlib import Path

from .models import BackendKind, CameraCandidate, CameraIdentity

_KNOWN_USB_CAMERAS: dict[tuple[str, str], tuple[str, str]] = {
    ("1415", "2000"): ("Sony", "PlayStation Eye"),
    ("07ca", "313a"): ("AVerMedia", "Live Streamer CAM 313"),
}

logger = logging.getLogger(__name__)

# VIDIOC_QUERYCAP for struct v4l2_capability (104 bytes), built with Linux _IOC.
_VIDIOC_QUERYCAP = (2 << 30) | (104 << 16) | (ord("V") << 8)
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
_V4L2_CAP_DEVICE_CAPS = 0x80000000


def _query_v4l2_capabilities(device_path: Path) -> tuple[int, int] | None:
    """Return (advertised capabilities, effective device capabilities) read-only."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(device_path, flags)
    except OSError as exc:
        logger.debug("Unable to query V4L2 capabilities for a video node: %s", exc)
        return None
    try:
        payload = bytearray(104)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, payload, True)
        advertised, device_caps = struct.unpack_from("=II", payload, 84)
    except OSError as exc:
        logger.debug("V4L2 capability query failed for a video node: %s", exc)
        return None
    finally:
        os.close(fd)
    effective = device_caps if advertised & _V4L2_CAP_DEVICE_CAPS else advertised
    return advertised, effective


def _supports_image_capture(effective_capabilities: int) -> bool:
    return bool(effective_capabilities & (
        _V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_VIDEO_CAPTURE_MPLANE
    ))


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _usb_metadata(device_path: Path) -> dict[str, str | None]:
    current = device_path
    for ancestor in (current, *current.parents):
        vendor_id = _read_text(ancestor / "idVendor")
        product_id = _read_text(ancestor / "idProduct")
        if vendor_id and product_id:
            return {
                "vendor_id": vendor_id.lower(),
                "product_id": product_id.lower(),
                "serial_number": _read_text(ancestor / "serial"),
                "manufacturer": _read_text(ancestor / "manufacturer"),
                "product": _read_text(ancestor / "product"),
                "usb_sysfs_path": str(ancestor),
            }
    return {}


class V4L2Discovery:
    """Enumerate image-capture V4L2 nodes; querycap uses a read-only open."""

    def __init__(
        self,
        *,
        dev_root: str | Path = "/dev",
        sysfs_root: str | Path = "/sys/class/video4linux",
        by_id_root: str | Path = "/dev/v4l/by-id",
    ) -> None:
        self.dev_root = Path(dev_root)
        self.sysfs_root = Path(sysfs_root)
        self.by_id_root = Path(by_id_root)

    def discover(self) -> list[CameraCandidate]:
        if not self.sysfs_root.is_dir():
            return []

        by_node: dict[str, list[Path]] = {}
        if self.by_id_root.is_dir():
            for link in sorted(self.by_id_root.iterdir()):
                try:
                    target_name = Path(os.path.realpath(link)).name
                except OSError:
                    continue
                if target_name.startswith("video"):
                    by_node.setdefault(target_name, []).append(link)

        candidates: list[CameraCandidate] = []
        for entry in sorted(self.sysfs_root.glob("video*")):
            node_name = entry.name
            current_node = self.dev_root / node_name
            capabilities = _query_v4l2_capabilities(current_node)
            if capabilities is None:
                continue
            advertised_caps, effective_caps = capabilities
            if not _supports_image_capture(effective_caps):
                continue
            device_link = entry / "device"
            try:
                sysfs_device = device_link.resolve(strict=False)
            except OSError:
                sysfs_device = entry.resolve(strict=False)
            usb = _usb_metadata(sysfs_device)
            id_links = by_node.get(node_name, [])
            stable_link = id_links[0] if id_links else None
            if stable_link is not None:
                stable_identifier = str(stable_link)
                identity_source = "v4l_by_id"
            elif usb:
                location = usb.get("serial_number") or usb.get("usb_sysfs_path") or str(sysfs_device)
                video_index = _read_text(entry / "index") or node_name
                stable_identifier = (
                    f"v4l2:usb:{usb['vendor_id']}:{usb['product_id']}:{location}:index-{video_index}"
                )
                identity_source = "sysfs_usb_identity"
            else:
                stable_identifier = f"v4l2:sysfs:{sysfs_device}"
                identity_source = "sysfs_path"

            sysfs_name = _read_text(entry / "name") or _read_text(sysfs_device / "name") or node_name
            usb_pair = (str(usb.get("vendor_id", "")), str(usb.get("product_id", "")))
            known_label = _KNOWN_USB_CAMERAS.get(usb_pair)
            manufacturer = known_label[0] if known_label else usb.get("manufacturer")
            model = known_label[1] if known_label else usb.get("product") or sysfs_name
            display_name = " ".join(part for part in (manufacturer, model) if part) or sysfs_name
            identity = CameraIdentity(
                stable_identifier=stable_identifier,
                backend=BackendKind.V4L2,
                device_path=str(current_node),
                manufacturer=manufacturer,
                model=model,
                serial_number=usb.get("serial_number"),
                vendor_id=usb.get("vendor_id"),
                product_id=usb.get("product_id"),
                identity_source=identity_source,
                metadata={
                    "sysfs_path": str(entry),
                    "sysfs_device_path": str(sysfs_device),
                    "by_id_aliases": [str(link) for link in id_links],
                    "video_index": _read_text(entry / "index"),
                    "usb_reported_manufacturer": usb.get("manufacturer"),
                    "usb_reported_product": usb.get("product"),
                    "device_node_is_ephemeral": True,
                    "v4l2_capabilities": advertised_caps,
                    "v4l2_device_caps": effective_caps,
                },
            )
            candidates.append(
                CameraCandidate(
                    identity=identity,
                    display_name=display_name,
                    node_present=current_node.exists(),
                )
            )
        return candidates
