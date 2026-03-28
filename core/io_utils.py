from __future__ import annotations

import json
import os
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def with_utf8_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def last_json_line(text: str) -> dict[str, Any]:
    for line in reversed((text or "").splitlines()):
        t = line.strip()
        if not t:
            continue
        try:
            return json.loads(t)
        except Exception:
            continue
    raise RuntimeError("No JSON payload in stdout")


def wav_duration_sec(path: str) -> Optional[float]:
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return None


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = path
    if out.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out = out.with_name(f"{out.stem}_{stamp}{out.suffix}")
    out.write_text(text, encoding="utf-8")

