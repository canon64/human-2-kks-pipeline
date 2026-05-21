from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from core.sd_prompt_bridge import normalize_endpoint

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ReceiverConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 18768
    endpoint: str = "/sd-prompt"
    token: str = ""
    sd_webui_url: str = "http://127.0.0.1:7860"
    sd_api_path: str = "/sdapi/v1/txt2img"
    sd_timeout_sec: float = 180.0
    save_response_images: bool = True
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "outputs" / "sd_receiver")
    default_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "negative_prompt": "",
            "steps": 20,
            "width": 768,
            "height": 1024,
            "cfg_scale": 7.0,
            "batch_size": 1,
            "n_iter": 1,
            "save_images": True,
        }
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_config(path: Path) -> ReceiverConfig:
    data = _load_json(path)
    default_payload = data.get("default_payload")
    output_dir_text = str(data.get("output_dir", "") or "").strip()
    if output_dir_text:
        output_dir = Path(output_dir_text).expanduser()
        if not output_dir.is_absolute():
            output_dir = path.parent / output_dir
        output_dir = output_dir.resolve()
    else:
        output_dir = PROJECT_ROOT / "outputs" / "sd_receiver"
    return ReceiverConfig(
        listen_host=str(data.get("listen_host", "0.0.0.0") or "0.0.0.0").strip(),
        listen_port=max(1, min(65535, int(data.get("listen_port", 18768) or 18768))),
        endpoint=normalize_endpoint(str(data.get("endpoint", "/sd-prompt") or "/sd-prompt")),
        token=str(data.get("token", "") or "").strip(),
        sd_webui_url=str(data.get("sd_webui_url", "http://127.0.0.1:7860") or "http://127.0.0.1:7860").strip(),
        sd_api_path=normalize_endpoint(str(data.get("sd_api_path", "/sdapi/v1/txt2img") or "/sdapi/v1/txt2img")),
        sd_timeout_sec=max(1.0, float(data.get("sd_timeout_sec", 180.0) or 180.0)),
        save_response_images=bool(data.get("save_response_images", True)),
        output_dir=output_dir,
        default_payload=default_payload if isinstance(default_payload, dict) else ReceiverConfig().default_payload,
    )


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _decode_image(raw_image: str) -> bytes:
    text = str(raw_image or "")
    if "," in text and text[:32].lower().startswith("data:image"):
        text = text.split(",", 1)[1]
    return base64.b64decode(text)


def _save_images(images: list[Any], output_dir: Path) -> list[str]:
    if not images:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved: list[str] = []
    for index, image_text in enumerate(images, start=1):
        try:
            image_bytes = _decode_image(str(image_text or ""))
        except Exception:
            continue
        path = output_dir / f"sd_{stamp}_{index:02d}.png"
        path.write_bytes(image_bytes)
        saved.append(str(path))
    return saved


def _post_to_sd(cfg: ReceiverConfig, prompt: str, request_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cfg.default_payload)
    sd_payload = request_payload.get("sd_payload")
    if isinstance(sd_payload, dict):
        payload.update(sd_payload)
    for key in ("negative_prompt", "steps", "width", "height", "cfg_scale", "sampler_name", "seed"):
        if key in request_payload:
            payload[key] = request_payload[key]
    payload["prompt"] = prompt

    url = cfg.sd_webui_url.rstrip("/") + cfg.sd_api_path
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.sd_timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return {"ok": False, "status": int(exc.code), "error": f"SD HTTP {exc.code}", "body": body[:1000], "url": url}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc), "body": "", "url": url}

    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    images = data.get("images") if isinstance(data, dict) else None
    saved_images: list[str] = []
    if cfg.save_response_images and isinstance(images, list):
        saved_images = _save_images(images, cfg.output_dir)

    return {
        "ok": 200 <= status < 300,
        "status": status,
        "error": "",
        "url": url,
        "elapsed_sec": round(time.time() - started, 3),
        "image_count": len(images) if isinstance(images, list) else 0,
        "saved_images": saved_images,
        "info": data.get("info", "") if isinstance(data, dict) else "",
    }


def run_server(cfg: ReceiverConfig) -> int:
    endpoint = normalize_endpoint(cfg.endpoint)
    token = cfg.token

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                _write_json(self, int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})
                return
            _write_json(
                self,
                int(HTTPStatus.OK),
                {
                    "ok": True,
                    "endpoint": endpoint,
                    "sd_webui_url": cfg.sd_webui_url,
                    "sd_api_path": cfg.sd_api_path,
                },
            )

        def do_POST(self) -> None:
            if self.path != endpoint:
                _write_json(self, int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})
                return
            if token and (self.headers.get("X-Auth-Token") or "").strip() != token:
                _write_json(self, int(HTTPStatus.FORBIDDEN), {"ok": False, "error": "forbidden"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            if length <= 0:
                _write_json(self, int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "empty body"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                _write_json(self, int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "invalid json"})
                return
            if not isinstance(payload, dict):
                _write_json(self, int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "payload must be object"})
                return
            prompt = str(payload.get("prompt", "") or "").strip()
            if not prompt:
                _write_json(self, int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "prompt is empty"})
                return

            result = _post_to_sd(cfg, prompt, payload)
            status = int(HTTPStatus.OK) if result.get("ok") else int(HTTPStatus.BAD_GATEWAY)
            _write_json(self, status, result)

        def log_message(self, fmt: str, *args: Any) -> None:
            print("[sd-prompt-receiver] " + (fmt % args), flush=True)

    server = ThreadingHTTPServer((cfg.listen_host, int(cfg.listen_port)), Handler)
    server.daemon_threads = True
    print(f"[sd-prompt-receiver] listening http://{cfg.listen_host}:{cfg.listen_port}{endpoint}", flush=True)
    print(f"[sd-prompt-receiver] forwarding to {cfg.sd_webui_url.rstrip('/')}{cfg.sd_api_path}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("[sd-prompt-receiver] stopped", flush=True)
    finally:
        server.server_close()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive SD prompts from Human_2_KKS and forward them to Stable Diffusion WebUI.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "sd_prompt_receiver_config.json"), help="Receiver config JSON path.")
    parser.add_argument("--host", default="", help="Override listen_host.")
    parser.add_argument("--port", type=int, default=0, help="Override listen_port.")
    parser.add_argument("--sd-url", default="", help="Override sd_webui_url.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(Path(args.config).expanduser().resolve())
    if args.host.strip():
        cfg.listen_host = args.host.strip()
    if int(args.port or 0) > 0:
        cfg.listen_port = max(1, min(65535, int(args.port)))
    if args.sd_url.strip():
        cfg.sd_webui_url = args.sd_url.strip()
    return run_server(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
