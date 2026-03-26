import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from grok_bridge.transcribe_server import main

if __name__ == "__main__":
    raise SystemExit(main())
