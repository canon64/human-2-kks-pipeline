from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path

from .tcp_wav_transfer import TcpWavReceiver, TcpWavReceiverConfig


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Receive WAV files over TCP and save them to directory.")
    p.add_argument("--bind-host", default="0.0.0.0", help="Bind host")
    p.add_argument("--bind-port", type=int, default=17890, help="Bind port")
    p.add_argument("--output-dir", required=True, help="Directory to save received WAV")
    p.add_argument("--token", default="", help="Auth token (optional)")
    p.add_argument("--max-file-mb", type=int, default=64, help="Max file size in MB")
    p.add_argument("--timeout", type=float, default=30.0, help="Socket I/O timeout seconds")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    cfg = TcpWavReceiverConfig(
        bind_host=args.bind_host,
        bind_port=int(args.bind_port),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        token=args.token,
        max_file_bytes=max(1, int(args.max_file_mb)) * 1024 * 1024,
        io_timeout_seconds=max(1.0, float(args.timeout)),
    )
    receiver = TcpWavReceiver(cfg, log_callback=print)

    running = {"value": True}

    def _stop(_sig, _frame) -> None:
        running["value"] = False
        receiver.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        receiver.start()
        while running["value"]:
            time.sleep(0.3)
        return 0
    except KeyboardInterrupt:
        receiver.stop()
        return 0
    except Exception as exc:
        print(f"[tcp_recv] fatal: {exc}")
        receiver.stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
