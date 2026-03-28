from __future__ import annotations

from typing import Callable, Optional

from entrypoints.gui_entry import run_gui_default
from entrypoints.transcribe_one_entry import run_transcribe_one
from entrypoints.transcribe_server_entry import run_transcribe_server
from entrypoints.tts_event_entry import run_tts_event


def _usage() -> str:
    return (
        "Usage: main.py [command] [args...]\n"
        "Commands:\n"
        "  gui                Start GUI (default)\n"
        "  transcribe-server  Start FasterWhisper HTTP server\n"
        "  transcribe-one     Transcribe one wav\n"
        "  tts-event          Run Grok+TTS event pipeline\n"
    )


def run(argv: Optional[list[str]] = None) -> int:
    args = list(argv or [])
    if not args:
        return run_gui_default()

    command = args[0].strip().lower()
    sub_args = args[1:]

    routes: dict[str, Callable[[Optional[list[str]]], int]] = {
        "gui": lambda a: run_gui_default(["main.py", *(a or [])]),
        "transcribe-server": run_transcribe_server,
        "transcribe-one": run_transcribe_one,
        "tts-event": run_tts_event,
    }

    fn = routes.get(command)
    if fn is None:
        print(_usage())
        return 2
    return int(fn(sub_args))

