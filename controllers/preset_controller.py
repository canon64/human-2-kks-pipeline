from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

PresetKind = Literal["transcription", "dictionary"]


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("", "0", "false", "off", "no"):
            return False
        if token in ("1", "true", "on", "yes"):
            return True
    return default


def _as_non_negative_int(value: object, default: int) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return max(0, int(default))


def now_iso8601() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_preset_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def normalize_transcription_entries(entries: object) -> list[dict]:
    rows = entries if isinstance(entries, list) else []
    normalized: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        from_text = str(row.get("from", "")).strip()
        if not from_text:
            continue
        normalized.append(
            {
                "enabled": _as_bool(row.get("enabled", True), True),
                "from": from_text,
                "to_grok": str(row.get("to_grok", row.get("to", ""))).strip(),
                "to_display": str(row.get("to_display", row.get("to", ""))).strip(),
                "display_apply": _as_bool(row.get("display_apply", True), True),
                "order_index": _as_non_negative_int(row.get("order_index"), index),
            }
        )
    normalized.sort(key=lambda entry: int(entry.get("order_index", 0)))
    return normalized


def normalize_dictionary_entries(entries: object) -> list[dict]:
    rows = entries if isinstance(entries, list) else []
    normalized: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        from_text = str(row.get("from", "")).strip()
        if not from_text:
            continue
        normalized.append(
            {
                "enabled": _as_bool(row.get("enabled", True), True),
                "from": from_text,
                "to_sbv2": str(row.get("to_sbv2", row.get("to_grok", row.get("to", "")))).strip(),
                "to_display": str(row.get("to_display", row.get("to", ""))).strip(),
                "display_apply": _as_bool(row.get("display_apply", False), False),
                "order_index": _as_non_negative_int(row.get("order_index"), index),
            }
        )
    normalized.sort(key=lambda entry: int(entry.get("order_index", 0)))
    return normalized


def load_presets(path: Path, *, kind: PresetKind) -> list[dict]:
    data = _read_json(path)
    raw_presets = data.get("presets", [])
    if not isinstance(raw_presets, list):
        return []

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    prefix = "tr" if kind == "transcription" else "dc"

    for row in raw_presets:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        preset_id = str(row.get("id", "")).strip() or new_preset_id(prefix)
        while preset_id in seen_ids:
            preset_id = new_preset_id(prefix)
        seen_ids.add(preset_id)

        created_at = str(row.get("createdAt", "")).strip() or now_iso8601()
        updated_at = str(row.get("updatedAt", "")).strip() or created_at
        entries = row.get("entries", [])
        normalized_entries = (
            normalize_transcription_entries(entries)
            if kind == "transcription"
            else normalize_dictionary_entries(entries)
        )
        normalized.append(
            {
                "id": preset_id,
                "name": name,
                "createdAt": created_at,
                "updatedAt": updated_at,
                "entries": normalized_entries,
            }
        )
    return normalized


def save_presets(path: Path, presets: list[dict]) -> None:
    payload = {
        "version": 1,
        "presets": presets,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_active_presets(path: Path) -> dict[str, str]:
    data = _read_json(path)
    active = data.get("active", {})
    if not isinstance(active, dict):
        active = {}
    return {
        "transcriptionPresetId": str(active.get("transcriptionPresetId", "")).strip(),
        "dictionaryPresetId": str(active.get("dictionaryPresetId", "")).strip(),
    }


def save_active_presets(
    path: Path,
    *,
    transcription_preset_id: str,
    dictionary_preset_id: str,
) -> None:
    payload = {
        "version": 1,
        "active": {
            "transcriptionPresetId": str(transcription_preset_id or "").strip(),
            "dictionaryPresetId": str(dictionary_preset_id or "").strip(),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
