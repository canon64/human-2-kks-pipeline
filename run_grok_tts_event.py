import sys

from entrypoints.tts_event_entry import run_tts_event

if __name__ == "__main__":
    raise SystemExit(run_tts_event(sys.argv[1:]))
