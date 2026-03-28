import sys

from entrypoints.transcribe_one_entry import run_transcribe_one

if __name__ == "__main__":
    raise SystemExit(run_transcribe_one(sys.argv[1:]))
