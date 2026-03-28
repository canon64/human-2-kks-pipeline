import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from entrypoints.tts_event_entry import run_tts_event

if __name__ == "__main__":
    raise SystemExit(run_tts_event(sys.argv[1:]))
