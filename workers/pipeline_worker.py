from __future__ import annotations

import json
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from config.constants import DEFAULT_SOURCE_MODE
from config.models import AppConfig
from core.io_utils import (
    last_json_line as _last_json_line,
    wav_duration_sec as _wav_duration_sec,
    with_utf8_env as _with_utf8_env,
)
from core.sd_prompt_bridge import append_sd_prompt_instruction

PROJECT_ROOT = Path(__file__).resolve().parent.parent

class PipelineWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self._running = True
        self._paused = False
        self._observer = None
        self._wav_queue: queue.Queue[Path] = queue.Queue(maxsize=1024)
        self._text_queue: queue.Queue[str] = queue.Queue(maxsize=256)
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._transcribe_conv_lock = threading.Lock()
        self._transcribe_conv_rules: list[dict] = list(cfg.transcribe_conversion_dict or [])
        self._transcribe_proc: Optional[subprocess.Popen] = None
        self._sbv2_proc: Optional[subprocess.Popen] = None
        self._sbv2_log_tail: deque[str] = deque(maxlen=200)
        self._sbv2_log_lock = threading.Lock()
        self._sbv2_last_exit_code: Optional[int] = None
        self._sbv2_last_exit_at: str = ""
        self._current_proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._external_server: Optional[ThreadingHTTPServer] = None
        self._external_server_thread: Optional[threading.Thread] = None
        self._external_id_queue: deque[str] = deque()
        self._external_id_set: set[str] = set()
        self._external_lock = threading.Lock()
        # Generate forever (SD) state
        self._sd_forever_running = False
        self._sd_forever_thread: Optional[threading.Thread] = None
        self._current_sd_prompt: str = ""
        self._sd_prompt_lock = threading.Lock()
        self._sd_iteration: int = 0
        self._sd_control_server: Optional[ThreadingHTTPServer] = None
        self._sd_control_thread: Optional[threading.Thread] = None
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        # song_kana_map / video_metadata
        _data_dir = PROJECT_ROOT / "data"
        self._song_kana_map_path: Path = _data_dir / "song_kana_map.json"
        self._song_kana_map: list[dict] = self._load_json_safe(self._song_kana_map_path)
        self._video_metadata: list[dict] = self._load_json_safe(self._cfg.video_metadata_path)
        self._song_kana_lock = threading.Lock()
        self._title_to_indices: dict[str, list[int]] = self._build_title_to_indices()
        self._sorted_titles: list[str] = sorted(self._title_to_indices.keys(), key=len, reverse=True)
        # カナ変換用: (原文キー小文字, カナ値, エントリインデックス, kind) を長さ降順でソート
        self._kana_rules: list[tuple[str, str, int, str]] = self._build_kana_rules()

    @staticmethod
    def _load_json_safe(path: Optional[Path]) -> list:
        if path is None:
            return []
        try:
            if (not path.exists()) or (not path.is_file()):
                return []
        except Exception:
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def _entry_enabled(entry: dict, default: bool = True) -> bool:
        value = entry.get("enabled", default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            token = value.strip().lower()
            if token in ("", "0", "false", "off", "no"):
                return False
            if token in ("1", "true", "on", "yes"):
                return True
        return default

    def _build_kana_rules(self) -> list[tuple[str, str, int, str, str]]:
        # song_kana_map の title→index テーブル（play_count追跡用）
        title_to_idx: dict[str, int] = {}
        for i, entry in enumerate(self._song_kana_map):
            t = entry.get("title", "").lower()
            if t and t not in title_to_idx:
                title_to_idx[t] = i

        # カナはvideo_metadata.jsonから取得（song_kana_mapにはカナフィールドなし）
        # tuple: (key_lower, kana_value, idx, kind, original_text)
        rules = []
        seen: set[str] = set()
        for entry in self._video_metadata:
            artist = entry.get("artist", "")
            artist_kana = entry.get("artist_kana", "")
            title = entry.get("title", "")
            title_kana = entry.get("title_kana", "")
            title_lower = title.lower()
            idx = title_to_idx.get(title_lower, -1)
            if artist and artist_kana and artist.lower() not in seen:
                seen.add(artist.lower())
                rules.append((artist.lower(), artist_kana, idx, "artist", artist))
            if title and title_kana and title_lower not in seen:
                seen.add(title_lower)
                rules.append((title_lower, title_kana, idx, "title", title))
        # 長い文字列を優先してマッチ
        rules.sort(key=lambda r: len(r[0]), reverse=True)
        return rules

    def _build_title_to_indices(self) -> dict[str, list[int]]:
        title_to_indices: dict[str, list[int]] = {}
        for i, entry in enumerate(self._song_kana_map):
            title = str(entry.get("title", "")).strip().lower()
            if not title:
                continue
            title_to_indices.setdefault(title, []).append(i)
        return title_to_indices

    def _append_sbv2_log_tail(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._sbv2_log_lock:
            self._sbv2_log_tail.append(f"{stamp} {line}")

    def _emit_sbv2_diagnostics(self, reason: str) -> None:
        proc = self._sbv2_proc
        url = self._sbv2_server_url() or "(empty)"
        mode = self._sbv2_effective_mode_label()
        if proc is None:
            self.log.emit(f"[sbv2-diag] reason={reason} mode={mode} proc=none url={url}")
        else:
            rc = proc.poll()
            state = f"running(pid={proc.pid})" if rc is None else f"exited(pid={proc.pid}, rc={rc})"
            self.log.emit(f"[sbv2-diag] reason={reason} mode={mode} proc={state} url={url}")
        if self._sbv2_last_exit_code is not None:
            self.log.emit(
                f"[sbv2-diag] last_exit code={self._sbv2_last_exit_code} at={self._sbv2_last_exit_at or '(unknown)'}"
            )
        with self._sbv2_log_lock:
            tail = list(self._sbv2_log_tail)[-30:]
        if tail:
            self.log.emit(f"[sbv2-diag] tail_lines={len(tail)}")
            for ln in tail:
                self.log.emit(f"[sbv2-diag] {ln}")

    @staticmethod
    def _strip_video_payload_prefix(text: str) -> str:
        # タイトル前に付きやすい装飾記号だけを除去する
        return (text or "").strip().lstrip(" 　「『\"'（([【<♡♥❤💗💖💓…。.、,:：;；!-")

    def stop(self) -> None:
        self._running = False
        self._stop_sd_forever(interrupt_current=True)
        self._stop_sd_control_server()
        if self._observer is not None:
            self._observer.stop()
        self._stop_external_text_server()
        if self._transcribe_proc is not None:
            try:
                self._transcribe_proc.terminate()
            except Exception:
                pass
            self._transcribe_proc = None
        if self._sbv2_proc is not None:
            try:
                self._sbv2_proc.terminate()
            except Exception:
                pass
            self._sbv2_proc = None
        with self._proc_lock:
            if self._current_proc is not None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass

    def pause(self) -> None:
        self._paused = True
        # 保留中のWAVを全て破棄
        drained = 0
        while not self._wav_queue.empty():
            try:
                self._wav_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        try:
            self.log.emit(f"[pause] WAVキュー破棄: {drained}件")
        except RuntimeError:
            pass

    def resume(self) -> None:
        self._paused = False
        try:
            self.log.emit("[resume] 再開")
        except RuntimeError:
            pass

    def send_text(self, text: str) -> None:
        try:
            self._text_queue.put_nowait(text)
        except queue.Full:
            self.log.emit("[warn] テキストキューが満杯")

    def _source_mode(self) -> str:
        mode = (self._cfg.source_mode or DEFAULT_SOURCE_MODE).strip().lower()
        if mode not in ("external", "mic", "both"):
            return DEFAULT_SOURCE_MODE
        return mode

    def _sbv2_mode(self) -> str:
        mode = str(getattr(self._cfg, "sbv2_mode", "auto") or "auto").strip().lower()
        if mode in ("auto", "http", "local"):
            return mode
        return "auto"

    def _sbv2_server_url(self) -> str:
        return str(getattr(self._cfg, "sbv2_server_url", "") or "").strip()

    def _sbv2_use_http(self) -> bool:
        mode = self._sbv2_mode()
        if mode == "http":
            return True
        if mode == "local":
            return False
        return bool(self._sbv2_server_url())

    def _sbv2_effective_mode_label(self) -> str:
        if self._sbv2_mode() == "auto":
            return "http" if self._sbv2_use_http() else "local"
        return self._sbv2_mode()

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        value = (endpoint or "").strip()
        if not value:
            return "/manual-text"
        if not value.startswith("/"):
            return "/" + value
        return value

    def _register_external_event_id(self, event_id: str) -> bool:
        if not event_id:
            return True

        key = event_id.strip()
        if not key:
            return True

        max_ids = max(10, int(self._cfg.external_text_dedupe_max))
        with self._external_lock:
            if key in self._external_id_set:
                return False
            self._external_id_set.add(key)
            self._external_id_queue.append(key)
            while len(self._external_id_queue) > max_ids:
                old = self._external_id_queue.popleft()
                self._external_id_set.discard(old)
        return True

    def _accept_external_text(self, text: str, event_id: str, source: str) -> tuple[bool, str, int]:
        mode = self._source_mode()
        if mode == "mic":
            return False, "source_mode=mic", int(HTTPStatus.CONFLICT)

        normalized = (text or "").strip()
        if not normalized:
            return False, "text is empty", int(HTTPStatus.BAD_REQUEST)

        if not self._register_external_event_id(event_id):
            return False, "duplicate event_id", int(HTTPStatus.CONFLICT)

        try:
            self._text_queue.put_nowait(normalized)
        except queue.Full:
            return False, "text queue is full", int(HTTPStatus.SERVICE_UNAVAILABLE)

        src = (source or "external").strip() or "external"
        self.log.emit(f"[external] queued source={src} text={normalized[:80]}")
        return True, "", int(HTTPStatus.OK)

    def _start_external_text_server(self) -> None:
        if not self._cfg.external_text_enabled:
            self.log.emit("[external] disabled")
            return

        mode = self._source_mode()
        if mode == "mic":
            self.log.emit("[external] source_mode=mic のため受信サーバーを起動しません")
            return

        endpoint = self._normalize_endpoint(self._cfg.external_text_endpoint)
        host = (self._cfg.external_text_host or "127.0.0.1").strip() or "127.0.0.1"
        port = max(1, min(65535, int(self._cfg.external_text_port)))
        token = (self._cfg.external_text_token or "").strip()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path != "/health":
                    self._write_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})
                    return
                self._write_json(
                    int(HTTPStatus.OK),
                    {"ok": True, "mode": owner._source_mode(), "endpoint": endpoint},
                )

            def do_POST(self) -> None:
                if self.path != endpoint:
                    self._write_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})
                    return

                if token:
                    req_token = (self.headers.get("X-Auth-Token") or "").strip()
                    if req_token != token:
                        self._write_json(int(HTTPStatus.FORBIDDEN), {"ok": False, "error": "forbidden"})
                        return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except Exception:
                    length = 0
                if length <= 0:
                    self._write_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "empty body"})
                    return

                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._write_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "invalid json"})
                    return

                if not isinstance(payload, dict):
                    self._write_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "error": "payload must be object"})
                    return

                text = str(payload.get("text", "")).strip()
                event_id = str(payload.get("event_id", "")).strip()
                source = str(payload.get("source", "external")).strip()

                ok, reason, status = owner._accept_external_text(text, event_id, source)
                if ok:
                    self._write_json(status, {"ok": True, "queued": True})
                else:
                    self._write_json(status, {"ok": False, "error": reason})

            def log_message(self, fmt: str, *args: Any) -> None:
                owner.log.emit("[external-http] " + (fmt % args))

        try:
            server = ThreadingHTTPServer((host, port), Handler)
        except Exception as exc:
            self.log.emit(f"[external] 起動失敗: {exc}")
            return

        server.daemon_threads = True
        server.timeout = 0.5
        self._external_server = server
        self._external_server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        self._external_server_thread.start()
        self.log.emit(f"[external] listening http://{host}:{port}{endpoint}")

    def _stop_external_text_server(self) -> None:
        if self._external_server is not None:
            try:
                self._external_server.shutdown()
            except Exception:
                pass
            try:
                self._external_server.server_close()
            except Exception:
                pass
            self._external_server = None

    # ---------------------------------------------------------------- SD Generate forever
    def _set_current_sd_prompt(self, prompt: str) -> None:
        text = str(prompt or "").strip()
        with self._sd_prompt_lock:
            self._current_sd_prompt = text
            self._sd_iteration += 1
        if text:
            self.log.emit(f"[sd-forever] prompt updated len={len(text)} iter={self._sd_iteration}")

    def _start_sd_forever_loop(self) -> None:
        if not getattr(self._cfg, "sd_prompt_generate_forever", False):
            self.log.emit("[sd-forever] disabled")
            return
        if self._sd_forever_thread is not None and self._sd_forever_thread.is_alive():
            return
        self._sd_forever_running = True
        self._sd_forever_thread = threading.Thread(
            target=self._sd_forever_worker, daemon=True, name="sd-forever"
        )
        self._sd_forever_thread.start()
        self.log.emit("[sd-forever] loop started")

    def _stop_sd_forever(self, interrupt_current: bool = True) -> None:
        self._sd_forever_running = False
        if interrupt_current:
            try:
                from core.sd_prompt_bridge import post_a1111_interrupt
                res = post_a1111_interrupt(
                    host=str(self._cfg.sd_prompt_target_host or ""),
                    port=int(self._cfg.sd_prompt_target_port or 7860),
                    token=str(self._cfg.sd_prompt_token or ""),
                    timeout_sec=3.0,
                )
                self.log.emit(f"[sd-forever] stop interrupt ok={int(res.ok)} status={res.status} err={res.error[:120]}")
            except Exception as exc:
                self.log.emit(f"[sd-forever] stop interrupt error: {exc}")
        with self._sd_prompt_lock:
            self._current_sd_prompt = ""
        self.log.emit("[sd-forever] loop stopped")

    def _fetch_blankmap_slideshow_status(self) -> dict:
        host = str(getattr(self._cfg, "sd_blankmap_status_host", "127.0.0.1") or "127.0.0.1").strip()
        port = max(1, min(65535, int(getattr(self._cfg, "sd_blankmap_status_port", 55782) or 55782)))
        endpoint = str(getattr(self._cfg, "sd_blankmap_status_endpoint", "/slideshow/status") or "/slideshow/status").strip()
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        if host.startswith("http://") or host.startswith("https://"):
            url = host.rstrip("/") + endpoint
        else:
            url = f"http://{host}:{port}{endpoint}"
        timeout = max(0.2, float(getattr(self._cfg, "sd_blankmap_status_timeout_sec", 1.0) or 1.0))
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status_code = int(getattr(resp, "status", 200))
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("status response is not a JSON object")
        except Exception as exc:
            return {"ok": False, "url": url, "error": str(exc)}

        def pick(*keys: str, default=None):
            for key in keys:
                if key in data:
                    return data.get(key)
            return default

        return {
            "ok": bool(pick("ok", "Ok", default=(200 <= status_code < 300))),
            "url": url,
            "enabled": bool(pick("enabled", "Enabled", default=False)),
            "play_mode": str(pick("play_mode", "PlayMode", default="") or ""),
            "folder": str(pick("folder", "Folder", default="") or ""),
            "current_path": str(pick("current_path", "CurrentPath", default="") or ""),
            "pending_path": str(pick("pending_path", "PendingPath", default="") or ""),
            "transition_path": str(pick("transition_path", "TransitionPath", default="") or ""),
            "transition_active": bool(pick("transition_active", "TransitionActive", default=False)),
            "count": int(pick("count", "Count", default=0) or 0),
            "seconds": float(pick("seconds", "Seconds", default=0.0) or 0.0),
            "scan_interval_sec": float(pick("scan_interval_sec", "ScanIntervalSec", default=1.0) or 1.0),
            "next_slide_in_sec": float(pick("next_slide_in_sec", "NextSlideInSec", default=0.0) or 0.0),
        }

    def _post_blankmap_slideshow_show_latest(self) -> bool:
        """新プロンプトの絵を即表示させるため、ゲームへ『今すぐ最新へ飛べ』命令を送る。"""
        host = str(getattr(self._cfg, "sd_blankmap_status_host", "127.0.0.1") or "127.0.0.1").strip()
        port = max(1, min(65535, int(getattr(self._cfg, "sd_blankmap_status_port", 55782) or 55782)))
        if host.startswith("http://") or host.startswith("https://"):
            url = host.rstrip("/") + "/slideshow/show-latest"
        else:
            url = f"http://{host}:{port}/slideshow/show-latest"
        timeout = max(0.2, float(getattr(self._cfg, "sd_blankmap_status_timeout_sec", 1.0) or 1.0))
        try:
            req = urllib.request.Request(url, data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                int(getattr(resp, "status", 200))
            return True
        except Exception as exc:
            self.log.emit(f"[sd-forever] show-latest failed url={url} err={str(exc)[:120]}")
            return False

    def _sd_forever_worker(self) -> None:
        from core.sd_prompt_bridge import send_a1111_txt2img, post_a1111_interrupt

        last_send_started_at: float = 0.0
        last_prompt_iter = -1
        waiting_for_slideshow_consume = False
        waiting_base_current_path = ""
        last_status_error = ""
        last_wait_log_at = 0.0

        def _do_send(prompt_text: str, iter_at_start: int) -> bool:
            try:
                result = send_a1111_txt2img(
                    prompt=prompt_text,
                    host=str(self._cfg.sd_prompt_target_host or ""),
                    port=int(self._cfg.sd_prompt_target_port or 7860),
                    endpoint=str(self._cfg.sd_prompt_endpoint or "/sdapi/v1/txt2img"),
                    token=str(self._cfg.sd_prompt_token or ""),
                    timeout_sec=float(self._cfg.sd_prompt_timeout_sec or 40.0),
                    model_checkpoint=str(self._cfg.sd_prompt_model_checkpoint or ""),
                    vae=str(self._cfg.sd_prompt_vae or ""),
                    clip_skip=int(self._cfg.sd_prompt_clip_skip or 0),
                    append_prompt=str(self._cfg.sd_prompt_append_prompt or ""),
                    negative_prompt=str(self._cfg.sd_prompt_negative_prompt or ""),
                    steps=int(self._cfg.sd_prompt_steps or 20),
                    width=int(self._cfg.sd_prompt_width or 512),
                    height=int(self._cfg.sd_prompt_height or 768),
                    cfg_scale=float(self._cfg.sd_prompt_cfg_scale or 7.0),
                    sampler_name=str(self._cfg.sd_prompt_sampler_name or ""),
                    scheduler=str(self._cfg.sd_prompt_scheduler or ""),
                    seed=int(self._cfg.sd_prompt_seed if self._cfg.sd_prompt_seed is not None else -1),
                    subseed=int(self._cfg.sd_prompt_subseed if self._cfg.sd_prompt_subseed is not None else -1),
                    subseed_strength=float(self._cfg.sd_prompt_subseed_strength or 0.0),
                    batch_size=int(self._cfg.sd_prompt_batch_size or 1),
                    n_iter=int(self._cfg.sd_prompt_n_iter or 1),
                    restore_faces=bool(self._cfg.sd_prompt_restore_faces),
                    tiling=bool(self._cfg.sd_prompt_tiling),
                    save_images=bool(self._cfg.sd_prompt_save_images),
                    send_images=bool(self._cfg.sd_prompt_send_images),
                    enable_hr=bool(self._cfg.sd_prompt_enable_hr),
                    hr_scale=float(self._cfg.sd_prompt_hr_scale or 2.0),
                    hr_upscaler=str(self._cfg.sd_prompt_hr_upscaler or "Latent"),
                    hr_second_pass_steps=int(self._cfg.sd_prompt_hr_second_pass_steps or 0),
                    denoising_strength=float(self._cfg.sd_prompt_denoising_strength or 0.45),
                    hr_resize_x=int(self._cfg.sd_prompt_hr_resize_x or 0),
                    hr_resize_y=int(self._cfg.sd_prompt_hr_resize_y or 0),
                    hr_sampler_name=str(self._cfg.sd_prompt_hr_sampler_name or ""),
                    hr_scheduler=str(self._cfg.sd_prompt_hr_scheduler or ""),
                    hr_checkpoint_name=str(self._cfg.sd_prompt_hr_checkpoint_name or ""),
                    hr_prompt=str(self._cfg.sd_prompt_hr_prompt or ""),
                    hr_negative_prompt=str(self._cfg.sd_prompt_hr_negative_prompt or ""),
                    extra_payload_json=str(self._cfg.sd_prompt_extra_payload_json or ""),
                )
                self.log.emit(
                    f"[sd-forever] send iter={iter_at_start} ok={int(result.ok)} status={result.status} err={result.error[:120]}"
                )
                return bool(result.ok)
            except Exception as exc:
                self.log.emit(f"[sd-forever] send error iter={iter_at_start}: {exc}")
                return False

        while self._sd_forever_running and self._running:
            with self._sd_prompt_lock:
                prompt = self._current_sd_prompt
                iter_at_start = self._sd_iteration
            if not prompt:
                time.sleep(0.5)
                continue

            prompt_changed = iter_at_start != last_prompt_iter
            if prompt_changed:
                waiting_for_slideshow_consume = False
                waiting_base_current_path = ""
                last_prompt_iter = iter_at_start

            sync_enabled = bool(getattr(self._cfg, "sd_blankmap_sync_enabled", True))
            status_current_path = ""
            status_play_mode = ""
            if sync_enabled:
                status = self._fetch_blankmap_slideshow_status()
                if not status.get("ok", False):
                    err = str(status.get("error", "") or "status failed")
                    if err != last_status_error:
                        self.log.emit(f"[sd-forever] BlankMapAdd status failed url={status.get('url', '')} err={err[:160]}")
                        last_status_error = err
                    time.sleep(2.0)
                    continue
                last_status_error = ""

                status_current_path = str(status.get("current_path", "") or "")
                status_play_mode = str(status.get("play_mode", "") or "")
                pending_path = str(status.get("pending_path", "") or "")
                transition_path = str(status.get("transition_path", "") or "")
                scan_interval = max(0.5, min(5.0, float(status.get("scan_interval_sec", 1.0) or 1.0)))

                if not bool(status.get("enabled", False)):
                    now = time.monotonic()
                    if now - last_wait_log_at >= 10.0:
                        self.log.emit("[sd-forever] BlankMapAdd slideshow disabled; generation paused")
                        last_wait_log_at = now
                    time.sleep(scan_interval)
                    continue

                if waiting_for_slideshow_consume:
                    consumed = False
                    if waiting_base_current_path and status_current_path and status_current_path != waiting_base_current_path:
                        consumed = True
                    elif (not waiting_base_current_path) and status_current_path:
                        consumed = True
                    if consumed:
                        waiting_for_slideshow_consume = False
                        waiting_base_current_path = ""
                        self.log.emit(f"[sd-forever] slideshow consumed current={status_current_path}")

                if waiting_for_slideshow_consume:
                    now = time.monotonic()
                    if now - last_wait_log_at >= 10.0:
                        self.log.emit(
                            f"[sd-forever] waiting slideshow consume current={status_current_path or '(empty)'} pending={pending_path or '(empty)'}"
                        )
                        last_wait_log_at = now
                    time.sleep(scan_interval)
                    continue

                if (not prompt_changed) and (pending_path or transition_path):
                    now = time.monotonic()
                    if now - last_wait_log_at >= 10.0:
                        self.log.emit(
                            f"[sd-forever] pending image exists pending={pending_path or transition_path}"
                        )
                        last_wait_log_at = now
                    time.sleep(scan_interval)
                    continue
            else:
                # fallback: BlankMapAdd同期なしの場合だけ従来の送信開始間隔で抑制する
                interval = max(0, int(getattr(self._cfg, "sd_slideshow_interval_sec", 20) or 0))
                if last_send_started_at > 0 and interval > 0:
                    elapsed = time.monotonic() - last_send_started_at
                    wait_left = interval - elapsed
                    while wait_left > 0 and self._sd_forever_running and self._running:
                        with self._sd_prompt_lock:
                            if self._sd_iteration != iter_at_start:
                                break
                        time.sleep(min(0.2, wait_left))
                        wait_left = interval - (time.monotonic() - last_send_started_at)

            if not self._sd_forever_running or not self._running:
                break

            last_send_started_at = time.monotonic()
            send_state = {"done": False, "ok": False}

            def _send_and_mark() -> None:
                send_state["ok"] = _do_send(prompt, iter_at_start)
                send_state["done"] = True

            send_thread = threading.Thread(target=_send_and_mark, daemon=True)
            send_thread.start()

            # 監視: 別プロンプトが来たら interrupt、ループ停止要求が来たら interrupt
            interrupted = False
            while send_thread.is_alive():
                if not self._sd_forever_running or not self._running:
                    try:
                        post_a1111_interrupt(
                            host=str(self._cfg.sd_prompt_target_host or ""),
                            port=int(self._cfg.sd_prompt_target_port or 7860),
                            token=str(self._cfg.sd_prompt_token or ""),
                            timeout_sec=3.0,
                        )
                    except Exception:
                        pass
                    interrupted = True
                    break
                with self._sd_prompt_lock:
                    current_iter = self._sd_iteration
                if current_iter != iter_at_start:
                    self.log.emit(f"[sd-forever] prompt changed -> interrupt iter={iter_at_start}")
                    try:
                        post_a1111_interrupt(
                            host=str(self._cfg.sd_prompt_target_host or ""),
                            port=int(self._cfg.sd_prompt_target_port or 7860),
                            token=str(self._cfg.sd_prompt_token or ""),
                            timeout_sec=3.0,
                        )
                    except Exception:
                        pass
                    interrupted = True
                    break
                time.sleep(0.2)
            send_thread.join(timeout=max(1.0, float(self._cfg.sd_prompt_timeout_sec or 40.0)))
            if sync_enabled and (not interrupted) and send_state.get("done") and send_state.get("ok"):
                # 新しいプロンプトの絵はQueueモードでも待たせず即表示させる
                if prompt_changed and status_play_mode == "Queue":
                    if self._post_blankmap_slideshow_show_latest():
                        self.log.emit("[sd-forever] new prompt -> show latest now")
                waiting_for_slideshow_consume = True
                waiting_base_current_path = status_current_path
                self.log.emit(
                    f"[sd-forever] generated prefetch; wait slideshow current change from={waiting_base_current_path or '(empty)'}"
                )
            elif not send_state.get("done"):
                time.sleep(2.0)

    def _start_sd_control_server(self) -> None:
        port = int(getattr(self._cfg, "sd_control_port", 18768) or 18768)
        host = "127.0.0.1"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _write_json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._write_json(int(HTTPStatus.OK), {
                        "ok": True,
                        "sd_forever_running": owner._sd_forever_running,
                        "iteration": owner._sd_iteration,
                    })
                    return
                self._write_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})

            def do_POST(self) -> None:
                if self.path == "/sd/stop":
                    owner._stop_sd_forever(interrupt_current=True)
                    self._write_json(int(HTTPStatus.OK), {"ok": True, "stopped": True})
                    return
                self._write_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "error": "not found"})

            def log_message(self, fmt: str, *args) -> None:
                owner.log.emit("[sd-control-http] " + (fmt % args))

        try:
            server = ThreadingHTTPServer((host, port), Handler)
        except Exception as exc:
            self.log.emit(f"[sd-control] 起動失敗: {exc}")
            return
        server.daemon_threads = True
        server.timeout = 0.5
        self._sd_control_server = server
        self._sd_control_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        self._sd_control_thread.start()
        self.log.emit(f"[sd-control] listening http://{host}:{port}/sd/stop")

    def _stop_sd_control_server(self) -> None:
        if self._sd_control_server is not None:
            try:
                self._sd_control_server.shutdown()
            except Exception:
                pass
            try:
                self._sd_control_server.server_close()
            except Exception:
                pass
            self._sd_control_server = None

        if self._external_server_thread is not None:
            self._external_server_thread.join(timeout=2.0)
            self._external_server_thread = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.log.emit("[pipeline] フォルダ作成中...")
            self._cfg.wav_dir.mkdir(parents=True, exist_ok=True)
            for sub in (
                "transcripts",
                "responses",
                "grok_tts_outputs",
                "source_wavs",
                "sbv2_inputs",
                "sbv2_wavs",
            ):
                (self._cfg.output_dir / sub).mkdir(parents=True, exist_ok=True)
            self.log.emit(f"[pipeline] フォルダOK: wav={self._cfg.wav_dir}, out={self._cfg.output_dir}")

            self.log.emit("[pipeline] transcribeサーバー起動中...")
            self._start_transcribe_server()
            self.log.emit("[pipeline] transcribeサーバーOK")

            self.log.emit("[pipeline] SBV2サーバー起動中...")
            self._start_sbv2_server()
            self.log.emit("[pipeline] SBV2サーバーOK")

            self.log.emit("[pipeline] 外部テキストサーバー起動中...")
            self._start_external_text_server()
            self.log.emit("[pipeline] 外部テキストサーバーOK")

            self._start_sd_forever_loop()
            self._start_sd_control_server()

            self.log.emit("[pipeline] ファイル監視起動中...")
            self._observer = self._create_observer()
            self._observer.start()
            self.log.emit(f"[pipeline] 全起動完了 — 監視開始: {self._cfg.wav_dir}")

            while self._running:
                # 手動テキスト優先
                try:
                    text = self._text_queue.get_nowait()
                    raw_text = str(text or "").strip()
                    if not raw_text:
                        continue
                    text_grok = self._apply_transcribe_conversion(raw_text, mode="grok")
                    text_display = self._apply_transcribe_conversion(raw_text, mode="display")
                    if text_grok != raw_text:
                        self.log.emit(f"[manual-conv] grok: '{raw_text}' -> '{text_grok}'")
                    if text_display != raw_text:
                        self.log.emit(f"[manual-conv] display: '{raw_text}' -> '{text_display}'")
                    self._process_text(text_grok, manual=True, display_text=text_display)
                except queue.Empty:
                    pass

                # WAVキュー
                try:
                    wav = self._wav_queue.get(timeout=0.3)
                    if self._paused:
                        self.log.emit(f"[pause] 破棄: {wav.name}")
                    elif self._source_mode() == "external":
                        self.log.emit(f"[source_mode] external: WAV無視 {wav.name}")
                        try:
                            wav.unlink(missing_ok=True)
                        except Exception:
                            pass
                    else:
                        self._process_wav(wav)
                except queue.Empty:
                    pass

        except Exception:
            tb = traceback.format_exc()
            self.log.emit(f"[pipeline] ★致命的エラーで停止:\n{tb}")
            self.error.emit(tb)
        finally:
            self.log.emit("[pipeline] 終了処理開始...")
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=3.0)
            self._stop_external_text_server()
            self.log.emit("[pipeline] パイプライン停止完了")
            self.finished.emit()

    def _create_observer(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        owner = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    owner._enqueue(Path(event.src_path))
            def on_moved(self, event):
                if not event.is_directory:
                    owner._enqueue(Path(event.dest_path))

        obs = Observer()
        obs.schedule(Handler(), str(self._cfg.wav_dir), recursive=False)
        return obs

    def _enqueue(self, path: Path) -> None:
        if path.suffix.lower() != ".wav":
            return
        if self._source_mode() == "external":
            return
        key = str(path.resolve())
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
        try:
            self._wav_queue.put_nowait(path)
        except queue.Full:
            self.log.emit(f"[warn] WAVキュー満杯: {path.name}")

    def _wait_stable(self, path: Path) -> bool:
        stable, last_size, start = 0, -1, time.time()
        while self._running and not self._paused and (time.time() - start) < 30.0:
            if not path.exists():
                stable = 0; last_size = -1; time.sleep(0.25); continue
            size = path.stat().st_size
            if size > 0 and size == last_size:
                stable += 1
                if stable >= 3:
                    return True
            else:
                stable = 0; last_size = size
            time.sleep(0.25)
        return False

    def _is_filtered(self, text: str) -> bool:
        lower = text.lower()
        normalized: list[tuple[int, str, str]] = []
        for idx, entry in enumerate(self._cfg.filter_phrases):
            if isinstance(entry, str):
                entry = {"pattern": entry, "type": "partial"}
            if not self._entry_enabled(entry, True):
                continue
            pattern = entry.get("pattern", "").strip()
            ftype = entry.get("type", "partial")
            if not pattern:
                continue
            normalized.append((idx, pattern, ftype))

        normalized.sort(key=lambda x: (-len(x[1]), x[0]))
        for _idx, pattern, ftype in normalized:
            if ftype == "exact":
                if text.strip() == pattern:
                    return True
            elif ftype == "regex":
                try:
                    if re.search(pattern, text):
                        return True
                except re.error:
                    pass
            else:
                if pattern.lower() in lower:
                    return True
        return False

    def _run_cmd(self, cmd: list[str], timeout_sec: float) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=_with_utf8_env(),
        )
        with self._proc_lock:
            self._current_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.terminate()
            stdout, stderr = proc.communicate()
        finally:
            with self._proc_lock:
                self._current_proc = None
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def _resolve_scripts(self) -> tuple[Path, Path]:
        # ローカル同梱スクリプトを優先、なければ従来のkks_rootパスにフォールバック
        local_root = PROJECT_ROOT
        transcribe = local_root / "run_transcribe_one_wav.py"
        tts_event = local_root / "run_grok_tts_event.py"
        if transcribe.exists() and tts_event.exists():
            return transcribe, tts_event
        root = self._cfg.kks_root / "work" / "tools" / "grok_bridge"
        return (root / "run_transcribe_one_wav.py").resolve(), (root / "run_grok_tts_event.py").resolve()

    def _transcribe_server_url(self) -> str:
        return f"http://127.0.0.1:{self._cfg.transcribe_server_port}"

    def _start_transcribe_server(self) -> None:
        health_url = f"{self._transcribe_server_url()}/health"

        # 既存サーバーが起動済みなら再利用
        try:
            with urllib.request.urlopen(health_url, timeout=2.0):
                self.log.emit("[server] 既存転写サーバーを再利用")
                return
        except Exception:
            pass

        local_root = PROJECT_ROOT
        server_script = local_root / "run_transcribe_server.py"
        if not server_script.exists():
            root = self._cfg.kks_root / "work" / "tools" / "grok_bridge"
            server_script = (root / "run_transcribe_server.py").resolve()
        cmd = [
            str(self._cfg.faster_python), str(server_script),
            "--model", self._cfg.faster_model,
            "--device", self._cfg.faster_device,
            "--compute-type", self._cfg.faster_compute,
            "--language", self._cfg.faster_language,
            "--beam-size", str(self._cfg.faster_beam),
            "--port", str(self._cfg.transcribe_server_port),
        ]
        self.log.emit(f"[server] 転写サーバー起動中 ({self._cfg.faster_model}) ...")
        self._transcribe_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_with_utf8_env(),
        )

        # stdout をバックグラウンドスレッドでログに転送
        proc = self._transcribe_proc
        log_emit = self.log.emit
        def _forward():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        log_emit(line)
            except Exception:
                pass
        threading.Thread(target=_forward, daemon=True).start()

        # サーバーが Ready になるまで待機（モデルロード時間を考慮して最大90秒）
        deadline = time.time() + 90.0
        while time.time() < deadline and self._running:
            if proc.poll() is not None:
                raise RuntimeError("転写サーバーが異常終了しました")
            try:
                with urllib.request.urlopen(health_url, timeout=1.0):
                    self.log.emit("[server] 転写サーバー Ready")
                    return
            except Exception:
                time.sleep(1.0)
        raise RuntimeError("転写サーバーの起動タイムアウト (90秒)")

    def _start_sbv2_server(self) -> None:
        if not self._sbv2_use_http():
            self.log.emit("[sbv2] mode=local: HTTPサーバー起動チェックをスキップ")
            return

        base_url = self._sbv2_server_url()
        if not base_url:
            raise RuntimeError("sbv2_mode=http ですが sbv2_server_url が未設定です")

        if not self._cfg.sbv2_server_auto_start:
            return
        health_url = base_url.rstrip("/") + "/models/info"
        try:
            with urllib.request.urlopen(health_url, timeout=2.0):
                self.log.emit("[sbv2] 既存SBV2サーバーを再利用")
                return
        except Exception:
            pass

        sbv2_root = self._cfg.sbv2_root
        sbv2_python = sbv2_root / "venv" / "Scripts" / "python.exe"
        if not sbv2_python.exists():
            self.log.emit(f"[sbv2] python not found: {sbv2_python} → 手動起動してください")
            return

        server_script = sbv2_root / "server_fastapi.py"
        if not server_script.exists():
            self.log.emit(f"[sbv2] server_fastapi.py not found: {server_script}")
            return

        self.log.emit("[sbv2] SBV2サーバー起動中 (モデルロードに数十秒かかります) ...")
        self._sbv2_proc = subprocess.Popen(
            [str(sbv2_python), str(server_script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_with_utf8_env(),
            cwd=str(sbv2_root),
        )

        proc = self._sbv2_proc
        log_emit = self.log.emit
        def _forward():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._append_sbv2_log_tail(line)
                        log_emit(f"[sbv2] {line}")
            except Exception:
                pass
            finally:
                rc = proc.poll()
                if rc is not None:
                    self._sbv2_last_exit_code = rc
                    self._sbv2_last_exit_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if self._running:
                        log_emit(f"[sbv2] server process exited rc={rc}")
                        self._emit_sbv2_diagnostics("stdout_closed")
        threading.Thread(target=_forward, daemon=True).start()

        deadline = time.time() + 300.0
        while time.time() < deadline and self._running:
            if proc.poll() is not None:
                self._sbv2_last_exit_code = proc.poll()
                self._sbv2_last_exit_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._emit_sbv2_diagnostics("start_wait_exited")
                raise RuntimeError("SBV2サーバーが異常終了しました")
            try:
                with urllib.request.urlopen(health_url, timeout=1.0):
                    self.log.emit("[sbv2] SBV2サーバー Ready")
                    return
            except Exception:
                time.sleep(1.0)
        self._emit_sbv2_diagnostics("start_wait_timeout")
        raise RuntimeError("SBV2サーバーの起動タイムアウト (300秒)")

    def _transcribe_via_server(self, wav: Path, trace_id: str = "") -> dict:
        url = f"{self._transcribe_server_url()}/transcribe"
        payload = json.dumps({
            "audio": str(wav),
            "language": self._cfg.faster_language,
            "beam_size": self._cfg.faster_beam,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        trace = trace_id or wav.stem
        self.log.emit(
            "[stt] send "
            f"trace={trace} wav={wav.name} lang={self._cfg.faster_language} beam={self._cfg.faster_beam}"
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120.0) as resp:
                raw = resp.read()
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                data = json.loads(raw.decode("utf-8"))
                ok = bool(data.get("ok", False)) if isinstance(data, dict) else False
                text_len = len(str(data.get("text", ""))) if isinstance(data, dict) else -1
                self.log.emit(
                    "[stt] recv "
                    f"trace={trace} status={getattr(resp, 'status', '?')} bytes={len(raw)} "
                    f"elapsed_ms={elapsed_ms:.0f} ok={ok} text_len={text_len}"
                )
                return data
        except urllib.error.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            self.log.emit(
                "[stt] error "
                f"trace={trace} type=http status={exc.code} elapsed_ms={elapsed_ms:.0f} "
                f"detail={(body or str(exc))[:200]}"
            )
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.log.emit(
                "[stt] error "
                f"trace={trace} type=exception elapsed_ms={elapsed_ms:.0f} detail={exc}"
            )
            raise

    @staticmethod
    def _unique_output_path(path: Path) -> Path:
        if not path.exists():
            return path
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return path.with_name(f"{path.stem}_{stamp}{path.suffix}")

    def _save_text_file(self, path: Path, text: str) -> Optional[Path]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            out = self._unique_output_path(path)
            out.write_text(text, encoding="utf-8")
            return out
        except Exception as exc:
            self.log.emit(f"[warn] テキスト保存失敗: {path} ({exc})")
            return None

    def _append_session_text(self, subdir: str, text: str, *, label: str = "") -> Optional[Path]:
        try:
            out = self._cfg.output_dir / subdir / f"session_{self._session_id}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            head = f"[{stamp}] {label}".rstrip() + "\n"
            body = (text or "").rstrip("\n") + "\n\n"
            with out.open("a", encoding="utf-8", newline="\n") as f:
                f.write(head)
                f.write(body)
            return out
        except Exception as exc:
            self.log.emit(f"[warn] セッション追記保存失敗: {subdir} ({exc})")
            return None

    def _copy_output_file(self, src: Path, dst_dir: Path, *, out_name: Optional[str] = None) -> Optional[Path]:
        try:
            if not src.exists() or not src.is_file():
                return None
            dst_dir.mkdir(parents=True, exist_ok=True)
            target = dst_dir / (out_name or src.name)
            target = self._unique_output_path(target)
            shutil.copy2(src, target)
            return target
        except Exception as exc:
            self.log.emit(f"[warn] ファイル保存失敗: {src} -> {dst_dir} ({exc})")
            return None

    def _session_dir(self, subdir: str) -> Path:
        return self._cfg.output_dir / subdir / f"session_{self._session_id}"

    def _studio_face_preset_json_path(self) -> Path:
        preferred = (self._cfg.kks_root / "BepInEx" / "plugins" / "StudioFacePresetTool" / "StudioFacePresets.json").resolve()
        if preferred.exists():
            return preferred
        try:
            fallback_root = PROJECT_ROOT.parents[2]
            fallback = (fallback_root / "BepInEx" / "plugins" / "StudioFacePresetTool" / "StudioFacePresets.json").resolve()
            if fallback.exists():
                return fallback
        except Exception:
            pass
        return preferred

    def _load_studio_face_presets(self) -> list[dict]:
        path = self._studio_face_preset_json_path()
        if (not path.exists()) or (not path.is_file()):
            raise FileNotFoundError(f"StudioFacePresets.json not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        presets = raw.get("Presets", []) if isinstance(raw, dict) else []
        if not isinstance(presets, list):
            return []
        rows: list[dict] = []
        for entry in presets:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("Name", "")).strip()
            preset_id = str(entry.get("Id", "")).strip()
            if name:
                rows.append({"name": name, "id": preset_id})
        return rows

    def _process_wav(self, wav: Path) -> None:
        if not self._wait_stable(wav):
            self.log.emit(f"[warn] 不安定/一時停止のためスキップ: {wav.name}")
            return
        if self._paused:
            self.log.emit(f"[pause] 破棄: {wav.name}")
            return

        try:
            trace = wav.stem
            self.log.emit(f"[transcribe] {wav.name} trace={trace}")
            t_json = self._transcribe_via_server(wav, trace_id=trace)
            if not t_json.get("ok"):
                raise RuntimeError(str(t_json.get("error", "transcribe failed")))
            text = str(t_json.get("text", "")).strip()
            if not text:
                self.log.emit(f"[info] 空テキスト: {wav.name}")
                return
            raw_text = text
            text = self._apply_transcribe_conversion(raw_text, mode="grok")
            display_text = self._apply_transcribe_conversion(raw_text, mode="display")
            if not text:
                self.log.emit(f"[info] 変換後に空テキスト: {wav.name}")
                return
            if self._cfg.save_fasterwhisper_text:
                saved_transcript = self._append_session_text(
                    "transcripts",
                    raw_text,
                    label=f"wav={wav.name}",
                )
                if saved_transcript is not None:
                    self.log.emit(f"[save] whisper_text: {saved_transcript}")

            if self._paused:
                self.log.emit(f"[pause] 破棄: {text[:40]}")
                return
            if self._is_filtered(text):
                self.log.emit(f"[filter] 除外: {text[:60]}")
                return
            self._process_text(text, wav=wav, display_text=display_text)
        except Exception as exc:
            self.log.emit(f"[error] {wav.name}: {exc}")
        finally:
            if self._cfg.save_source_wav:
                saved_wav = self._copy_output_file(
                    wav,
                    self._session_dir("source_wavs"),
                )
                if saved_wav is not None:
                    self.log.emit(f"[save] source_wav: {saved_wav}")
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass

    def update_transcribe_conv(self, rules: list[dict]) -> None:
        with self._transcribe_conv_lock:
            self._transcribe_conv_rules = list(rules or [])

    def update_runtime_config(self, cfg: AppConfig) -> None:
        prev_video_metadata = self._cfg.video_metadata_path
        self._cfg = cfg
        self.update_transcribe_conv(cfg.transcribe_conversion_dict)
        if prev_video_metadata != cfg.video_metadata_path:
            with self._song_kana_lock:
                self._video_metadata = self._load_json_safe(cfg.video_metadata_path)
                self._title_to_indices = self._build_title_to_indices()
                self._sorted_titles = sorted(self._title_to_indices.keys(), key=len, reverse=True)
                self._kana_rules = self._build_kana_rules()
            self.log.emit(f"[live] video_metadata reloaded: {cfg.video_metadata_path or '(none)'}")

    def _apply_transcribe_conversion(self, text: str, mode: str = "grok") -> str:
        converted = text
        applied = 0
        with self._transcribe_conv_lock:
            rules = list(self._transcribe_conv_rules)
        target = "display" if str(mode).strip().lower() == "display" else "grok"
        ordered_rules: list[tuple[int, str, dict]] = []
        for idx, row in enumerate(rules):
            if not isinstance(row, dict):
                continue
            if not self._entry_enabled(row, True):
                continue
            src = str(row.get("from", ""))
            if not src:
                continue
            ordered_rules.append((idx, src, row))

        ordered_rules.sort(key=lambda x: (-len(x[1]), x[0]))
        for _idx, src, row in ordered_rules:
            if target == "display":
                if not bool(row.get("display_apply", True)):
                    continue
                if "to_display" in row:
                    dst = str(row.get("to_display", ""))
                elif "to" in row:
                    # backward compatible with old schema
                    dst = str(row.get("to", ""))
                else:
                    dst = str(row.get("to_grok", ""))
            else:
                if "to_grok" in row:
                    dst = str(row.get("to_grok", ""))
                elif "to" in row:
                    # backward compatible with old schema
                    dst = str(row.get("to", ""))
                else:
                    dst = str(row.get("to_display", ""))
            hit = converted.count(src)
            if hit <= 0:
                continue
            converted = converted.replace(src, dst)
            applied += 1
            tag = "stt-conv-display" if target == "display" else "stt-conv-grok"
            self.log.emit(f"[{tag}] '{src}' -> '{dst}' (hits={hit})")
        if applied > 0:
            tag = "stt-conv-display" if target == "display" else "stt-conv-grok"
            self.log.emit(f"[{tag}] applied_rules={applied}")
        return converted

    def _post_json(self, host: str, port: int, endpoint: str, token: str,
                   payload: dict, timeout_sec: float) -> tuple[bool, str]:
        ep = ("/" + endpoint.strip().lstrip("/")) if endpoint.strip() else "/"
        url = f"http://{host}:{port}{ep}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            req.add_header("X-Auth-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=max(0.1, timeout_sec)) as resp:
                return True, resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            return False, f"HTTP {exc.code}: {detail}"
        except Exception as exc:
            return False, str(exc)

    def _send_subtitle(self, text: str, wav_name: str, mode: str, hold_seconds: Optional[float] = None) -> None:
        if not self._cfg.subtitle_send_enabled:
            return
        host = self._cfg.subtitle_target_host or "127.0.0.1"
        text_payload = text
        if mode.lower() == "stackfemale":
            t = text.strip()
            if t and "<color=" not in t.lower():
                text_payload = f"<color=#FF7ACDFF>{t}</color>"
        payload = {"text": text_payload, "source": "human2kks", "wav_name": wav_name, "display_mode": mode}
        if hold_seconds is not None:
            payload["hold_seconds"] = hold_seconds
        ok, detail = self._post_json(
            host, self._cfg.subtitle_target_port,
            self._cfg.subtitle_endpoint, self._cfg.subtitle_token,
            payload,
            self._cfg.subtitle_timeout_sec,
        )
        if not ok:
            self.log.emit(f"[subtitle] 失敗: {detail}")

    def _apply_kana_conversion(self, text: str) -> tuple[str, list[int]]:
        """テキスト中のartist/titleをカナに変換し、マッチしたsong_kana_mapのインデックス一覧を返す"""
        result = text
        matched_indices: list[int] = []
        for key_lower, kana_value, idx, _kind in self._kana_rules:
            search_pos = 0
            lower_result = result.lower()
            while True:
                pos = lower_result.find(key_lower, search_pos)
                if pos < 0:
                    break
                original = result[pos:pos + len(key_lower)]
                result = result[:pos] + kana_value + result[pos + len(key_lower):]
                lower_result = result.lower()
                search_pos = pos + len(kana_value)
                if idx not in matched_indices:
                    matched_indices.append(idx)
        return result, matched_indices

    def _find_video_indices_from_response(self, response: str) -> list[int]:
        """Grokレスポンスから動画切り替えトリガーを検出してsong_kana_mapのインデックスを返す"""
        if not response:
            return []

        trigger_match = re.search(r"今から(?:もう一度|もう一回|また)?流すね[♡♥❤💗💖💓…\.\s]*", response)
        if not trigger_match:
            return []

        payload = response[trigger_match.end():].splitlines()[0].strip()
        if not payload:
            return []

        payload_candidates = [
            payload,
            self._strip_video_payload_prefix(payload),
        ]

        # 1) タイトル前方一致（最も厳密）を優先
        for candidate in payload_candidates:
            candidate_lower = candidate.lower()
            for title_lower in self._sorted_titles:
                if candidate_lower.startswith(title_lower):
                    indices = self._title_to_indices.get(title_lower, [])
                    self.log.emit(f"[video] トリガー検出(前方一致): '{payload}' -> title='{title_lower}' indices={indices}")
                    return indices

        # 2) 先頭付近の部分一致（「今から流すね♡ TITLE♡ 〜」を拾うため）
        best_title = ""
        best_pos = 9999
        for candidate in payload_candidates:
            candidate_lower = candidate.lower()
            for title_lower in self._sorted_titles:
                pos = candidate_lower.find(title_lower)
                if pos < 0 or pos > 24:
                    continue
                if pos < best_pos or (pos == best_pos and len(title_lower) > len(best_title)):
                    best_pos = pos
                    best_title = title_lower

        if best_title:
            indices = self._title_to_indices.get(best_title, [])
            self.log.emit(f"[video] トリガー検出(近傍一致): '{payload}' -> title='{best_title}' indices={indices}")
            return indices

        self.log.emit(f"[video] トリガー検出失敗: '{payload}'")
        return []

    def _schedule_response_text(self, text: str, main_index: int, delay_sec: float, session_id: str = "") -> None:
        """Grokの生テキストをそのままKKSへ送る（C#側でキーワードマッチ）"""
        sender_ps1 = Path(__file__).resolve().parent.parent / "send_voice_face_event.ps1"
        pipe_name = self._cfg.pipe_name
        target_host = self._cfg.target_host.strip()
        remote_http = self._cfg.remote_http
        target_port = self._cfg.target_port
        target_endpoint = self._cfg.target_endpoint
        running_ref = lambda: self._running

        def _send():
            if not running_ref():
                return
            payload_obj = {"type": "response_text", "text": text, "main": main_index, "delaySeconds": delay_sec or 0.0}
            if session_id:
                payload_obj["sessionId"] = session_id
            payload = json.dumps(payload_obj, ensure_ascii=False)
            json_path = ""
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as temp_file:
                temp_file.write(payload)
                temp_file.write("\n")
                json_path = temp_file.name
            cmd = [
                "powershell", "-ExecutionPolicy", "Bypass", "-File", str(sender_ps1),
                "-PipeName", pipe_name,
                "-JsonFile", json_path,
            ]
            if remote_http or target_host:
                cmd.append("-RemoteHttp")
            if target_host:
                cmd.extend(["-TargetHost", target_host,
                             "-TargetPort", str(target_port),
                             "-TargetEndpoint", target_endpoint])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=10,
                                        env=_with_utf8_env())
                if result.returncode != 0:
                    self.log.emit(f"[response_text] pipe送信失敗: {(result.stderr or result.stdout or '').strip()}")
                else:
                    self.log.emit(f"[response_text] 送信完了")
            except Exception as e:
                self.log.emit(f"[response_text] pipe送信例外: {e}")
            finally:
                if json_path:
                    try:
                        Path(json_path).unlink(missing_ok=True)
                    except Exception:
                        pass

        threading.Thread(target=_send, daemon=True).start()

    def _increment_play_counts(self, indices: list[int]) -> None:
        """song_kana_mapのplay_countをインクリメントしてファイルに書き戻す"""
        if not indices:
            return
        if not self._song_kana_map:
            return
        with self._song_kana_lock:
            for idx in indices:
                if 0 <= idx < len(self._song_kana_map):
                    self._song_kana_map[idx]["play_count"] = self._song_kana_map[idx].get("play_count", 0) + 1
            try:
                self._song_kana_map_path.parent.mkdir(parents=True, exist_ok=True)
                self._song_kana_map_path.write_text(
                    json.dumps(self._song_kana_map, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as e:
                self.log.emit(f"[warn] song_kana_map 書き込み失敗: {e}")

    def _schedule_video_switch(self, matched_indices: list[int], delay_sec: float) -> None:
        """delay_sec秒後にKKSへ動画切り替え信号を送るバックグラウンドスレッドを起動"""
        if not matched_indices:
            return

        # マッチしたタイトル一覧を収集（重複除去）
        titles = []
        seen_titles: set[str] = set()
        for idx in matched_indices:
            if 0 <= idx < len(self._song_kana_map):
                t = self._song_kana_map[idx].get("title", "")
                if t and t not in seen_titles:
                    titles.append(t)
                    seen_titles.add(t)

        if not titles:
            return

        chosen_title = random.choice(titles)

        # video_metadata.json から同タイトルの全ファイルを取得
        candidates = [
            e["file"] for e in self._video_metadata
            if e.get("title", "").lower() == chosen_title.lower() and e.get("file", "")
        ]
        if not candidates:
            self.log.emit(f"[video] タイトル '{chosen_title}' に一致する動画なし")
            return

        chosen_file = random.choice(candidates)

        def _send():
            if delay_sec and delay_sec > 0:
                time.sleep(delay_sec)
            if not self._running:
                return
            payload = json.dumps({"filename": chosen_file}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:55982/videoroom/play",
                data=payload, method="POST"
            )
            req.add_header("Content-Type", "application/json; charset=utf-8")
            try:
                with urllib.request.urlopen(req, timeout=5.0):
                    pass
                self.log.emit(f"[video] 切替 → {chosen_file} (title: {chosen_title})")
            except Exception as e:
                self.log.emit(f"[video] KKS送信失敗: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def _process_text(self, text: str, wav: Optional[Path] = None, manual: bool = False, display_text: Optional[str] = None) -> None:
        _, pipeline_script = self._resolve_scripts()
        wav_name = wav.name if wav else "manual"
        # 字幕は元テキストのまま送る
        self._send_subtitle(display_text if display_text is not None else text, wav_name, "StackMale")

        # カナ変換ルールをconversion_jsonとして渡す（Grokレスポンスに適用される）
        kana_conv = [{"from": r[4], "to": r[1]} for r in self._kana_rules if r[4] and r[1]]
        combined_conv = kana_conv + list(self._cfg.conversion_dict or [])
        response_limit = max(3000, max(1, int(self._cfg.max_response_chars))) if self._cfg.max_response_chars_enabled else 0
        llm_text = text

        p_cmd = [
            str(self._cfg.pipeline_python), str(pipeline_script),
            "--text", llm_text,
            "--max-response-chars", str(response_limit),
            "--sbv2-root", str(self._cfg.sbv2_root),
            "--model-name", self._cfg.sbv2_model_name,
            "--speaker", self._cfg.sbv2_speaker,
            "--style", self._cfg.sbv2_style,
            "--length", str(self._cfg.sbv2_length),
            "--output-dir", str(self._cfg.output_dir / "grok_tts_outputs"),
            "--pipe-name", self._cfg.pipe_name,
            "--main", str(self._cfg.main_index),
        ]
        p_cmd.extend([
            "--llm-backend", str(self._cfg.llm_backend or "grok_browser"),
            "--llm-base-url", str(self._cfg.llm_base_url or "http://127.0.0.1:1234/v1"),
            "--llm-model", str(self._cfg.llm_model or ""),
            "--llm-api-key", str(self._cfg.llm_api_key or ""),
            "--llm-temperature", str(self._cfg.llm_temperature),
            "--llm-max-tokens", str(self._cfg.llm_max_tokens),
            "--llm-timeout", str(self._cfg.llm_timeout_seconds),
        ])
        if str(self._cfg.llm_system_prompt or "").strip():
            p_cmd.extend(["--llm-system-prompt", str(self._cfg.llm_system_prompt)])
        if self._cfg.voice_volume >= 0:
            p_cmd.extend(["--voice-volume", str(self._cfg.voice_volume)])
        if self._cfg.voice_pitch >= 0:
            p_cmd.extend(["--voice-pitch", str(self._cfg.voice_pitch)])
        target_host = self._cfg.target_host.strip()
        if self._cfg.remote_http and target_host:
            p_cmd.append("--remote-http")
        if target_host:
            p_cmd.extend(["--target-host", target_host,
                           "--target-port", str(self._cfg.target_port),
                           "--target-endpoint", self._cfg.target_endpoint])
            if self._cfg.target_token:
                p_cmd.extend(["--target-token", self._cfg.target_token])
        p_cmd.extend([
            "--sd-prompt-begin-tag", str(self._cfg.sd_prompt_begin_tag or "[SD_PROMPT_BEGIN]"),
            "--sd-prompt-end-tag", str(self._cfg.sd_prompt_end_tag or "[SD_PROMPT_END]"),
        ])
        if self._cfg.sd_prompt_send_enabled:
            p_cmd.append("--sd-prompt-send-enabled")
            p_cmd.extend([
                "--sd-prompt-target-host", self._cfg.sd_prompt_target_host,
                "--sd-prompt-target-port", str(self._cfg.sd_prompt_target_port),
                "--sd-prompt-endpoint", self._cfg.sd_prompt_endpoint,
                "--sd-prompt-timeout", str(self._cfg.sd_prompt_timeout_sec),
                "--sd-prompt-model-checkpoint", self._cfg.sd_prompt_model_checkpoint,
                "--sd-prompt-vae", self._cfg.sd_prompt_vae,
                "--sd-prompt-clip-skip", str(self._cfg.sd_prompt_clip_skip),
                "--sd-prompt-append-prompt", self._cfg.sd_prompt_append_prompt,
                "--sd-prompt-negative-prompt", self._cfg.sd_prompt_negative_prompt,
                "--sd-prompt-steps", str(self._cfg.sd_prompt_steps),
                "--sd-prompt-width", str(self._cfg.sd_prompt_width),
                "--sd-prompt-height", str(self._cfg.sd_prompt_height),
                "--sd-prompt-cfg-scale", str(self._cfg.sd_prompt_cfg_scale),
                "--sd-prompt-sampler-name", self._cfg.sd_prompt_sampler_name,
                "--sd-prompt-scheduler", self._cfg.sd_prompt_scheduler,
                "--sd-prompt-seed", str(self._cfg.sd_prompt_seed),
                "--sd-prompt-subseed", str(self._cfg.sd_prompt_subseed),
                "--sd-prompt-subseed-strength", str(self._cfg.sd_prompt_subseed_strength),
                "--sd-prompt-batch-size", str(self._cfg.sd_prompt_batch_size),
                "--sd-prompt-n-iter", str(self._cfg.sd_prompt_n_iter),
                "--sd-prompt-hr-scale", str(self._cfg.sd_prompt_hr_scale),
                "--sd-prompt-hr-upscaler", self._cfg.sd_prompt_hr_upscaler,
                "--sd-prompt-hr-second-pass-steps", str(self._cfg.sd_prompt_hr_second_pass_steps),
                "--sd-prompt-denoising-strength", str(self._cfg.sd_prompt_denoising_strength),
                "--sd-prompt-hr-resize-x", str(self._cfg.sd_prompt_hr_resize_x),
                "--sd-prompt-hr-resize-y", str(self._cfg.sd_prompt_hr_resize_y),
                "--sd-prompt-hr-sampler-name", self._cfg.sd_prompt_hr_sampler_name,
                "--sd-prompt-hr-scheduler", self._cfg.sd_prompt_hr_scheduler,
                "--sd-prompt-hr-checkpoint-name", self._cfg.sd_prompt_hr_checkpoint_name,
                "--sd-prompt-hr-prompt", self._cfg.sd_prompt_hr_prompt,
                "--sd-prompt-hr-negative-prompt", self._cfg.sd_prompt_hr_negative_prompt,
                "--sd-prompt-extra-payload-json", self._cfg.sd_prompt_extra_payload_json,
            ])
            if self._cfg.sd_prompt_token:
                p_cmd.extend(["--sd-prompt-token", self._cfg.sd_prompt_token])
            if self._cfg.sd_prompt_restore_faces:
                p_cmd.append("--sd-prompt-restore-faces")
            if self._cfg.sd_prompt_tiling:
                p_cmd.append("--sd-prompt-tiling")
            if self._cfg.sd_prompt_save_images:
                p_cmd.append("--sd-prompt-save-images")
            if self._cfg.sd_prompt_send_images:
                p_cmd.append("--sd-prompt-send-images")
            if self._cfg.sd_prompt_enable_hr:
                p_cmd.append("--sd-prompt-enable-hr")
            if getattr(self._cfg, "sd_prompt_generate_forever", False):
                p_cmd.append("--sd-skip-send")
        if self._cfg.sbv2_model_file:
            p_cmd.extend(["--model-file", self._cfg.sbv2_model_file])
        if self._sbv2_use_http():
            sbv2_url = self._sbv2_server_url()
            if sbv2_url:
                p_cmd.extend(["--sbv2-server-url", sbv2_url])
        if combined_conv:
            p_cmd.extend(["--conversion-json", json.dumps(combined_conv, ensure_ascii=False)])
        # event-senderは同梱スクリプトを明示指定
        sender_ps1 = Path(__file__).resolve().parent.parent / "send_voice_face_event.ps1"
        if sender_ps1.exists():
            p_cmd.extend(["--event-sender", str(sender_ps1)])
        face_send_mode = str(getattr(self._cfg, "face_send_mode", "game_preset") or "game_preset").strip().lower()
        if face_send_mode not in ("game_preset", "preset_name", "preset_id"):
            face_send_mode = "game_preset"
        if face_send_mode == "preset_id":
            face_send_mode = "preset_name"
        p_cmd.extend(["--face-send-mode", face_send_mode])

        if face_send_mode == "preset_name":
            face_preset_name = str(getattr(self._cfg, "face_preset_name", "") or "").strip()
            face_preset_id = str(getattr(self._cfg, "face_preset_id", "") or "").strip()
            face_preset_random = bool(getattr(self._cfg, "face_preset_random", False))
            selected_name = face_preset_name
            selected_id = face_preset_id
            preset_path = self._studio_face_preset_json_path()
            if face_preset_random:
                p_cmd.append("--face-preset-random")
                selected_name = ""
                selected_id = ""
            if selected_name:
                p_cmd.extend(["--face-preset-name", selected_name])
            if selected_id:
                p_cmd.extend(["--face-preset-id", selected_id])
            if (not face_preset_random) and (not selected_name) and (not selected_id):
                raise RuntimeError("face_send_mode=preset_name ですが face_preset_name が未設定です")
            self.log.emit(
                "[event-face] "
                f"mode=preset_name random={int(face_preset_random)} "
                f"preset_name={face_preset_name or '(empty)'} "
                f"selected_name={selected_name or '(empty)'} "
                f"selected_id={selected_id or '(empty)'} "
                f"dropdown_ignored={int(face_preset_random)} "
                f"path={preset_path}"
            )
        else:
            if self._cfg.keep_current_face:
                p_cmd.append("--keep-current-face")
                self.log.emit("[event-face] mode=game_preset keep_current_face=1")
            elif self._cfg.face >= 0:
                p_cmd.extend(["--face", str(self._cfg.face)])
                self.log.emit(f"[event-face] mode=game_preset face={self._cfg.face}")
            else:
                self.log.emit("[event-face] mode=game_preset face=bridge_default")

        label = "手動" if manual else wav_name
        self.log.emit(f"[pipeline] {label}: {text[:40]}")
        self.log.emit(
            f"[grok-limit] request max={response_limit} enabled={int(self._cfg.max_response_chars_enabled)} text_len={len(text or '')}"
        )
        self.log.emit(
            f"[llm] backend={self._cfg.llm_backend or 'grok_browser'} "
            f"model={self._cfg.llm_model or '(default)'}"
        )
        try:
            p_ret = self._run_cmd(p_cmd, timeout_sec=420.0)
            if p_ret.returncode != 0:
                raise RuntimeError((p_ret.stderr or p_ret.stdout or "").strip())
            for ln in (p_ret.stdout or "").splitlines():
                if "conversion_" in ln:
                    self.log.emit(f"[tts-conv] {ln}")
            p_json = _last_json_line(p_ret.stdout)
            event_mode = str(p_json.get("event_face_send_mode", "") or "").strip()
            if event_mode:
                self.log.emit(
                    "[event-face][result] "
                    f"mode={event_mode} sent={int(bool(p_json.get('event_sent', False)))} "
                    f"preset_name={str(p_json.get('event_face_preset_name', '') or '').strip() or '(empty)'} "
                    f"preset_id={str(p_json.get('event_face_preset_id', '') or '').strip() or '(empty)'} "
                    f"random={int(bool(p_json.get('event_face_preset_random', False)))} "
                    f"picked_name={str(p_json.get('event_face_selected_name', '') or '').strip() or '(empty)'} "
                    f"picked_id={str(p_json.get('event_face_selected_id', '') or '').strip() or '(empty)'} "
                    f"face={int(p_json.get('event_face', -1) or -1)} "
                    f"keep_current_face={int(bool(p_json.get('event_keep_current_face', False)))}"
                )
            event_stderr = str(p_json.get("event_stderr", "") or "").strip()
            if event_stderr:
                self.log.emit(f"[event-face][stderr] {event_stderr[:240]}")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            merged_wav_raw = str(p_json.get("merged_wav", "")).strip()
            merged_wav_path = Path(merged_wav_raw) if merged_wav_raw else None
            sequence_event_raw = str(p_json.get("sequence_event_file", "")).strip()
            sequence_event_path = Path(sequence_event_raw) if sequence_event_raw else None
            run_dir = merged_wav_path.parent if merged_wav_path is not None else (sequence_event_path.parent if sequence_event_path is not None else None)
            sequence_sent = bool(p_json.get("sequence_sent", False))
            sequence_session_id = str(p_json.get("sequence_session_id", "") or "").strip()
            raw_len = int(p_json.get("response_raw_length", 0) or 0)
            capped_len = int(p_json.get("response_capped_length", 0) or 0)
            truncated = bool(p_json.get("response_truncated", False))
            max_chars = int(p_json.get("max_response_chars", self._cfg.max_response_chars) or self._cfg.max_response_chars)
            self.log.emit(
                f"[grok-limit] max={max_chars} raw_len={raw_len} capped_len={capped_len} truncated={int(truncated)}"
            )
            if truncated:
                self.log.emit(f"[grok-limit] truncated_chars={max(0, raw_len - capped_len)}")
            response_send = str(p_json.get("response", "") or "")
            response_display = str(p_json.get("response_display", "") or "")
            self.log.emit(
                f"[grok-limit] send_len={len(response_send)} display_len={len(response_display)} line_count={int(p_json.get('line_count', 0) or 0)}"
            )
            sd_prompt = str(p_json.get("sd_prompt", "") or "").strip()
            if sd_prompt:
                self.log.emit(f"[sd-prompt] detected len={len(sd_prompt)}")
                if getattr(self._cfg, "sd_prompt_generate_forever", False):
                    self._set_current_sd_prompt(sd_prompt)
                sd_result = p_json.get("sd_prompt_send_result", {})
                if isinstance(sd_result, dict) and sd_result:
                    self.log.emit(
                        "[sd-prompt] send "
                        f"ok={int(bool(sd_result.get('ok', False)))} "
                        f"status={int(sd_result.get('status', 0) or 0)} "
                        f"url={str(sd_result.get('url', '') or '')} "
                        f"error={str(sd_result.get('error', '') or '')[:120]}"
                    )
                saved_sd_prompt = self._append_session_text(
                    "sd_prompts",
                    sd_prompt,
                    label=f"source={wav_name}",
                )
                if saved_sd_prompt is not None:
                    p_json["sd_prompt_file"] = str(saved_sd_prompt)
                    self.log.emit(f"[save] sd_prompt: {saved_sd_prompt}")

            if self._cfg.save_sbv2_input_text:
                sbv2_input_text = ""
                line_texts = p_json.get("line_texts", [])
                if isinstance(line_texts, list):
                    lines = [str(v).strip() for v in line_texts if str(v).strip()]
                    if lines:
                        sbv2_input_text = "\n".join(lines)
                if not sbv2_input_text:
                    sbv2_input_text = str(p_json.get("response", "")).strip()
                if sbv2_input_text:
                    saved_sbv2_input = self._append_session_text(
                        "sbv2_inputs",
                        sbv2_input_text,
                        label=f"source={wav_name} lines={int(p_json.get('line_count', 0) or 0)}",
                    )
                    if saved_sbv2_input is not None:
                        p_json["sbv2_input_file"] = str(saved_sbv2_input)
                        self.log.emit(f"[save] sbv2_input_text: {saved_sbv2_input}")

            if self._cfg.save_sbv2_output_wav and merged_wav_path is not None:
                saved_sbv2_wav = self._copy_output_file(
                    merged_wav_path,
                    self._session_dir("sbv2_wavs"),
                    out_name=f"{stamp}.wav",
                )
                if saved_sbv2_wav is not None:
                    p_json["saved_merged_wav"] = str(saved_sbv2_wav)
                    self.log.emit(f"[save] sbv2_wav: {saved_sbv2_wav}")

            try:
                female_hold = float(p_json.get("total_wav_duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                female_hold = 0.0
            if female_hold <= 0.0:
                female_hold = _wav_duration_sec(p_json.get("merged_wav", ""))
            response_original = str(p_json.get("response_original", p_json.get("response", ""))).strip()
            response_send = str(p_json.get("response", response_original)).strip()
            response_display = str(p_json.get("response_display", response_original)).strip()
            if response_original and response_send == response_original:
                self.log.emit("[tts-conv] send unchanged (no hit or empty replacement)")
            if response_original and response_display == response_original:
                self.log.emit("[tts-conv] display unchanged (display_apply off or no hit)")
            if p_json.get("ok"):
                if response_display and not sequence_sent:
                    self._send_subtitle(response_display, wav_name, "StackFemale", hold_seconds=female_hold)
                elif response_display and sequence_sent:
                    self.log.emit("[subtitle] line subtitles are handled by VoiceFaceEventBridge sequence playback")
                self.log.emit(f"[done] {label}")
            else:
                self.log.emit(f"[error] pipeline: {p_json.get('error', '')}")
                response_original = ""
                response_display = ""
            # 動画切り替え信号は VoiceFaceEventBridge 側へ移管するため、Human_2_kks 側からは送信しない
            matched_indices = self._find_video_indices_from_response(response_original)
            if matched_indices:
                self.log.emit("[video] Human_2_kks からの動画切り替え送信は無効化中")
            # 生テキストをC#へ送信 → C#側でcoord/clothes検出・遅延実行
            if response_display:
                delay = female_hold if female_hold else 0.0
                self._schedule_response_text(response_display, self._cfg.main_index, delay, session_id=sequence_session_id)
            # TTS出力フォルダを削除
            if run_dir is not None and run_dir.exists() and not sequence_sent:
                shutil.rmtree(run_dir, ignore_errors=True)
            elif run_dir is not None and run_dir.exists():
                self.log.emit(f"[cleanup] keep sequence wav dir until playback ends: {run_dir}")
        except Exception as exc:
            msg = str(exc)
            lower = msg.lower()
            if ("10054" in msg) or ("10061" in msg) or ("connection reset" in lower) or ("connection refused" in lower):
                self._emit_sbv2_diagnostics("pipeline_http_error")
            self.log.emit(f"[error] pipeline {label}: {exc}")


# ---------------------------------------------------------------------------
# ホイール誤爆防止
# ---------------------------------------------------------------------------

