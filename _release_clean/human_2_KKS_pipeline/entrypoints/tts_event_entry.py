from __future__ import annotations

import sys
from typing import Optional

from grok_bridge.tts_event_cli import main as tts_event_main


def _as_exit_code(ret) -> int:
    return 0 if ret is None else int(ret)


def run_tts_event(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        return _as_exit_code(tts_event_main())
    prev = sys.argv
    try:
        sys.argv = [prev[0], *argv]
        return _as_exit_code(tts_event_main())
    finally:
        sys.argv = prev
