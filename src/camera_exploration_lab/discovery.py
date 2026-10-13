"""Read-only V4L2 discovery using stable by-id links and Linux sysfs."""

from __future__ import annotations

import os
from pathlib import Path

from .models import BackendKind, CameraCandidate, CameraIdentity

_KNOWN_USB_CAMERAS: dict[tuple[str, str], tuple[str, str]] = {
    ("1415", "2000"): ("Sony", "PlayStation Eye"),
    ("07ca", "313a"): ("AVerMedia", "Live Streamer CAM 313"),
}


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
    """Enumerate video nodes without opening them or changing camera controls."""

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
