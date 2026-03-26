from __future__ import annotations

import argparse
import json
from pathlib import Path

from .io_utf8 import force_stdio_utf8
from .tcp_wav_transfer import send_wav_file


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Send one WAV file to TCP receiver and print JSON response.")
    p.add_argument("--host", required=True, help="Receiver host")
    p.add_argument("--port", type=int, default=17890, help="Receiver port")
    p.add_argument("--wav", required=True, help="WAV file path")
    p.add_argument("--token", default="", help="Auth token")
    p.add_argument("--timeout", type=float, default=20.0, help="Socket timeout seconds")
    return p


def main() -> int:
    force_stdio_utf8()
    args = _build_parser().parse_args()
    try:
        response = send_wav_file(
            host=args.host,
            port=int(args.port),
            wav_path=Path(args.wav),
            token=args.token,
            timeout_seconds=float(args.timeout),
        )
        print(json.dumps({"ok": True, "error": "", "response": response}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "response": {}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
