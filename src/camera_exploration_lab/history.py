"""Searchable, provenance-preserving rows for the local History workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .identity import camera_family_key, camera_family_label, identity_fingerprint, identity_source_label


@dataclass(frozen=True, slots=True)
class HistoryRow:
    session_id: str
    benchmark_id: str | None
    cells: tuple[str, ...]
    sort_values: tuple[Any, ...]
    searchable_text: str
    camera_group_key: str
    camera_group_label: str
    camera_identity_key: str
    source_value: str

    @property
    def selection(self) -> tuple[str, str | None]:
        return self.session_id, self.benchmark_id


def make_history_rows(entries: Iterable[dict[str, Any]]) -> list[HistoryRow]:
    rows: list[HistoryRow] = []
    for entry in entries:
        source = str(entry.get("source") or "unknown")
        source_label = _source_label(source)
        camera = str(entry.get("model") or entry.get("manufacturer") or "Identity unavailable")
        manufacturer = str(entry.get("manufacturer") or "")
        model = str(entry.get("model") or "")
        stable_id = str(entry.get("stable_identifier") or "")
        camera_id = str(entry.get("camera_id") or "")
        identity_source = str(entry.get("identity_source") or "")
        identity_label = identity_source_label(identity_source)
        identity_key = stable_id or camera_id or f"unavailable:{camera.casefold()}"
        identity_token = identity_fingerprint(identity_key)
        camera_cell = (
            f"{camera} · {identity_label} · {identity_token}"
            if stable_id or camera_id
            else f"{camera} · identity unavailable"
        )

        label = str(entry.get("session_label") or "Unnamed session")
        benchmark_label = entry.get("benchmark_label")
        if benchmark_label and label != benchmark_label:
            label += f" · {benchmark_label}"
        status = str(entry.get("status") or ("imported" if source == "imported" else "session"))
        started = str(
            entry.get("benchmark_started_at")
            or entry.get("session_started_at")
            or ""
        )
        capture_fps = _optional_float(entry.get("capture_fps"))
        preview_fps = _optional_float(entry.get("preview_fps"))
        cells = (
            label,
            camera_cell,
            source_label,
            status,
            started or "—",
            f"{capture_fps:.1f}" if capture_fps is not None else "—",
            f"{preview_fps:.1f}" if preview_fps is not None else "—",
        )
        family_key = camera_family_key(
            str(entry["vendor_id"]) if entry.get("vendor_id") is not None else None,
            str(entry["product_id"]) if entry.get("product_id") is not None else None,
            manufacturer or None,
            model or None,
            identity_key,
        )
        family_label = camera_family_label(manufacturer or None, model or None)
        configuration = _json_object(entry.get("configuration_json"))
        searchable = " ".join(
            (
                label,
                camera,
                manufacturer,
                source,
                source_label,
                identity_label,
                str(entry.get("notes") or ""),
                str(entry.get("source_basename") or ""),
                status,
                started,
                _searchable_value(configuration),
                str(entry.get("setting_search_text") or ""),
            )
        ).casefold()
        rows.append(HistoryRow(
            session_id=str(entry.get("session_id") or ""),
            benchmark_id=str(entry["benchmark_id"]) if entry.get("benchmark_id") else None,
            cells=cells,
            sort_values=(
                label.casefold(), camera_cell.casefold(), source_label.casefold(),
                status.casefold(), started, capture_fps if capture_fps is not None else -1.0,
                preview_fps if preview_fps is not None else -1.0,
            ),
            searchable_text=searchable,
            camera_group_key=family_key,
            camera_group_label=family_label,
            camera_identity_key=identity_key,
            source_value=source,
        ))
    return rows


def filter_history_rows(
    rows: Iterable[HistoryRow],
    *,
    query: str = "",
    camera_group_key: str | None = None,
    source: str | None = None,
) -> list[HistoryRow]:
    needle = query.strip().casefold()
    return [
        row for row in rows
        if (not needle or needle in row.searchable_text)
        and (not camera_group_key or row.camera_group_key == camera_group_key)
        and (not source or row.source_value == source)
    ]


def _source_label(source: str) -> str:
    return {
        "imported": "Imported CSV",
        "live_experiment": "Live experiment",
    }.get(source, source)


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _searchable_value(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(
            f"{key} {_searchable_value(item)}"
            for key, item in value.items()
            if key.casefold() not in {"stable_identifier", "serial_number", "device_path", "source_path"}
        )
    if isinstance(value, (list, tuple)):
        return " ".join(_searchable_value(item) for item in value)
    if value is None:
        return ""
    return str(value)
