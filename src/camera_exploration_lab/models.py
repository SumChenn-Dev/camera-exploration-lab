"""Shared data types for camera identity, sessions, and measurements."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


class BackendKind(StrEnum):
    V4L2 = "v4l2"
    CHDKPTP = "chdkptp"
    UNKNOWN = "unknown"


class StreamScope(StrEnum):
    CAPTURE = "capture"
    PREVIEW = "preview"
    PROCESSING = "processing"
    TRANSPORT = "transport"
    CAMERA_REPORTED = "camera_reported"
    UNKNOWN = "unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return str(uuid4())


@dataclass(slots=True)
class CameraIdentity:
    """Identity for a discovered device or an explicitly inferred legacy camera.

    ``stable_identifier`` is the durable key. For V4L2 this should be a
    ``/dev/v4l/by-id`` path when available; ``device_path`` is only the current
    node used to open the device and may change after reconnecting.
    """

    stable_identifier: str
    backend: BackendKind | str
    device_path: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    identity_source: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CameraCandidate:
    identity: CameraIdentity
    display_name: str
    node_present: bool = True


@dataclass(slots=True)
class SessionRecord:
    camera_id: str | None
    label: str
    started_at: str | None = None
    ended_at: str | None = None
    source: str = "live"
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class BenchmarkRecord:
    session_id: str
    label: str
    started_at: str | None = None
    ended_at: str | None = None
    status: str = "running"
    configuration: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)


@dataclass(slots=True)
class SettingSnapshot:
    session_id: str
    setting_key: str
    requested_value: Any = None
    reported_value: Any = None
    verified_value: Any = None
    verification_status: str = "not_observed"
    requested_at: str | None = None
    checked_at: str | None = None
    verification_method: str | None = None
    benchmark_id: str | None = None
    raw_record_id: int | None = None
    id: str = field(default_factory=new_id)


@dataclass(slots=True)
class MeasurementSample:
    benchmark_id: str
    metric: str
    value: float
    unit: str
    stream_scope: StreamScope | str = StreamScope.UNKNOWN
    observed_at: str | None = None
    elapsed_seconds: float | None = None
    sample_index: int | None = None
    interval_ns: int | None = None
    method: str | None = None
    raw_record_id: int | None = None
    id: str = field(default_factory=new_id)


@dataclass(slots=True, frozen=True)
class ImportResult:
    source_path: str
    source_id: str | None
    session_id: str | None
    row_count: int
    measurement_count: int
    already_imported: bool = False
