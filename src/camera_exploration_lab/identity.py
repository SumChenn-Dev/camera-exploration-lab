"""Camera identity grouping helpers that never rewrite stored identities."""

from __future__ import annotations

import hashlib
import re


def camera_family_key(
    vendor_id: str | None,
    product_id: str | None,
    manufacturer: str | None,
    model: str | None,
    identity_fallback: str | None = None,
) -> str:
    """Build a model-family key for browsing, not a physical-unit identity."""
    vendor = _normalize_usb_id(vendor_id)
    product = _normalize_usb_id(product_id)
    if vendor and product:
        return f"usb-family:{vendor}:{product}"
    name = " ".join(part for part in (manufacturer, model) if part).casefold()
    normalized_name = " ".join(re.sub(r"[^a-z0-9]+", " ", name).split())
    if normalized_name:
        return f"model-family:{normalized_name}"
    return f"camera-identity:{identity_fallback}" if identity_fallback else "camera-family:unknown"


def camera_family_label(manufacturer: str | None, model: str | None) -> str:
    return " ".join(part.strip() for part in (manufacturer, model) if part and part.strip()) or "Camera identity unavailable"


def identity_source_label(source: str | None) -> str:
    labels = {
        "inferred_from_legacy_path": "Legacy CSV inferred",
        "v4l_by_id": "V4L2 by-id",
        "sysfs_usb_identity": "V4L2 USB topology",
        "sysfs_path": "V4L2 sysfs fallback",
    }
    return labels.get(source or "", "Camera identity unavailable")


def identity_fingerprint(stable_identifier: str | None) -> str:
    """Return a short local display token without revealing a serial or path."""
    if not stable_identifier:
        return "unknown"
    return hashlib.sha256(stable_identifier.encode("utf-8")).hexdigest()[:8].upper()


def _normalize_usb_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    return normalized if normalized else None
