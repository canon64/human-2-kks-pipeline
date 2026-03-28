import sys

from entrypoints.transcribe_server_entry import run_transcribe_server

if __name__ == "__main__":
    raise SystemExit(run_transcribe_server(sys.argv[1:]))
