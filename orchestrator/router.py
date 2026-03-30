from __future__ import annotations

from typing import Optional


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
        from entrypoints.gui_entry import run_gui_default
        return run_gui_default()

    command = args[0].strip().lower()
    sub_args = args[1:]

    if command == "gui":
        from entrypoints.gui_entry import run_gui_default
        return run_gui_default(["main.py", *(sub_args or [])])
    elif command == "transcribe-server":
        from entrypoints.transcribe_server_entry import run_transcribe_server
        return int(run_transcribe_server(sub_args))
    elif command == "transcribe-one":
        from entrypoints.transcribe_one_entry import run_transcribe_one
        return int(run_transcribe_one(sub_args))
    elif command == "tts-event":
        from entrypoints.tts_event_entry import run_tts_event
        return int(run_tts_event(sub_args))
    else:
        print(_usage())
        return 2

