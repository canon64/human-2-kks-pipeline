import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from entrypoints.transcribe_one_entry import run_transcribe_one

if __name__ == "__main__":
    raise SystemExit(run_transcribe_one(sys.argv[1:]))
