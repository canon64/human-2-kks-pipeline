"""
永続転写サーバー
起動時に WhisperModel を1回だけロードし、HTTP で転写リクエストを受け付ける。
POST /transcribe  {"audio": "path", "language": "ja", "beam_size": 1}
GET  /health
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .io_utf8 import force_stdio_utf8


def _make_result(ok: bool, text: str = "", duration: float = 0.0, error: str = "") -> dict[str, Any]:
    return {"ok": ok, "text": text, "duration": duration, "error": error}


def _build_handler(model, default_language: str, default_beam: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "not found"}, code=404)

        def do_POST(self):
            if self.path != "/transcribe":
                self._send_json({"ok": False, "error": "not found"}, code=404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
            except Exception as exc:
                self._send_json(_make_result(False, error=f"bad request: {exc}"))
                return

            audio = body.get("audio", "").strip()
            if not audio:
                self._send_json(_make_result(False, error="'audio' is required"))
                return

            language = body.get("language", default_language).strip() or None
            beam_size = max(1, int(body.get("beam_size", default_beam)))

            try:
                segments, info = model.transcribe(
                    audio,
                    beam_size=beam_size,
                    language=language,
                    condition_on_previous_text=False,
                )
                text = "".join(s.text for s in segments).strip()
                self._send_json(_make_result(True, text=text, duration=float(info.duration)))
            except Exception as exc:
                self._send_json(_make_result(False, error=str(exc)))

        def _send_json(self, data: dict, code: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # アクセスログ抑制

    return Handler


def main() -> int:
    force_stdio_utf8()
    parser = argparse.ArgumentParser(description="Persistent faster-whisper transcription server.")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18760)
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        print(f"[server] ERROR: faster-whisper not found: {exc}", flush=True)
        return 1

    print(f"[server] Loading {args.model} device={args.device} compute={args.compute_type} ...", flush=True)
    try:
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    except Exception as exc:
        print(f"[server] ERROR: model load failed: {exc}", flush=True)
        return 1

    print(f"[server] Ready on {args.host}:{args.port}", flush=True)

    handler = _build_handler(model, args.language, args.beam_size)
    server = HTTPServer((args.host, args.port), handler)
    # スレッドプール: 転写は重いので1リクエストずつ直列処理（デフォルト）
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
