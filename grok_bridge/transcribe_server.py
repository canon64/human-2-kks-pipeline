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


def _log_env_info() -> None:
    """起動時環境情報をログ出力する"""
    import sys
    import os
    print(f"[server] Python: {sys.executable}", flush=True)
    print(f"[server] Python version: {sys.version}", flush=True)

    # PATH内のcuda_dllsフォルダを表示
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    cuda_paths = [p for p in path_dirs if "cuda" in p.lower() or "cudnn" in p.lower() or "cublas" in p.lower()]
    if cuda_paths:
        print(f"[server] CUDA-related PATH entries:", flush=True)
        for p in cuda_paths:
            print(f"[server]   {p}", flush=True)
    else:
        print(f"[server] No CUDA-related PATH entries found", flush=True)

    # cuda_dlls フォルダの存在確認
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        os.path.join(script_dir, "..", "python", "cuda_dlls"),
        os.path.join(script_dir, "..", "..", "python", "cuda_dlls"),
    ]:
        candidate = os.path.normpath(candidate)
        if os.path.isdir(candidate):
            dlls = [f for f in os.listdir(candidate) if f.endswith(".dll")]
            print(f"[server] cuda_dlls found: {candidate}", flush=True)
            for dll in sorted(dlls):
                print(f"[server]   {dll}", flush=True)
            break
    else:
        print(f"[server] cuda_dlls folder not found (GPU may fail without CUDA Toolkit)", flush=True)

    # ctranslate2 バージョン
    try:
        import ctranslate2
        print(f"[server] ctranslate2: {ctranslate2.__version__}", flush=True)
        print(f"[server] ctranslate2 CUDA support: {ctranslate2.get_cuda_device_count()} device(s)", flush=True)
    except Exception as e:
        print(f"[server] ctranslate2 info failed: {e}", flush=True)

    # faster-whisper バージョン
    try:
        import faster_whisper
        print(f"[server] faster-whisper: {faster_whisper.__version__}", flush=True)
    except Exception as e:
        print(f"[server] faster-whisper version check failed: {e}", flush=True)


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

    _log_env_info()

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        print(f"[server] ERROR: faster-whisper not found: {exc}", flush=True)
        return 1

    print(f"[server] Loading {args.model} device={args.device} compute={args.compute_type} ...", flush=True)
    try:
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    except Exception as exc:
        print(f"[server] ERROR detail: {exc}", flush=True)
        cuda_hints = ("cublas", "cuda", "cudnn", "dll is not found", "cannot be loaded")
        if any(h in str(exc).lower() for h in cuda_hints) and args.device != "cpu":
            print(f"[server] WARN: GPU load failed. Retrying with CPU+int8 ...", flush=True)
            try:
                model = WhisperModel(args.model, device="cpu", compute_type="int8")
                print("[server] Loaded on CPU (int8). Transcription will be slower.", flush=True)
            except Exception as exc2:
                print(f"[server] ERROR: CPU fallback also failed: {exc2}", flush=True)
                return 1
        else:
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
