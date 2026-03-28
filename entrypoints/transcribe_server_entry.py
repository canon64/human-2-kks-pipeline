from __future__ import annotations

import sys
from typing import Optional

from grok_bridge.transcribe_server import main as transcribe_server_main


def _as_exit_code(ret) -> int:
    return 0 if ret is None else int(ret)


def run_transcribe_server(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        return _as_exit_code(transcribe_server_main())
    prev = sys.argv
    try:
        sys.argv = [prev[0], *argv]
        return _as_exit_code(transcribe_server_main())
    finally:
        sys.argv = prev
