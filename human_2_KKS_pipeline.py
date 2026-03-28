from __future__ import annotations

import atexit
import ctypes
import random
import re
import shutil
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voice_gate_recorder import RecorderConfig, VoiceGateRecorder, get_input_devices

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
DEFAULT_SOURCE_MODE = "both"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _with_utf8_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _last_json_line(text: str) -> dict[str, Any]:
    for line in reversed((text or "").splitlines()):
        t = line.strip()
        if not t:
            continue
        try:
            return json.loads(t)
        except Exception:
            continue
    raise RuntimeError("No JSON payload in stdout")


def _wav_duration_sec(path: str) -> Optional[float]:
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return None


def _save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
    path.write_text(text, encoding="utf-8")


def _acquire_single_instance(mutex_name: str) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    handle = create_mutex(None, False, mutex_name)
    if not handle:
        return False
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        return False
    atexit.register(lambda: close_handle(handle))
    return True


# ---------------------------------------------------------------------------
# 設定データクラス
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    # 録音
    wav_dir: Path
    threshold_dbfs: float
    silence_seconds: float
    min_duration_seconds: float
    pre_roll_seconds: float
    post_roll_seconds: float
    device: Optional[int]
    # パイプライン
    kks_root: Path
    output_dir: Path
    faster_python: Path
    faster_model: str
    faster_device: str
    faster_compute: str
    faster_language: str
    faster_beam: int
    pipeline_python: Path
    sbv2_root: Path
    sbv2_model_name: str
    sbv2_model_file: str
    sbv2_speaker: str
    sbv2_style: str
    sbv2_length: float
    voice_volume: float
    voice_pitch: float
    pipe_name: str
    target_host: str
    target_port: int
    target_endpoint: str
    target_token: str
    remote_http: bool
    subtitle_send_enabled: bool
    subtitle_target_host: str
    subtitle_target_port: int
    subtitle_endpoint: str
    subtitle_token: str
    subtitle_timeout_sec: float
    main_index: int
    face: int
    keep_current_face: bool
    source_mode: str
    external_text_enabled: bool
    external_text_host: str
    external_text_port: int
    external_text_endpoint: str
    external_text_token: str
    external_text_dedupe_max: int
    # 転写サーバー
    transcribe_server_port: int = 18760
    # 動画メタデータ
    video_metadata_path: Optional[Path] = None
    # SBV2サーバー
    sbv2_server_url: str = "http://127.0.0.1:5000"
    sbv2_server_auto_start: bool = True
    # フィルター
    filter_phrases: list[str] = field(default_factory=list)
    # 文字起こし直後の変換辞書（FasterWhisper結果向け）
    transcribe_conversion_dict: list[dict] = field(default_factory=list)
    # 変換辞書
    conversion_dict: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ワーカー: 録音
# ---------------------------------------------------------------------------

class RecorderWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, config: RecorderConfig) -> None:
        super().__init__()
        self._config = config
        self._recorder: Optional[VoiceGateRecorder] = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._recorder = VoiceGateRecorder(self._config, log_callback=self.log.emit)
            self._recorder.run()
        except Exception:
            self.error.emit(traceback.format_exc())
        finally:
            self.finished.emit()

    def stop(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()

    def update_live_config(
        self,
        *,
        output_dir: Path,
        threshold_dbfs: float,
        silence_seconds: float,
        min_duration_seconds: float,
        pre_roll_seconds: float,
        post_roll_seconds: float,
        device: Optional[int],
    ) -> None:
        # Keep RecorderConfig in sync.
        self._config.output_dir = output_dir
        self._config.threshold_dbfs = threshold_dbfs
        self._config.silence_seconds = silence_seconds
        self._config.min_duration_seconds = min_duration_seconds
        self._config.pre_roll_seconds = pre_roll_seconds
        self._config.post_roll_seconds = post_roll_seconds
        self._config.device = device

        # Apply values immediately when recorder is already running.
        if self._recorder is None:
            return

        rec = self._recorder
        rec.cfg.output_dir = output_dir
        rec.cfg.threshold_dbfs = threshold_dbfs
        rec.cfg.silence_seconds = silence_seconds
        rec.cfg.min_duration_seconds = min_duration_seconds
        rec.cfg.pre_roll_seconds = pre_roll_seconds
        rec.cfg.post_roll_seconds = post_roll_seconds
        rec.cfg.device = device
        rec.silence_limit_samples = int(silence_seconds * rec.cfg.sample_rate)
        rec.pre_roll_limit_samples = int(pre_roll_seconds * rec.cfg.sample_rate)
        rec.post_roll_keep_samples = int(post_roll_seconds * rec.cfg.sample_rate)


# ---------------------------------------------------------------------------
# ワーカー: Selenium起動（非同期）
# ---------------------------------------------------------------------------

class _SeleniumWorker(QThread):
    result_ready = pyqtSignal(str, object)  # (status, driver)
    error_occurred = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        try:
            status, driver = self._func()
            self.result_ready.emit(status or "skipped", driver)
        except Exception as e:
            self.error_occurred.emit(str(e))


class _TaskWorker(QThread):
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        try:
            self.result_ready.emit(self._func())
        except Exception as e:
            self.error_occurred.emit(str(e))


# ワーカー: パイプライン（WAV監視 → 文字起こし → フィルター → Grok → SBV2 → KKS）
# ---------------------------------------------------------------------------

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
        self._current_proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._external_server: Optional[ThreadingHTTPServer] = None
        self._external_server_thread: Optional[threading.Thread] = None
        self._external_id_queue: deque[str] = deque()
        self._external_id_set: set[str] = set()
        self._external_lock = threading.Lock()
        # song_kana_map / video_metadata
        _data_dir = Path(__file__).resolve().parent / "data"
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

    @staticmethod
    def _strip_video_payload_prefix(text: str) -> str:
        # タイトル前に付きやすい装飾記号だけを除去する
        return (text or "").strip().lstrip(" 　「『\"'（([【<♡♥❤💗💖💓…。.、,:：;；!-")

    def stop(self) -> None:
        self._running = False
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
        self.log.emit(f"[pause] WAVキュー破棄: {drained}件")

    def resume(self) -> None:
        self._paused = False
        self.log.emit("[resume] 再開")

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

        if self._external_server_thread is not None:
            self._external_server_thread.join(timeout=2.0)
            self._external_server_thread = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._cfg.wav_dir.mkdir(parents=True, exist_ok=True)
            for sub in ("transcripts", "responses", "results", "grok_tts_outputs"):
                (self._cfg.output_dir / sub).mkdir(parents=True, exist_ok=True)

            self._start_transcribe_server()
            self._start_sbv2_server()
            self._start_external_text_server()

            self._observer = self._create_observer()
            self._observer.start()
            self.log.emit(f"[info] 監視開始: {self._cfg.wav_dir}")

            while self._running:
                # 手動テキスト優先
                try:
                    text = self._text_queue.get_nowait()
                    self._process_text(text, manual=True)
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
            self.error.emit(traceback.format_exc())
        finally:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=3.0)
            self._stop_external_text_server()
            self.log.emit("[info] パイプライン停止")
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
        return any(p.strip() and p.strip().lower() in lower for p in self._cfg.filter_phrases)

    def _run_cmd(self, cmd: list[str], timeout_sec: float) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict",
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
        local_root = Path(__file__).resolve().parent
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

        local_root = Path(__file__).resolve().parent
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
        if not self._cfg.sbv2_server_auto_start:
            return
        health_url = self._cfg.sbv2_server_url.rstrip("/") + "/models/info"
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
                        log_emit(f"[sbv2] {line}")
            except Exception:
                pass
        threading.Thread(target=_forward, daemon=True).start()

        deadline = time.time() + 300.0
        while time.time() < deadline and self._running:
            if proc.poll() is not None:
                raise RuntimeError("SBV2サーバーが異常終了しました")
            try:
                with urllib.request.urlopen(health_url, timeout=1.0):
                    self.log.emit("[sbv2] SBV2サーバー Ready")
                    return
            except Exception:
                time.sleep(1.0)
        raise RuntimeError("SBV2サーバーの起動タイムアウト (300秒)")

    def _transcribe_via_server(self, wav: Path) -> dict:
        url = f"{self._transcribe_server_url()}/transcribe"
        payload = json.dumps({
            "audio": str(wav),
            "language": self._cfg.faster_language,
            "beam_size": self._cfg.faster_beam,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _process_wav(self, wav: Path) -> None:
        if not self._wait_stable(wav):
            self.log.emit(f"[warn] 不安定/一時停止のためスキップ: {wav.name}")
            return
        if self._paused:
            self.log.emit(f"[pause] 破棄: {wav.name}")
            return

        try:
            self.log.emit(f"[transcribe] {wav.name}")
            t_json = self._transcribe_via_server(wav)
            if not t_json.get("ok"):
                raise RuntimeError(str(t_json.get("error", "transcribe failed")))
            text = str(t_json.get("text", "")).strip()
            if not text:
                self.log.emit(f"[info] 空テキスト: {wav.name}")
                return
            text = self._apply_transcribe_conversion(text)
            if not text:
                self.log.emit(f"[info] 変換後に空テキスト: {wav.name}")
                return
            _save_text(self._cfg.output_dir / "transcripts" / f"{wav.stem}.txt", text + "\n")

            if self._paused:
                self.log.emit(f"[pause] 破棄: {text[:40]}")
                return
            if self._is_filtered(text):
                self.log.emit(f"[filter] 除外: {text[:60]}")
                return
            self._process_text(text, wav=wav)
        except Exception as exc:
            self.log.emit(f"[error] {wav.name}: {exc}")
        finally:
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

    def _apply_transcribe_conversion(self, text: str) -> str:
        converted = text
        applied = 0
        with self._transcribe_conv_lock:
            rules = list(self._transcribe_conv_rules)
        for row in rules:
            if not isinstance(row, dict):
                continue
            src = str(row.get("from", ""))
            dst = str(row.get("to", ""))
            if not src:
                continue
            hit = converted.count(src)
            if hit <= 0:
                continue
            converted = converted.replace(src, dst)
            applied += 1
            self.log.emit(f"[stt-conv] '{src}' -> '{dst}' (hits={hit})")
        if applied > 0:
            self.log.emit(f"[stt-conv] applied_rules={applied}")
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

    def _schedule_response_text(self, text: str, main_index: int, delay_sec: float) -> None:
        """Grokの生テキストをそのままKKSへ送る（C#側でキーワードマッチ）"""
        sender_ps1 = self._cfg.kks_root / "work" / "tools" / "voice_face_event_pipe_tester" / "send_voice_face_event.ps1"
        pipe_name = self._cfg.pipe_name
        target_host = self._cfg.target_host.strip()
        remote_http = self._cfg.remote_http
        target_port = self._cfg.target_port
        target_endpoint = self._cfg.target_endpoint
        running_ref = lambda: self._running

        def _send():
            if not running_ref():
                return
            payload = json.dumps(
                {"type": "response_text", "text": text, "main": main_index, "delaySeconds": delay_sec or 0.0},
                ensure_ascii=False
            )
            cmd = [
                "powershell", "-ExecutionPolicy", "Bypass", "-File", str(sender_ps1),
                "-PipeName", pipe_name,
                "-Json", payload,
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

    def _process_text(self, text: str, wav: Optional[Path] = None, manual: bool = False) -> None:
        _, pipeline_script = self._resolve_scripts()
        wav_name = wav.name if wav else "manual"
        # 字幕は元テキストのまま送る
        self._send_subtitle(text, wav_name, "StackMale")

        # カナ変換ルールをconversion_jsonとして渡す（Grokレスポンスに適用される）
        kana_conv = [{"from": r[4], "to": r[1]} for r in self._kana_rules if r[4] and r[1]]
        combined_conv = kana_conv + list(self._cfg.conversion_dict or [])

        p_cmd = [
            str(self._cfg.pipeline_python), str(pipeline_script),
            "--text", text,
            "--sbv2-root", str(self._cfg.sbv2_root),
            "--model-name", self._cfg.sbv2_model_name,
            "--speaker", self._cfg.sbv2_speaker,
            "--style", self._cfg.sbv2_style,
            "--length", str(self._cfg.sbv2_length),
            "--output-dir", str(self._cfg.output_dir / "grok_tts_outputs"),
            "--pipe-name", self._cfg.pipe_name,
            "--main", str(self._cfg.main_index),
        ]
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
        if self._cfg.sbv2_model_file:
            p_cmd.extend(["--model-file", self._cfg.sbv2_model_file])
        if self._cfg.sbv2_server_url:
            p_cmd.extend(["--sbv2-server-url", self._cfg.sbv2_server_url])
        if combined_conv:
            p_cmd.extend(["--conversion-json", json.dumps(combined_conv, ensure_ascii=False)])
        if self._cfg.keep_current_face:
            p_cmd.append("--keep-current-face")
        elif self._cfg.face >= 0:
            p_cmd.extend(["--face", str(self._cfg.face)])

        label = "手動" if manual else wav_name
        self.log.emit(f"[pipeline] {label}: {text[:40]}")
        try:
            p_ret = self._run_cmd(p_cmd, timeout_sec=420.0)
            if p_ret.returncode != 0:
                raise RuntimeError((p_ret.stderr or p_ret.stdout or "").strip())
            p_json = _last_json_line(p_ret.stdout)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            _save_text(self._cfg.output_dir / "results" / f"{stamp}.json",
                       json.dumps(p_json, ensure_ascii=False, indent=2) + "\n")
            female_hold = _wav_duration_sec(p_json.get("merged_wav", ""))
            response_original = str(p_json.get("response_original", p_json.get("response", ""))).strip()
            response_display = str(p_json.get("response_display", response_original)).strip()
            if p_json.get("ok"):
                if response_display:
                    self._send_subtitle(response_display, wav_name, "StackFemale", hold_seconds=female_hold)
                self.log.emit(f"[done] {label}")
            else:
                self.log.emit(f"[error] pipeline: {p_json.get('error', '')}")
                response_original = ""
                response_display = ""
            # Grokの元レスポンス（変換前）から「今から流すね♡ ○○」を検出 → 動画切り替え
            matched_indices = self._find_video_indices_from_response(response_original)
            if matched_indices:
                self._increment_play_counts(matched_indices)
                delay = female_hold if female_hold else 0.0
                self._schedule_video_switch(matched_indices, delay)
            # 生テキストをC#へ送信 → C#側でcoord/clothes検出・遅延実行
            if response_display:
                delay = female_hold if female_hold else 0.0
                self._schedule_response_text(response_display, self._cfg.main_index, delay)
            # TTS出力フォルダを削除
            merged_wav = p_json.get("merged_wav", "")
            if merged_wav:
                run_dir = Path(merged_wav).parent
                if run_dir.exists():
                    shutil.rmtree(run_dir, ignore_errors=True)
        except Exception as exc:
            self.log.emit(f"[error] pipeline {label}: {exc}")


# ---------------------------------------------------------------------------
# ホイール誤爆防止
# ---------------------------------------------------------------------------

from PyQt6.QtCore import QEvent

class _NoWheelMixin:
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelSpinBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QSpinBox']).QSpinBox): pass
class _NoWheelDoubleSpinBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QDoubleSpinBox']).QDoubleSpinBox): pass
class _NoWheelComboBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QComboBox']).QComboBox): pass

class _NoWheelPortSpinBox(_NoWheelSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysSpinBox(_NoWheelSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysDoubleSpinBox(_NoWheelDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysComboBox(_NoWheelComboBox):
    def wheelEvent(self, event):
        event.ignore()


# ---------------------------------------------------------------------------
# メインウィンドウ
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Human → KKS Pipeline")
        self.resize(970, 720)

        self._recorder_thread: Optional[QThread] = None
        self._recorder_worker: Optional[RecorderWorker] = None
        self._pipeline_thread: Optional[QThread] = None
        self._pipeline_worker: Optional[PipelineWorker] = None
        self._running = False
        self._paused = False
        self._active_runtime_cfg: Optional[AppConfig] = None
        self._pending_cfg: Optional[AppConfig] = None
        self._last_deferred_live_fields: tuple[str, ...] = tuple()
        self._fw_test_stream = None
        self._fw_test_chunks: list[np.ndarray] = []
        self._fw_test_sr = 16000
        self._fw_test_worker: Optional[_TaskWorker] = None
        self._sbv2_test_worker: Optional[_TaskWorker] = None
        self._sbv2_test_last_wav: Optional[Path] = None

        self._manual_history: list[str] = []
        self._model_presets: list[dict] = []
        self._loading_config = False

        self._build_ui()
        self._load_config()
        self._install_autosave_hooks()

    # ---- UI構築 ----

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_recorder_tab()
        self._build_pipeline_tab()
        self._build_test_tab()
        self._build_selenium_tab()
        self._build_transcribe_conversion_tab()
        self._build_filter_tab()
        self._build_conversion_tab()

        # コントロールボタン
        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("▶ 開始")
        self.start_btn.setFixedHeight(44)
        self.start_btn.clicked.connect(self._on_start_stop)
        self.pause_btn = QPushButton("⏸ 一時停止")
        self.pause_btn.setFixedHeight(44)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)
        ctrl.addWidget(self.start_btn, 2)
        ctrl.addWidget(self.pause_btn, 1)
        layout.addLayout(ctrl)

        # モデルプリセット クイックボタン
        preset_btn_layout = QHBoxLayout()
        self._preset_btns: list[QPushButton] = []
        for i in range(4):
            btn = QPushButton(f"--- ({i+1})")
            btn.setFixedHeight(32)
            btn.setEnabled(False)
            idx = i
            btn.clicked.connect(lambda _, n=idx: self._apply_preset(n))
            preset_btn_layout.addWidget(btn)
            self._preset_btns.append(btn)
        layout.addLayout(preset_btn_layout)

        # 手動テキスト送信
        manual_group = QGroupBox("手動テキスト送信")
        manual_layout = QHBoxLayout(manual_group)
        self.manual_combo = _NoWheelComboBox()
        self.manual_combo.setEditable(True)
        self.manual_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.manual_combo.lineEdit().setPlaceholderText("テキストを入力して送信...")
        self.manual_combo.lineEdit().returnPressed.connect(self._send_manual)
        self.manual_btn = QPushButton("送信")
        self.manual_btn.clicked.connect(self._send_manual)
        manual_layout.addWidget(self.manual_combo, 1)
        manual_layout.addWidget(self.manual_btn)
        layout.addWidget(manual_group)

        # ログ
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

    def _build_recorder_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "録音設定")
        form = QFormLayout(tab)

        device_row = QHBoxLayout()
        self.device_combo = _NoWheelAlwaysComboBox()
        refresh_btn = QPushButton("更新")
        refresh_btn.clicked.connect(self._reload_devices)
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh_btn)
        form.addRow("入力デバイス", device_row)

        self.wav_dir_edit = QLineEdit(str(Path(__file__).resolve().parent / "outputs" / "wav"))
        wav_btn = QPushButton("参照")
        wav_btn.clicked.connect(lambda: self._pick_dir(self.wav_dir_edit, "WAV保存先"))
        form.addRow("WAV保存先", self._hrow(self.wav_dir_edit, wav_btn))

        self.threshold_spin = _NoWheelAlwaysDoubleSpinBox()
        self.threshold_spin.setRange(-90.0, 0.0); self.threshold_spin.setValue(-35.0)
        self.threshold_spin.setDecimals(1); self.threshold_spin.setSuffix(" dBFS")
        form.addRow("閾値", self.threshold_spin)

        self.silence_spin = _NoWheelAlwaysDoubleSpinBox()
        self.silence_spin.setRange(0.1, 10.0); self.silence_spin.setValue(2.0)
        self.silence_spin.setDecimals(1); self.silence_spin.setSuffix(" 秒")
        form.addRow("無音停止秒数", self.silence_spin)

        self.min_dur_spin = _NoWheelAlwaysDoubleSpinBox()
        self.min_dur_spin.setRange(0.1, 30.0); self.min_dur_spin.setValue(3.0)
        self.min_dur_spin.setDecimals(1); self.min_dur_spin.setSuffix(" 秒")
        form.addRow("最小保存秒数", self.min_dur_spin)

        self.pre_roll_spin = _NoWheelAlwaysDoubleSpinBox()
        self.pre_roll_spin.setRange(0.0, 5.0); self.pre_roll_spin.setValue(0.5)
        self.pre_roll_spin.setDecimals(1); self.pre_roll_spin.setSuffix(" 秒")
        form.addRow("開始前バッファ", self.pre_roll_spin)

        self.post_roll_spin = _NoWheelAlwaysDoubleSpinBox()
        self.post_roll_spin.setRange(0.0, 5.0); self.post_roll_spin.setValue(0.5)
        self.post_roll_spin.setDecimals(1); self.post_roll_spin.setSuffix(" 秒")
        form.addRow("停止後余韻", self.post_roll_spin)

        self._reload_devices()

    def _build_pipeline_tab(self) -> None:
        inner = QWidget()
        inner.setMinimumWidth(0)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        from PyQt6.QtCore import Qt
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "パイプライン設定")
        form = QFormLayout(inner)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.kks_root_edit = QLineEdit()
        kks_btn = QPushButton("参照")
        kks_btn.clicked.connect(lambda: self._pick_dir(self.kks_root_edit, "KKSフォルダ"))
        form.addRow("KKSフォルダ", self._hrow(self.kks_root_edit, kks_btn))

        self.output_dir_edit = QLineEdit(str(Path(__file__).resolve().parent / "outputs"))
        out_btn = QPushButton("参照")
        out_btn.clicked.connect(lambda: self._pick_dir(self.output_dir_edit, "出力先"))
        form.addRow("出力先", self._hrow(self.output_dir_edit, out_btn))

        _local_py = str(Path(__file__).resolve().parent / "python" / "python.exe")
        self.faster_python_edit = QLineEdit(_local_py)
        fp_btn = QPushButton("参照")
        fp_btn.clicked.connect(lambda: self._pick_file(self.faster_python_edit, "FasterWhisper Python"))
        form.addRow("FasterWhisper Python", self._hrow(self.faster_python_edit, fp_btn))

        whisper_row = QHBoxLayout()
        self.faster_model_edit = _NoWheelComboBox()
        self.faster_model_edit.setEditable(True)
        self.faster_model_edit.addItems(["large-v3", "large-v2", "large", "medium", "small", "base", "tiny"])
        self.faster_model_edit.setCurrentText("large-v3")
        self.faster_device_combo = _NoWheelComboBox()
        self.faster_device_combo.addItems(["auto", "cuda", "cpu"])
        self.faster_compute_combo = _NoWheelComboBox()
        self.faster_compute_combo.addItems(["int8_float16", "float16", "int8", "float32"])
        self.faster_lang_edit = QLineEdit("ja")
        self.faster_lang_edit.setMaximumWidth(40)
        self.faster_beam_spin = _NoWheelSpinBox()
        self.faster_beam_spin.setRange(1, 10); self.faster_beam_spin.setValue(1)
        whisper_row.addWidget(QLabel("model")); whisper_row.addWidget(self.faster_model_edit)
        whisper_row.addWidget(self.faster_device_combo)
        whisper_row.addWidget(self.faster_compute_combo)
        whisper_row.addWidget(QLabel("lang")); whisper_row.addWidget(self.faster_lang_edit)
        whisper_row.addWidget(QLabel("beam")); whisper_row.addWidget(self.faster_beam_spin)
        w = QWidget(); w.setLayout(whisper_row)
        form.addRow("Whisper", w)

        self.pipeline_python_edit = QLineEdit(_local_py)
        pp_btn = QPushButton("参照")
        pp_btn.clicked.connect(lambda: self._pick_file(self.pipeline_python_edit, "Grok/TTS Python"))
        form.addRow("Grok/TTS Python", self._hrow(self.pipeline_python_edit, pp_btn))

        self.sbv2_root_edit = QLineEdit()
        sbv2_btn = QPushButton("参照")
        sbv2_btn.clicked.connect(lambda: self._pick_dir(self.sbv2_root_edit, "SBV2フォルダ"))
        form.addRow("SBV2フォルダ", self._hrow(self.sbv2_root_edit, sbv2_btn))

        self.video_metadata_edit = QLineEdit("")
        meta_btn = QPushButton("参照")
        meta_btn.clicked.connect(lambda: self._pick_file(self.video_metadata_edit, "動画メタデータJSON"))
        form.addRow("動画メタデータJSON", self._hrow(self.video_metadata_edit, meta_btn))

        sbv2_server_row = QHBoxLayout()
        self.sbv2_server_url_edit = QLineEdit("http://127.0.0.1:5000")
        self.sbv2_auto_start_chk = QCheckBox("自動起動")
        self.sbv2_auto_start_chk.setChecked(True)
        sbv2_server_row.addWidget(self.sbv2_server_url_edit, 1)
        sbv2_server_row.addWidget(self.sbv2_auto_start_chk)
        sbv2_server_w = QWidget(); sbv2_server_w.setLayout(sbv2_server_row)
        form.addRow("SBV2サーバーURL", sbv2_server_w)

        model_row = QHBoxLayout()
        self.model_name_combo = _NoWheelComboBox(); self.model_name_combo.setEditable(True)
        self.model_file_edit = _NoWheelComboBox(); self.model_file_edit.setEditable(True)
        self.model_file_edit.lineEdit().setPlaceholderText("checkpoint file (空=auto)")
        model_refresh_btn = QPushButton("更新")
        model_refresh_btn.clicked.connect(self._reload_models)
        self.model_name_combo.currentTextChanged.connect(self._reload_model_files)
        model_row.addWidget(self.model_name_combo, 1); model_row.addWidget(self.model_file_edit, 1)
        model_row.addWidget(model_refresh_btn)
        m = QWidget(); m.setLayout(model_row)
        form.addRow("SBV2モデル", m)

        # モデルプリセット管理
        preset_row = QHBoxLayout()
        self.preset_name_edit = QLineEdit()
        self.preset_name_edit.setPlaceholderText("プリセット名")
        preset_save_btn = QPushButton("保存")
        preset_save_btn.clicked.connect(self._save_preset)
        self.preset_list_combo = _NoWheelComboBox()
        preset_apply_btn = QPushButton("適用")
        preset_apply_btn.clicked.connect(self._apply_preset_from_combo)
        preset_del_btn = QPushButton("削除")
        preset_del_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.preset_name_edit, 2)
        preset_row.addWidget(preset_save_btn)
        preset_row.addWidget(self.preset_list_combo, 2)
        preset_row.addWidget(preset_apply_btn)
        preset_row.addWidget(preset_del_btn)
        pr = QWidget(); pr.setLayout(preset_row)
        form.addRow("モデルプリセット", pr)

        opt_row = QHBoxLayout()
        self.speaker_edit = QLineEdit("0")
        self.style_edit = QLineEdit("Neutral")
        self.length_spin = _NoWheelAlwaysDoubleSpinBox()
        self.length_spin.setRange(0.1, 3.0); self.length_spin.setValue(1.0); self.length_spin.setDecimals(2)
        self.voice_volume_spin = _NoWheelAlwaysDoubleSpinBox()
        self.voice_volume_spin.setRange(-1.0, 1.0); self.voice_volume_spin.setValue(-1.0); self.voice_volume_spin.setDecimals(2)
        self.voice_pitch_spin = _NoWheelAlwaysDoubleSpinBox()
        self.voice_pitch_spin.setRange(-1.0, 3.0); self.voice_pitch_spin.setValue(-1.0); self.voice_pitch_spin.setDecimals(2)
        self.pipe_edit = QLineEdit("kks_voice_face_events")
        self.main_spin = _NoWheelAlwaysSpinBox(); self.main_spin.setRange(0, 3)
        self.face_spin = _NoWheelAlwaysSpinBox(); self.face_spin.setRange(-1, 500); self.face_spin.setValue(-1)
        self.keep_face_chk = QCheckBox("現在表情維持"); self.keep_face_chk.setChecked(True)
        for label, widget in [("speaker", self.speaker_edit), ("style", self.style_edit),
                               ("length", self.length_spin), ("vol", self.voice_volume_spin),
                               ("pitch", self.voice_pitch_spin), ("pipe", self.pipe_edit),
                               ("main", self.main_spin), ("face", self.face_spin)]:
            opt_row.addWidget(QLabel(label)); opt_row.addWidget(widget)
        opt_row.addWidget(self.keep_face_chk)
        o = QWidget(); o.setLayout(opt_row)
        form.addRow("送信設定", o)

        net_row = QHBoxLayout()
        self.target_host_edit = QLineEdit()
        self.target_host_edit.setPlaceholderText("空欄=ローカルPipe")
        self.target_port_spin = _NoWheelPortSpinBox()
        self.target_port_spin.setRange(1, 65535); self.target_port_spin.setValue(18765)
        self.target_endpoint_edit = QLineEdit("/voice-face-event")
        self.target_token_edit = QLineEdit()
        self.target_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remote_http_chk = QCheckBox("HTTP強制")
        net_row.addWidget(QLabel("host")); net_row.addWidget(self.target_host_edit, 2)
        net_row.addWidget(QLabel("port")); net_row.addWidget(self.target_port_spin)
        net_row.addWidget(QLabel("endpoint")); net_row.addWidget(self.target_endpoint_edit)
        net_row.addWidget(QLabel("token")); net_row.addWidget(self.target_token_edit)
        net_row.addWidget(self.remote_http_chk)
        net = QWidget(); net.setLayout(net_row)
        form.addRow("LAN送信", net)

        sub_row = QHBoxLayout()
        self.subtitle_send_chk = QCheckBox("字幕送信"); self.subtitle_send_chk.setChecked(True)
        self.subtitle_host_edit = QLineEdit("127.0.0.1")
        self.subtitle_port_spin = _NoWheelPortSpinBox()
        self.subtitle_port_spin.setRange(1, 65535); self.subtitle_port_spin.setValue(18766)
        self.subtitle_endpoint_edit = QLineEdit("/subtitle-event")
        self.subtitle_token_edit = QLineEdit()
        self.subtitle_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subtitle_timeout_spin = _NoWheelSpinBox()
        self.subtitle_timeout_spin.setRange(1, 30); self.subtitle_timeout_spin.setValue(5)
        sub_row.addWidget(self.subtitle_send_chk)
        sub_row.addWidget(QLabel("host")); sub_row.addWidget(self.subtitle_host_edit, 2)
        sub_row.addWidget(QLabel("port")); sub_row.addWidget(self.subtitle_port_spin)
        sub_row.addWidget(QLabel("endpoint")); sub_row.addWidget(self.subtitle_endpoint_edit)
        sub_row.addWidget(QLabel("token")); sub_row.addWidget(self.subtitle_token_edit)
        sub_row.addWidget(QLabel("timeout")); sub_row.addWidget(self.subtitle_timeout_spin)
        sub = QWidget(); sub.setLayout(sub_row)
        form.addRow("字幕送信", sub)

        ext_row = QHBoxLayout()
        self.source_mode_combo = _NoWheelComboBox()
        self.source_mode_combo.addItems(["external", "mic", "both"])
        self.source_mode_combo.setCurrentText(DEFAULT_SOURCE_MODE)
        self.external_text_chk = QCheckBox("外部受信")
        self.external_text_chk.setChecked(True)
        self.external_text_host_edit = QLineEdit("127.0.0.1")
        self.external_text_port_spin = _NoWheelPortSpinBox()
        self.external_text_port_spin.setRange(1, 65535); self.external_text_port_spin.setValue(18767)
        self.external_text_endpoint_edit = QLineEdit("/manual-text")
        self.external_text_token_edit = QLineEdit()
        self.external_text_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.external_text_dedupe_spin = _NoWheelSpinBox()
        self.external_text_dedupe_spin.setRange(10, 10000); self.external_text_dedupe_spin.setValue(1024)
        ext_row.addWidget(QLabel("mode")); ext_row.addWidget(self.source_mode_combo)
        ext_row.addWidget(self.external_text_chk)
        ext_row.addWidget(QLabel("host")); ext_row.addWidget(self.external_text_host_edit, 2)
        ext_row.addWidget(QLabel("port")); ext_row.addWidget(self.external_text_port_spin)
        ext_row.addWidget(QLabel("endpoint")); ext_row.addWidget(self.external_text_endpoint_edit)
        ext_row.addWidget(QLabel("token")); ext_row.addWidget(self.external_text_token_edit)
        ext_row.addWidget(QLabel("dedupe")); ext_row.addWidget(self.external_text_dedupe_spin)
        ext = QWidget(); ext.setLayout(ext_row)
        form.addRow("外部テキスト受信", ext)

    def _build_test_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "テスト")
        layout = QVBoxLayout(tab)

        fw_group = QGroupBox("FasterWhisper テスト")
        fw_layout = QVBoxLayout(fw_group)
        fw_layout.addWidget(QLabel("ボタンを押している間だけ録音。離すと文字起こしします。"))
        self.fw_test_hold_btn = QPushButton("押して話す（離して判定）")
        self.fw_test_hold_btn.pressed.connect(self._fw_test_start_record)
        self.fw_test_hold_btn.released.connect(self._fw_test_stop_record)
        fw_layout.addWidget(self.fw_test_hold_btn)
        self.fw_test_status_label = QLabel("待機")
        fw_layout.addWidget(self.fw_test_status_label)
        self.fw_test_result_edit = QPlainTextEdit()
        self.fw_test_result_edit.setReadOnly(True)
        self.fw_test_result_edit.setPlaceholderText("ここに文字起こし結果が表示されます。")
        fw_layout.addWidget(self.fw_test_result_edit, 1)
        layout.addWidget(fw_group)

        sbv2_group = QGroupBox("SBV2 テスト")
        sbv2_layout = QVBoxLayout(sbv2_group)
        self.sbv2_test_text_edit = QPlainTextEdit()
        self.sbv2_test_text_edit.setPlaceholderText("SBV2で再生したいテキストを入力")
        sbv2_layout.addWidget(self.sbv2_test_text_edit)

        face_row = QHBoxLayout()
        self.sbv2_test_keep_face_chk = QCheckBox("現在表情維持")
        self.sbv2_test_keep_face_chk.setChecked(True)
        self.sbv2_test_keep_face_chk.toggled.connect(self._on_sbv2_test_keep_face_toggled)
        self.sbv2_test_face_spin = _NoWheelAlwaysSpinBox()
        self.sbv2_test_face_spin.setRange(-1, 500)
        self.sbv2_test_face_spin.setValue(-1)
        self.sbv2_test_face_spin.setEnabled(False)
        self.sbv2_test_face_spin.valueChanged.connect(self._on_sbv2_test_face_changed)
        face_row.addWidget(self.sbv2_test_keep_face_chk)
        face_row.addWidget(QLabel("face"))
        face_row.addWidget(self.sbv2_test_face_spin)
        face_row.addStretch()
        sbv2_layout.addLayout(face_row)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("音量"))
        self.sbv2_test_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.sbv2_test_volume_slider.setRange(0, 100)
        self.sbv2_test_volume_slider.setValue(100)
        self.sbv2_test_volume_slider.valueChanged.connect(self._on_sbv2_test_volume_changed)
        self.sbv2_test_volume_label = QLabel("100%")
        vol_row.addWidget(self.sbv2_test_volume_slider, 1)
        vol_row.addWidget(self.sbv2_test_volume_label)
        sbv2_layout.addLayout(vol_row)

        btn_row = QHBoxLayout()
        self.sbv2_test_run_btn = QPushButton("SBV2テスト実行")
        self.sbv2_test_run_btn.clicked.connect(self._run_sbv2_test)
        self.sbv2_test_play_btn = QPushButton("最後の音声をGUI再生")
        self.sbv2_test_play_btn.clicked.connect(self._play_last_sbv2_test)
        btn_row.addWidget(self.sbv2_test_run_btn)
        btn_row.addWidget(self.sbv2_test_play_btn)
        btn_row.addStretch()
        sbv2_layout.addLayout(btn_row)

        self.sbv2_test_status_label = QLabel("待機")
        sbv2_layout.addWidget(self.sbv2_test_status_label)
        layout.addWidget(sbv2_group, 1)

    def _on_sbv2_test_keep_face_toggled(self, checked: bool) -> None:
        self.sbv2_test_face_spin.setEnabled(not checked)

    def _on_sbv2_test_face_changed(self, value: int) -> None:
        if value >= 0 and self.sbv2_test_keep_face_chk.isChecked():
            self.sbv2_test_keep_face_chk.setChecked(False)

    def _on_sbv2_test_volume_changed(self, value: int) -> None:
        self.sbv2_test_volume_label.setText(f"{value}%")

    def _fw_test_audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            return
        self._fw_test_chunks.append(indata.copy())

    @staticmethod
    def _write_wav_float32_mono(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
        pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
        clipped = np.clip(pcm, -1.0, 1.0)
        int16_pcm = (clipped * 32767.0).astype(np.int16)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int16_pcm.tobytes())

    def _fw_test_start_record(self) -> None:
        if self._fw_test_worker is not None and self._fw_test_worker.isRunning():
            self.fw_test_status_label.setText("文字起こし中...")
            return
        if self._fw_test_stream is not None:
            return
        self._fw_test_chunks = []
        device = self.device_combo.currentData()
        try:
            self._fw_test_stream = sd.InputStream(
                samplerate=self._fw_test_sr,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._fw_test_audio_callback,
            )
            self._fw_test_stream.start()
            self.fw_test_hold_btn.setText("録音中（離して停止）")
            self.fw_test_status_label.setText("録音中...")
        except Exception as exc:
            self._fw_test_stream = None
            self.fw_test_hold_btn.setText("押して話す（離して判定）")
            self.fw_test_status_label.setText(f"録音失敗: {exc}")
            self._append_log(f"[fw-test] record start failed: {exc}")

    def _fw_test_stop_record(self) -> None:
        if self._fw_test_stream is None:
            return
        try:
            self._fw_test_stream.stop()
            self._fw_test_stream.close()
        except Exception:
            pass
        finally:
            self._fw_test_stream = None

        self.fw_test_hold_btn.setText("押して話す（離して判定）")
        if not self._fw_test_chunks:
            self.fw_test_status_label.setText("録音データなし")
            return

        pcm = np.concatenate(self._fw_test_chunks, axis=0).reshape(-1)
        self._fw_test_chunks = []
        duration = len(pcm) / float(self._fw_test_sr)
        if duration < 0.2:
            self.fw_test_status_label.setText("録音が短すぎます")
            return

        out_root = Path(self.output_dir_edit.text().strip()).expanduser().resolve() / "tests" / "fasterwhisper"
        wav_path = out_root / f"hold_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.wav"
        self._write_wav_float32_mono(wav_path, pcm, self._fw_test_sr)
        self.fw_test_status_label.setText("文字起こし中...")
        self._append_log(f"[fw-test] recorded {wav_path.name} ({duration:.2f}s)")
        self._fw_test_worker = _TaskWorker(lambda: self._run_fw_test_transcribe(wav_path))
        self._fw_test_worker.result_ready.connect(self._on_fw_test_transcribe_done)
        self._fw_test_worker.error_occurred.connect(self._on_fw_test_transcribe_error)
        self._fw_test_worker.start()

    def _run_fw_test_transcribe(self, wav_path: Path) -> dict:
        cfg = self._build_config()
        script = Path(__file__).resolve().parent / "run_transcribe_one_wav.py"
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")
        cmd = [
            str(cfg.faster_python), str(script),
            "--audio", str(wav_path),
            "--model", cfg.faster_model,
            "--device", cfg.faster_device,
            "--compute-type", cfg.faster_compute,
            "--language", cfg.faster_language,
            "--beam-size", str(cfg.faster_beam),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=_with_utf8_env(),
        )
        payload = _last_json_line(proc.stdout or "")
        payload["audio_path"] = str(wav_path)
        payload["returncode"] = proc.returncode
        if proc.returncode != 0 and payload.get("ok", False):
            payload["ok"] = False
            payload["error"] = (proc.stderr or proc.stdout or "").strip()
        return payload

    def _on_fw_test_transcribe_done(self, payload: object) -> None:
        self._fw_test_worker = None
        data = payload if isinstance(payload, dict) else {}
        if data.get("ok"):
            text = str(data.get("text", "")).strip()
            self.fw_test_result_edit.setPlainText(text or "(空テキスト)")
            self.fw_test_status_label.setText("完了")
            self._append_log(f"[fw-test] done: {text[:80]}")
        else:
            err = str(data.get("error", "unknown error"))
            self.fw_test_result_edit.setPlainText("")
            self.fw_test_status_label.setText("失敗")
            self._append_log(f"[fw-test] failed: {err}")

    def _on_fw_test_transcribe_error(self, err: str) -> None:
        self._fw_test_worker = None
        self.fw_test_status_label.setText("失敗")
        self._append_log(f"[fw-test] worker error: {err}")

    @staticmethod
    def _is_local_kks_running() -> bool:
        ps = (
            "$p=Get-Process -ErrorAction SilentlyContinue | "
            "Where-Object { $_.ProcessName -like '*KoikatsuSunshine*' -or $_.ProcessName -like '*CharaStudio*' }; "
            "if($p){'1'}else{'0'}"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return proc.returncode == 0 and (proc.stdout or "").strip().startswith("1")
        except Exception:
            return False

    @staticmethod
    def _load_wav_as_float(path: Path) -> tuple[np.ndarray, int]:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if width == 2:
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 1:
            data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 4:
            data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"unsupported wav sample width: {width}")
        if channels > 1:
            data = data.reshape(-1, channels)
        return data, rate

    def _play_wav_in_gui(self, wav_path: Path) -> bool:
        try:
            data, sample_rate = self._load_wav_as_float(wav_path)
            volume = float(self.sbv2_test_volume_slider.value()) / 100.0
            data = np.clip(data * volume, -1.0, 1.0)
            sd.stop()
            sd.play(data, sample_rate, blocking=False)
            return True
        except Exception as exc:
            self._append_log(f"[sbv2-test] gui play failed: {exc}")
            return False

    def _play_last_sbv2_test(self) -> None:
        if self._sbv2_test_last_wav is None or (not self._sbv2_test_last_wav.exists()):
            self.sbv2_test_status_label.setText("再生可能な音声がありません")
            return
        if self._play_wav_in_gui(self._sbv2_test_last_wav):
            self.sbv2_test_status_label.setText("GUI再生中")

    def _resolve_event_sender_script(self, cfg: AppConfig) -> Path:
        local = Path(__file__).resolve().parent / "send_voice_face_event.ps1"
        if local.exists():
            return local
        return cfg.kks_root / "work" / "tools" / "voice_face_event_pipe_tester" / "send_voice_face_event.ps1"

    def _send_sbv2_test_event(self, cfg: AppConfig, wav_path: Path) -> tuple[bool, str]:
        sender_ps1 = self._resolve_event_sender_script(cfg)
        if not sender_ps1.exists():
            return False, f"sender not found: {sender_ps1}"

        cmd = [
            "powershell", "-ExecutionPolicy", "Bypass", "-File", str(sender_ps1),
            "-PipeName", cfg.pipe_name,
            "-Main", str(cfg.main_index),
            "-AudioPath", str(wav_path),
        ]
        if cfg.remote_http or cfg.target_host.strip():
            cmd.append("-RemoteHttp")
        if cfg.target_host.strip():
            cmd.extend(["-TargetHost", cfg.target_host.strip()])
            cmd.extend(["-TargetPort", str(cfg.target_port)])
            cmd.extend(["-TargetEndpoint", cfg.target_endpoint])
            if cfg.target_token.strip():
                cmd.extend(["-TargetToken", cfg.target_token.strip()])

        if self.sbv2_test_keep_face_chk.isChecked():
            cmd.append("-KeepCurrentFace")
        else:
            face = int(self.sbv2_test_face_spin.value())
            if face >= 0:
                cmd.extend(["-Face", str(face)])
            else:
                cmd.append("-KeepCurrentFace")

        volume = float(self.sbv2_test_volume_slider.value()) / 100.0
        cmd.extend(["-Volume", f"{volume:.2f}"])
        if cfg.voice_pitch >= 0:
            cmd.extend(["-Pitch", str(cfg.voice_pitch)])

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=_with_utf8_env(),
        )
        detail = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, detail

    def _run_sbv2_test_task(self, cfg: AppConfig, text: str) -> dict:
        script = Path(__file__).resolve().parent / "run_grok_tts_event.py"
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")
        cmd = [
            str(cfg.pipeline_python), str(script),
            "--response-text", text,
            "--sbv2-root", str(cfg.sbv2_root),
            "--model-name", cfg.sbv2_model_name,
            "--speaker", cfg.sbv2_speaker,
            "--style", cfg.sbv2_style,
            "--length", str(cfg.sbv2_length),
            "--output-dir", str(cfg.output_dir / "grok_tts_outputs"),
            "--pipe-name", cfg.pipe_name,
            "--main", str(cfg.main_index),
            "--no-send-event",
        ]
        if cfg.sbv2_model_file:
            cmd.extend(["--model-file", cfg.sbv2_model_file])
        if cfg.sbv2_server_url:
            cmd.extend(["--sbv2-server-url", cfg.sbv2_server_url])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=_with_utf8_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip())
        payload = _last_json_line(proc.stdout or "")
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error", "sbv2 test failed")))
        payload["returncode"] = proc.returncode
        return payload

    def _run_sbv2_test(self) -> None:
        if self._sbv2_test_worker is not None and self._sbv2_test_worker.isRunning():
            self.sbv2_test_status_label.setText("実行中...")
            return
        text = self.sbv2_test_text_edit.toPlainText().strip()
        if not text:
            self.sbv2_test_status_label.setText("テキスト未入力")
            return
        try:
            cfg = self._build_config()
        except Exception as exc:
            self.sbv2_test_status_label.setText("設定エラー")
            self._append_log(f"[sbv2-test] config error: {exc}")
            return
        self.sbv2_test_status_label.setText("音声生成中...")
        self._sbv2_test_worker = _TaskWorker(lambda: self._run_sbv2_test_task(cfg, text))
        self._sbv2_test_worker.result_ready.connect(self._on_sbv2_test_done)
        self._sbv2_test_worker.error_occurred.connect(self._on_sbv2_test_error)
        self._sbv2_test_worker.start()

    def _on_sbv2_test_done(self, payload: object) -> None:
        self._sbv2_test_worker = None
        data = payload if isinstance(payload, dict) else {}
        merged_wav = Path(str(data.get("merged_wav", ""))).resolve()
        if not merged_wav.exists():
            self.sbv2_test_status_label.setText("音声ファイルなし")
            self._append_log("[sbv2-test] merged wav not found")
            return

        self._sbv2_test_last_wav = merged_wav
        try:
            cfg = self._active_runtime_cfg if self._active_runtime_cfg is not None else self._build_config()
        except Exception as exc:
            self.sbv2_test_status_label.setText("設定エラー")
            self._append_log(f"[sbv2-test] config error: {exc}")
            return
        if self._is_local_kks_running():
            ok, detail = self._send_sbv2_test_event(cfg, merged_wav)
            if ok:
                self.sbv2_test_status_label.setText("KKS送信完了")
                self._append_log("[sbv2-test] sent to KKS")
                return
            self._append_log(f"[sbv2-test] KKS send failed: {detail}")

        if self._play_wav_in_gui(merged_wav):
            self.sbv2_test_status_label.setText("KKS未起動: GUI再生中")
            self._append_log(f"[sbv2-test] local play: {merged_wav.name}")
        else:
            self.sbv2_test_status_label.setText("GUI再生失敗")

    def _on_sbv2_test_error(self, err: str) -> None:
        self._sbv2_test_worker = None
        self.sbv2_test_status_label.setText("失敗")
        self._append_log(f"[sbv2-test] error: {err}")

    def _build_conversion_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "変換辞書")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("TTS前に適用するテキスト変換（上から順に適用）:"))

        self.conversion_table = QTableWidget(0, 3)
        self.conversion_table.setHorizontalHeaderLabels(["変換前", "変換後", "表示適用"])
        self.conversion_table.horizontalHeader().setStretchLastSection(True)
        self.conversion_table.setColumnWidth(0, 250)
        self.conversion_table.setColumnWidth(1, 250)
        self.conversion_table.setColumnWidth(2, 110)
        layout.addWidget(self.conversion_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("行を追加")
        add_btn.clicked.connect(self._conv_add_row)
        del_btn = QPushButton("選択行を削除")
        del_btn.clicked.connect(self._conv_del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _conv_add_row(self) -> None:
        table = self.conversion_table
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.setItem(row, 2, self._new_display_apply_item(False))
        table.blockSignals(False)
        table.setCurrentCell(row, 0)
        table.scrollToItem(table.item(row, 0))
        table.editItem(table.item(row, 0))
        self._on_any_setting_changed()

    def _conv_del_row(self) -> None:
        table = self.conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._on_any_setting_changed()

    @staticmethod
    def _new_display_apply_item(checked: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def _build_transcribe_conversion_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "文字起こし変換")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("FasterWhisper結果に適用するテキスト変換（上から順に適用）:"))

        self.transcribe_conversion_table = QTableWidget(0, 2)
        self.transcribe_conversion_table.setHorizontalHeaderLabels(["変換前", "変換後"])
        self.transcribe_conversion_table.horizontalHeader().setStretchLastSection(True)
        self.transcribe_conversion_table.setColumnWidth(0, 250)
        self.transcribe_conversion_table.itemChanged.connect(self._on_transcribe_conv_item_changed)
        layout.addWidget(self.transcribe_conversion_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("行を追加")
        add_btn.clicked.connect(self._transcribe_conv_add_row)
        del_btn = QPushButton("選択行を削除")
        del_btn.clicked.connect(self._transcribe_conv_del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _transcribe_conv_add_row(self) -> None:
        table = self.transcribe_conversion_table
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.blockSignals(False)
        table.setCurrentCell(row, 0)
        table.scrollToItem(table.item(row, 0))
        table.editItem(table.item(row, 0))
        self._on_live_setting_changed()

    def _transcribe_conv_del_row(self) -> None:
        table = self.transcribe_conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._on_live_setting_changed()

    def _on_transcribe_conv_item_changed(self, _item: QTableWidgetItem) -> None:
        if self._loading_config:
            return
        self._on_live_setting_changed()

    def _build_selenium_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "Selenium")
        layout = QVBoxLayout(tab)

        # プロファイル選択
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Chromeプロファイル:"))
        self.chrome_profile_combo = QComboBox()
        profile_row.addWidget(self.chrome_profile_combo, 1)
        refresh_btn = QPushButton("更新")
        refresh_btn.clicked.connect(self._refresh_chrome_profiles)
        profile_row.addWidget(refresh_btn)
        layout.addLayout(profile_row)

        # ポート設定
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("デバッグポート:"))
        self.chrome_port_spin = QSpinBox()
        self.chrome_port_spin.setRange(1024, 65535)
        self.chrome_port_spin.setValue(9222)
        port_row.addWidget(self.chrome_port_spin)
        self.chrome_headless_chk = QCheckBox("ヘッドレス")
        port_row.addWidget(self.chrome_headless_chk)
        port_row.addStretch()
        layout.addLayout(port_row)

        # ボタン行
        btn_row = QHBoxLayout()
        self.chrome_launch_btn = QPushButton("Chrome起動")
        self.chrome_launch_btn.clicked.connect(self._do_chrome_launch)
        btn_row.addWidget(self.chrome_launch_btn)

        self.chrome_connect_btn = QPushButton("Selenium接続")
        self.chrome_connect_btn.clicked.connect(self._do_chrome_launch)
        self.chrome_connect_btn.setEnabled(False)
        btn_row.addWidget(self.chrome_connect_btn)

        self.chrome_close_btn = QPushButton("Chrome終了")
        self.chrome_close_btn.clicked.connect(self._do_chrome_close)
        self.chrome_close_btn.setEnabled(False)
        btn_row.addWidget(self.chrome_close_btn)
        layout.addLayout(btn_row)

        # テストボタン
        test_row = QHBoxLayout()
        self.chrome_test_btn = QPushButton("Grokを開く（テスト）")
        self.chrome_test_btn.clicked.connect(self._do_chrome_test_grok)
        self.chrome_test_btn.setEnabled(False)
        test_row.addWidget(self.chrome_test_btn)
        layout.addLayout(test_row)

        # ステータス
        self.chrome_status_label = QLabel("")
        layout.addWidget(self.chrome_status_label)

        layout.addStretch()

        # 初期化
        self._chrome_driver = None
        self._refresh_chrome_profiles()

    def _refresh_chrome_profiles(self) -> None:
        self.chrome_profile_combo.clear()
        try:
            from chrome_debug import get_profiles
            profiles = get_profiles()
            for p in profiles:
                display = f"{p['profile_dir']}: {p['email'] or '(未ログイン)'}"
                self.chrome_profile_combo.addItem(display, p["profile_dir"])
            self.chrome_status_label.setText(f"{len(profiles)}個のプロファイルを検出")
        except Exception as e:
            self.chrome_status_label.setText(f"エラー: {e}")

    def _is_chrome_debug_running(self, port: int) -> bool:
        """指定ポートでデバッグChromeが起動済みか確認"""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _find_grok_tab(self) -> bool:
        """既存タブからgrok.comを探してアクティブにする"""
        if not self._chrome_driver:
            return False
        try:
            for handle in self._chrome_driver.window_handles:
                self._chrome_driver.switch_to.window(handle)
                if "grok" in self._chrome_driver.current_url.lower():
                    self.chrome_status_label.setText(f"Grokタブ検出: {self._chrome_driver.current_url}")
                    return True
        except Exception:
            pass
        return False

    def _do_chrome_launch(self) -> None:
        port = self.chrome_port_spin.value()
        headless = self.chrome_headless_chk.isChecked()
        profile_dir = str(self.chrome_profile_combo.currentData() or "").strip()
        self.chrome_launch_btn.setEnabled(False)
        self.chrome_status_label.setText("Chrome起動中...")

        def _task(**kwargs):
            from chrome_debug import launch_chrome, get_driver
            already_running = False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                    already_running = resp.status == 200
            except Exception:
                pass
            if already_running:
                return "existing", get_driver(port=port)
            else:
                driver = launch_chrome(port=port, headless=headless, profile_dir=profile_dir)
                return "launched", driver

        self._chrome_tab_worker = _SeleniumWorker(_task)
        self._chrome_tab_worker.result_ready.connect(self._on_chrome_tab_done)
        self._chrome_tab_worker.error_occurred.connect(self._on_chrome_tab_error)
        self._chrome_tab_worker.start()

    def _on_chrome_tab_done(self, status, driver) -> None:
        self._chrome_driver = driver
        self.chrome_close_btn.setEnabled(True)
        self.chrome_test_btn.setEnabled(True)
        self.chrome_connect_btn.setEnabled(False)
        if self._find_grok_tab():
            pass  # ステータスは_find_grok_tab内で設定済み
        elif status == "existing":
            self.chrome_status_label.setText("既存Chrome接続完了（Grokタブなし）")
        else:
            self.chrome_status_label.setText("Chrome起動＋Selenium接続完了（Grokタブなし）")

    def _on_chrome_tab_error(self, err) -> None:
        self.chrome_status_label.setText(f"起動エラー: {err}")
        self.chrome_launch_btn.setEnabled(True)

    def _do_chrome_close(self) -> None:
        try:
            from chrome_debug import close_chrome
            close_chrome()
            self._chrome_driver = None
            self.chrome_status_label.setText("Chrome終了")
            self.chrome_launch_btn.setEnabled(True)
            self.chrome_connect_btn.setEnabled(False)
            self.chrome_close_btn.setEnabled(False)
            self.chrome_test_btn.setEnabled(False)
        except Exception as e:
            self.chrome_status_label.setText(f"終了エラー: {e}")

    def _do_chrome_test_grok(self) -> None:
        if self._chrome_driver:
            try:
                self._chrome_driver.get("https://grok.com")
                self.chrome_status_label.setText("Grokを開きました")
            except Exception as e:
                self.chrome_status_label.setText(f"エラー: {e}")

    def _build_filter_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "フィルター")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("除外するフレーズ（1行1フレーズ、部分一致で除外）:"))
        self.filter_edit = QPlainTextEdit()
        self.filter_edit.setPlainText(
            "ありがとうございました\n"
            "ご視聴ありがとうございました\n"
            "チャンネル登録よろしくお願いします\n"
            "高評価よろしくお願いします\n"
            "字幕は自動生成されています\n"
            "お疲れ様でした\n"
            "視聴ありがとうございました\n"
            "ご視聴ありがとう\n"
            "MBC\n"
            "NHK\n"
        )
        layout.addWidget(self.filter_edit, 1)

    # ---- ヘルパー ----

    def _hrow(self, edit: QLineEdit, btn: QPushButton) -> QWidget:
        row = QHBoxLayout(); row.addWidget(edit, 1); row.addWidget(btn)
        w = QWidget(); w.setLayout(row)
        return w

    def _pick_dir(self, edit: QLineEdit, title: str) -> None:
        selected = QFileDialog.getExistingDirectory(self, title, edit.text().strip() or str(Path.cwd()))
        if selected:
            edit.setText(selected)

    def _pick_file(self, edit: QLineEdit, title: str) -> None:
        current = edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.cwd())
        selected, _ = QFileDialog.getOpenFileName(self, title, start, "All Files (*)")
        if selected:
            edit.setText(selected)

    def _reload_models(self) -> None:
        sbv2_root = Path(self.sbv2_root_edit.text().strip()).expanduser()
        model_assets = sbv2_root / "model_assets"
        prev = self.model_name_combo.currentText().strip()
        self.model_name_combo.clear()
        if not model_assets.is_dir():
            return
        names = sorted(
            d.name for d in model_assets.iterdir()
            if d.is_dir() and (d / "config.json").exists()
        )
        for name in names:
            self.model_name_combo.addItem(name)
        if prev:
            idx = self.model_name_combo.findText(prev)
            if idx >= 0:
                self.model_name_combo.setCurrentIndex(idx)
            else:
                self.model_name_combo.setEditText(prev)
        self._reload_model_files()

    def _reload_model_files(self) -> None:
        sbv2_root = Path(self.sbv2_root_edit.text().strip()).expanduser()
        model_name = self.model_name_combo.currentText().strip()
        prev = self.model_file_edit.currentText().strip()
        self.model_file_edit.clear()
        self.model_file_edit.addItem("")
        if model_name:
            model_dir = sbv2_root / "model_assets" / model_name
            if model_dir.is_dir():
                files = sorted(p.name for p in model_dir.glob("*.safetensors"))
                for f in files:
                    self.model_file_edit.addItem(f)
        if prev:
            idx = self.model_file_edit.findText(prev)
            if idx >= 0:
                self.model_file_edit.setCurrentIndex(idx)
            else:
                self.model_file_edit.setEditText(prev)

    @staticmethod
    def _strip_device_index(label: str) -> str:
        """'[2] Microphone (...)' → 'Microphone (...)'"""
        return re.sub(r'^\[\d+\]\s*', '', label)

    def _select_device_by_name(self, saved_label: str) -> bool:
        target = self._strip_device_index(saved_label)
        for i in range(self.device_combo.count()):
            if self._strip_device_index(self.device_combo.itemText(i)) == target:
                self.device_combo.setCurrentIndex(i)
                return True
        return False

    def _reload_devices(self) -> None:
        prev_label = self.device_combo.currentText()
        self.device_combo.clear()
        self.device_combo.addItem("System default", None)
        for idx, label in get_input_devices():
            self.device_combo.addItem(label, idx)
        if prev_label and prev_label != "System default":
            self._select_device_by_name(prev_label)

    def _append_log(self, msg: str) -> None:
        self.log_text.appendPlainText(msg)

    @staticmethod
    def _deferred_live_fields(cfg_prev: AppConfig, cfg_now: AppConfig) -> list[str]:
        fields: list[str] = []
        if cfg_prev.wav_dir != cfg_now.wav_dir:
            fields.append("wav_dir")
        if cfg_prev.faster_python != cfg_now.faster_python:
            fields.append("faster_python")
        if cfg_prev.faster_model != cfg_now.faster_model:
            fields.append("faster_model")
        if cfg_prev.faster_device != cfg_now.faster_device:
            fields.append("faster_device")
        if cfg_prev.faster_compute != cfg_now.faster_compute:
            fields.append("faster_compute")
        if cfg_prev.faster_language != cfg_now.faster_language:
            fields.append("faster_language")
        if cfg_prev.faster_beam != cfg_now.faster_beam:
            fields.append("faster_beam")
        if cfg_prev.transcribe_server_port != cfg_now.transcribe_server_port:
            fields.append("transcribe_server_port")
        if cfg_prev.sbv2_root != cfg_now.sbv2_root:
            fields.append("sbv2_root")
        if cfg_prev.sbv2_server_url != cfg_now.sbv2_server_url:
            fields.append("sbv2_server_url")
        if cfg_prev.sbv2_server_auto_start != cfg_now.sbv2_server_auto_start:
            fields.append("sbv2_server_auto_start")
        if cfg_prev.external_text_enabled != cfg_now.external_text_enabled:
            fields.append("external_text_enabled")
        if cfg_prev.external_text_host != cfg_now.external_text_host:
            fields.append("external_text_host")
        if cfg_prev.external_text_port != cfg_now.external_text_port:
            fields.append("external_text_port")
        if cfg_prev.external_text_endpoint != cfg_now.external_text_endpoint:
            fields.append("external_text_endpoint")
        if cfg_prev.external_text_token != cfg_now.external_text_token:
            fields.append("external_text_token")
        if cfg_prev.external_text_dedupe_max != cfg_now.external_text_dedupe_max:
            fields.append("external_text_dedupe_max")
        if cfg_prev.device != cfg_now.device:
            fields.append("device")
        return fields

    def _apply_live_settings(self, cfg: AppConfig) -> None:
        prev = self._active_runtime_cfg
        self._active_runtime_cfg = cfg
        if not self._running:
            self._last_deferred_live_fields = tuple()
            return

        if self._pipeline_worker is None:
            self._pending_cfg = cfg
            return

        self._pipeline_worker.update_runtime_config(cfg)

        if self._recorder_worker is not None:
            self._recorder_worker.update_live_config(
                output_dir=cfg.wav_dir,
                threshold_dbfs=cfg.threshold_dbfs,
                silence_seconds=cfg.silence_seconds,
                min_duration_seconds=cfg.min_duration_seconds,
                pre_roll_seconds=cfg.pre_roll_seconds,
                post_roll_seconds=cfg.post_roll_seconds,
                device=cfg.device,
            )

        need_rec = self._is_recorder_needed(cfg)
        has_rec = self._recorder_worker is not None
        if need_rec and (not has_rec) and (not self._paused):
            self._start_recorder(cfg)
            self._append_log("[live] 録音ワーカーを開始")
        elif (not need_rec) and has_rec:
            self._stop_recorder()
            self._append_log("[live] source_mode=external: 録音ワーカー停止")

        if prev is None:
            self._last_deferred_live_fields = tuple()
            return

        deferred = tuple(self._deferred_live_fields(prev, cfg))
        if deferred and deferred != self._last_deferred_live_fields:
            self._append_log("[live] 反映保留(再起動時): " + ", ".join(deferred))
        self._last_deferred_live_fields = deferred

    def _on_live_setting_changed(self, *_args) -> None:
        if self._loading_config:
            return
        cfg = self._save_config()
        if cfg is None:
            return
        self._apply_live_settings(cfg)

    def _on_any_setting_changed(self, *_args) -> None:
        self._on_live_setting_changed(*_args)

    def _install_autosave_hooks(self) -> None:
        # 録音設定
        self.device_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.wav_dir_edit.textChanged.connect(self._on_any_setting_changed)
        self.threshold_spin.valueChanged.connect(self._on_any_setting_changed)
        self.silence_spin.valueChanged.connect(self._on_any_setting_changed)
        self.min_dur_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pre_roll_spin.valueChanged.connect(self._on_any_setting_changed)
        self.post_roll_spin.valueChanged.connect(self._on_any_setting_changed)

        # パイプライン設定
        self.kks_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.output_dir_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_model_edit.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_device_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_compute_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_lang_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_beam_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pipeline_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.video_metadata_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_server_url_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_auto_start_chk.toggled.connect(self._on_any_setting_changed)
        self.model_name_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.model_file_edit.currentTextChanged.connect(self._on_any_setting_changed)
        self.speaker_edit.textChanged.connect(self._on_any_setting_changed)
        self.style_edit.textChanged.connect(self._on_any_setting_changed)
        self.length_spin.valueChanged.connect(self._on_any_setting_changed)
        self.voice_volume_spin.valueChanged.connect(self._on_any_setting_changed)
        self.voice_pitch_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pipe_edit.textChanged.connect(self._on_any_setting_changed)
        self.main_spin.valueChanged.connect(self._on_any_setting_changed)
        self.face_spin.valueChanged.connect(self._on_any_setting_changed)
        self.keep_face_chk.toggled.connect(self._on_any_setting_changed)
        self.target_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.target_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.target_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.target_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.remote_http_chk.toggled.connect(self._on_any_setting_changed)
        self.subtitle_send_chk.toggled.connect(self._on_any_setting_changed)
        self.subtitle_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.subtitle_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_timeout_spin.valueChanged.connect(self._on_any_setting_changed)
        self.source_mode_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.external_text_chk.toggled.connect(self._on_any_setting_changed)
        self.external_text_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.external_text_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_dedupe_spin.valueChanged.connect(self._on_any_setting_changed)

        # フィルター / 変換
        self.filter_edit.textChanged.connect(self._on_any_setting_changed)
        self.conversion_table.itemChanged.connect(self._on_any_setting_changed)
        self.chrome_profile_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.chrome_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.chrome_headless_chk.toggled.connect(self._on_any_setting_changed)

    @staticmethod
    def _is_recorder_needed(cfg: AppConfig) -> bool:
        mode = (cfg.source_mode or DEFAULT_SOURCE_MODE).strip().lower()
        return mode in ("mic", "both")

    # ---- 設定 ----

    def _build_config(self) -> AppConfig:
        filter_phrases = [l for l in self.filter_edit.toPlainText().splitlines() if l.strip()]
        transcribe_conversion_dict = []
        for row in range(self.transcribe_conversion_table.rowCount()):
            from_item = self.transcribe_conversion_table.item(row, 0)
            to_item = self.transcribe_conversion_table.item(row, 1)
            from_str = (from_item.text() if from_item else "").strip()
            to_str = (to_item.text() if to_item else "").strip()
            if from_str:
                transcribe_conversion_dict.append({"from": from_str, "to": to_str})
        conversion_dict = []
        for row in range(self.conversion_table.rowCount()):
            from_item = self.conversion_table.item(row, 0)
            to_item = self.conversion_table.item(row, 1)
            display_item = self.conversion_table.item(row, 2)
            from_str = (from_item.text() if from_item else "").strip()
            to_str = (to_item.text() if to_item else "").strip()
            if from_str:
                display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
                conversion_dict.append({"from": from_str, "to": to_str, "display_apply": display_apply})
        return AppConfig(
            wav_dir=Path(self.wav_dir_edit.text().strip()).expanduser().resolve(),
            threshold_dbfs=float(self.threshold_spin.value()),
            silence_seconds=float(self.silence_spin.value()),
            min_duration_seconds=float(self.min_dur_spin.value()),
            pre_roll_seconds=float(self.pre_roll_spin.value()),
            post_roll_seconds=float(self.post_roll_spin.value()),
            device=self.device_combo.currentData(),
            kks_root=Path(self.kks_root_edit.text().strip()).expanduser().resolve(),
            output_dir=Path(self.output_dir_edit.text().strip()).expanduser().resolve(),
            faster_python=Path(self.faster_python_edit.text().strip()).expanduser().resolve(),
            faster_model=self.faster_model_edit.currentText().strip() or "large-v3",
            faster_device=self.faster_device_combo.currentText().strip(),
            faster_compute=self.faster_compute_combo.currentText().strip(),
            faster_language=self.faster_lang_edit.text().strip() or "ja",
            faster_beam=max(1, int(self.faster_beam_spin.value())),
            pipeline_python=Path(self.pipeline_python_edit.text().strip()).expanduser().resolve(),
            sbv2_root=Path(self.sbv2_root_edit.text().strip()).expanduser().resolve(),
            sbv2_model_name=self.model_name_combo.currentText().strip(),
            sbv2_model_file=self.model_file_edit.currentText().strip(),
            sbv2_speaker=self.speaker_edit.text().strip() or "0",
            sbv2_style=self.style_edit.text().strip() or "Neutral",
            sbv2_length=float(self.length_spin.value()),
            voice_volume=float(self.voice_volume_spin.value()),
            voice_pitch=float(self.voice_pitch_spin.value()),
            pipe_name=self.pipe_edit.text().strip() or "kks_voice_face_events",
            target_host=self.target_host_edit.text().strip(),
            target_port=int(self.target_port_spin.value()),
            target_endpoint=self.target_endpoint_edit.text().strip() or "/voice-face-event",
            target_token=self.target_token_edit.text().strip(),
            remote_http=bool(self.remote_http_chk.isChecked()),
            subtitle_send_enabled=bool(self.subtitle_send_chk.isChecked()),
            subtitle_target_host=self.subtitle_host_edit.text().strip() or "127.0.0.1",
            subtitle_target_port=int(self.subtitle_port_spin.value()),
            subtitle_endpoint=self.subtitle_endpoint_edit.text().strip() or "/subtitle-event",
            subtitle_token=self.subtitle_token_edit.text().strip(),
            subtitle_timeout_sec=float(self.subtitle_timeout_spin.value()),
            main_index=int(self.main_spin.value()),
            face=int(self.face_spin.value()),
            keep_current_face=bool(self.keep_face_chk.isChecked()),
            source_mode=self.source_mode_combo.currentText().strip().lower() or DEFAULT_SOURCE_MODE,
            external_text_enabled=bool(self.external_text_chk.isChecked()),
            external_text_host=self.external_text_host_edit.text().strip() or "127.0.0.1",
            external_text_port=int(self.external_text_port_spin.value()),
            external_text_endpoint=self.external_text_endpoint_edit.text().strip() or "/manual-text",
            external_text_token=self.external_text_token_edit.text().strip(),
            external_text_dedupe_max=int(self.external_text_dedupe_spin.value()),
            transcribe_server_port=int(
                json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("transcribe_server_port", 18760)
                if CONFIG_FILE.exists() else 18760
            ),
            sbv2_server_url=self.sbv2_server_url_edit.text().strip() or "http://127.0.0.1:5000",
            sbv2_server_auto_start=self.sbv2_auto_start_chk.isChecked(),
            video_metadata_path=(
                Path(self.video_metadata_edit.text().strip()).expanduser().resolve()
                if self.video_metadata_edit.text().strip()
                else None
            ),
            filter_phrases=filter_phrases,
            transcribe_conversion_dict=transcribe_conversion_dict,
            conversion_dict=conversion_dict,
        )

    def _save_config(self, cfg: Optional[AppConfig] = None) -> Optional[AppConfig]:
        if cfg is None:
            try:
                cfg = self._build_config()
            except Exception:
                return None
        data = {
            "device_name": self.device_combo.currentText(),
            "wav_dir": str(cfg.wav_dir), "threshold_dbfs": cfg.threshold_dbfs,
            "silence_seconds": cfg.silence_seconds, "min_duration_seconds": cfg.min_duration_seconds,
            "pre_roll_seconds": cfg.pre_roll_seconds, "post_roll_seconds": cfg.post_roll_seconds,
            "kks_root": str(cfg.kks_root), "output_dir": str(cfg.output_dir),
            "faster_python": str(cfg.faster_python), "faster_model": cfg.faster_model,
            "faster_device": cfg.faster_device, "faster_compute": cfg.faster_compute,
            "faster_language": cfg.faster_language, "faster_beam": cfg.faster_beam,
            "pipeline_python": str(cfg.pipeline_python), "sbv2_root": str(cfg.sbv2_root),
            "sbv2_model_name": cfg.sbv2_model_name, "sbv2_model_file": cfg.sbv2_model_file,
            "sbv2_speaker": cfg.sbv2_speaker, "sbv2_style": cfg.sbv2_style,
            "sbv2_length": cfg.sbv2_length, "voice_volume": cfg.voice_volume,
            "voice_pitch": cfg.voice_pitch, "pipe_name": cfg.pipe_name,
            "target_host": cfg.target_host, "target_port": cfg.target_port,
            "target_endpoint": cfg.target_endpoint, "target_token": cfg.target_token,
            "remote_http": cfg.remote_http, "subtitle_send_enabled": cfg.subtitle_send_enabled,
            "subtitle_target_host": cfg.subtitle_target_host,
            "subtitle_target_port": cfg.subtitle_target_port,
            "subtitle_endpoint": cfg.subtitle_endpoint, "subtitle_token": cfg.subtitle_token,
            "subtitle_timeout_sec": cfg.subtitle_timeout_sec, "main_index": cfg.main_index,
            "face": cfg.face, "keep_current_face": cfg.keep_current_face,
            "source_mode": cfg.source_mode,
            "external_text_enabled": cfg.external_text_enabled,
            "external_text_host": cfg.external_text_host,
            "external_text_port": cfg.external_text_port,
            "external_text_endpoint": cfg.external_text_endpoint,
            "external_text_token": cfg.external_text_token,
            "external_text_dedupe_max": cfg.external_text_dedupe_max,
            "transcribe_server_port": cfg.transcribe_server_port,
            "sbv2_server_url": cfg.sbv2_server_url,
            "sbv2_server_auto_start": cfg.sbv2_server_auto_start,
            "video_metadata_path": str(cfg.video_metadata_path) if cfg.video_metadata_path else "",
            "filter_phrases": cfg.filter_phrases,
            "transcribe_conversion_dict": cfg.transcribe_conversion_dict,
            "conversion_dict": cfg.conversion_dict,
            "manual_history": self._manual_history[:50],
            "model_presets": self._model_presets,
            "chrome_debug_port": self.chrome_port_spin.value(),
            "chrome_headless": self.chrome_headless_chk.isChecked(),
            "chrome_profile": self.chrome_profile_combo.currentData() or "",
        }
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg

    def _load_config(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self._loading_config = True
        try:
            def s(key, widget_val): return str(data.get(key, widget_val))
            def f(key, widget_val): return float(data.get(key, widget_val))
            def i(key, widget_val): return int(data.get(key, widget_val))
            def b(key, widget_val): return bool(data.get(key, widget_val))
            device_name = data.get("device_name", "")
            if device_name and device_name != "System default":
                self._select_device_by_name(device_name)
            self.wav_dir_edit.setText(s("wav_dir", self.wav_dir_edit.text()))
            self.threshold_spin.setValue(f("threshold_dbfs", self.threshold_spin.value()))
            self.silence_spin.setValue(f("silence_seconds", self.silence_spin.value()))
            self.min_dur_spin.setValue(f("min_duration_seconds", self.min_dur_spin.value()))
            self.pre_roll_spin.setValue(f("pre_roll_seconds", self.pre_roll_spin.value()))
            self.post_roll_spin.setValue(f("post_roll_seconds", self.post_roll_spin.value()))
            self.kks_root_edit.setText(s("kks_root", self.kks_root_edit.text()))
            self.output_dir_edit.setText(s("output_dir", self.output_dir_edit.text()))
            self.faster_python_edit.setText(s("faster_python", self.faster_python_edit.text()))
            self.faster_model_edit.setCurrentText(s("faster_model", self.faster_model_edit.currentText()))
            self.faster_device_combo.setCurrentText(s("faster_device", self.faster_device_combo.currentText()))
            self.faster_compute_combo.setCurrentText(s("faster_compute", self.faster_compute_combo.currentText()))
            self.faster_lang_edit.setText(s("faster_language", self.faster_lang_edit.text()))
            self.faster_beam_spin.setValue(i("faster_beam", self.faster_beam_spin.value()))
            self.pipeline_python_edit.setText(s("pipeline_python", self.pipeline_python_edit.text()))
            self.sbv2_root_edit.setText(s("sbv2_root", self.sbv2_root_edit.text()))
            self._reload_models()
            self.model_name_combo.setEditText(s("sbv2_model_name", ""))
            self.model_file_edit.setEditText(s("sbv2_model_file", ""))
            self.speaker_edit.setText(s("sbv2_speaker", self.speaker_edit.text()))
            self.style_edit.setText(s("sbv2_style", self.style_edit.text()))
            self.length_spin.setValue(f("sbv2_length", self.length_spin.value()))
            self.voice_volume_spin.setValue(f("voice_volume", self.voice_volume_spin.value()))
            self.voice_pitch_spin.setValue(f("voice_pitch", self.voice_pitch_spin.value()))
            self.pipe_edit.setText(s("pipe_name", self.pipe_edit.text()))
            self.target_host_edit.setText(s("target_host", self.target_host_edit.text()))
            self.target_port_spin.setValue(i("target_port", self.target_port_spin.value()))
            self.target_endpoint_edit.setText(s("target_endpoint", self.target_endpoint_edit.text()))
            self.target_token_edit.setText(s("target_token", ""))
            self.remote_http_chk.setChecked(b("remote_http", False))
            self.subtitle_send_chk.setChecked(b("subtitle_send_enabled", True))
            self.subtitle_host_edit.setText(s("subtitle_target_host", self.subtitle_host_edit.text()))
            self.subtitle_port_spin.setValue(i("subtitle_target_port", self.subtitle_port_spin.value()))
            self.subtitle_endpoint_edit.setText(s("subtitle_endpoint", self.subtitle_endpoint_edit.text()))
            self.subtitle_token_edit.setText(s("subtitle_token", ""))
            self.subtitle_timeout_spin.setValue(i("subtitle_timeout_sec", self.subtitle_timeout_spin.value()))
            self.main_spin.setValue(i("main_index", self.main_spin.value()))
            self.face_spin.setValue(i("face", self.face_spin.value()))
            self.keep_face_chk.setChecked(b("keep_current_face", True))
            self.source_mode_combo.setCurrentText(s("source_mode", DEFAULT_SOURCE_MODE))
            self.external_text_chk.setChecked(b("external_text_enabled", True))
            self.external_text_host_edit.setText(s("external_text_host", self.external_text_host_edit.text()))
            self.external_text_port_spin.setValue(i("external_text_port", self.external_text_port_spin.value()))
            self.external_text_endpoint_edit.setText(s("external_text_endpoint", self.external_text_endpoint_edit.text()))
            self.external_text_token_edit.setText(s("external_text_token", ""))
            self.external_text_dedupe_spin.setValue(i("external_text_dedupe_max", self.external_text_dedupe_spin.value()))
            # transcribe_server_port は UI 非公開、config.json のみで管理
            self.sbv2_server_url_edit.setText(s("sbv2_server_url", "http://127.0.0.1:5000"))
            self.sbv2_auto_start_chk.setChecked(b("sbv2_server_auto_start", True))
            self.video_metadata_edit.setText(s("video_metadata_path", self.video_metadata_edit.text()))
            # Selenium設定
            self.chrome_port_spin.setValue(i("chrome_debug_port", 9222))
            self.chrome_headless_chk.setChecked(b("chrome_headless", False))
            saved_profile = s("chrome_profile", "")
            if saved_profile:
                for idx in range(self.chrome_profile_combo.count()):
                    if self.chrome_profile_combo.itemData(idx) == saved_profile:
                        self.chrome_profile_combo.setCurrentIndex(idx)
                        break
            phrases = data.get("filter_phrases", [])
            if phrases:
                self.filter_edit.setPlainText("\n".join(phrases))
            stt_conv = data.get("transcribe_conversion_dict", [])
            self.transcribe_conversion_table.setRowCount(0)
            for entry in stt_conv:
                row = self.transcribe_conversion_table.rowCount()
                self.transcribe_conversion_table.insertRow(row)
                self.transcribe_conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
                self.transcribe_conversion_table.setItem(row, 1, QTableWidgetItem(entry.get("to", "")))
            conv = data.get("conversion_dict", [])
            self.conversion_table.setRowCount(0)
            for entry in conv:
                row = self.conversion_table.rowCount()
                self.conversion_table.insertRow(row)
                self.conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
                self.conversion_table.setItem(row, 1, QTableWidgetItem(entry.get("to", "")))
                self.conversion_table.setItem(row, 2, self._new_display_apply_item(bool(entry.get("display_apply", False))))
            self._model_presets = [p for p in data.get("model_presets", []) if isinstance(p, dict) and p.get("name")]
            self._refresh_preset_ui()
            history = data.get("manual_history", [])
            self._manual_history = list(history)[:50]
            self.manual_combo.clear()
            for h in self._manual_history:
                self.manual_combo.addItem(h)
        finally:
            self._loading_config = False

    # ---- モデルプリセット ----

    def _refresh_preset_ui(self) -> None:
        self.preset_list_combo.clear()
        for p in self._model_presets:
            self.preset_list_combo.addItem(p.get("name", ""))
        for i, btn in enumerate(self._preset_btns):
            if i < len(self._model_presets):
                name = self._model_presets[i].get("name", f"({i+1})")
                btn.setText(name)
                btn.setEnabled(True)
            else:
                btn.setText(f"--- ({i+1})")
                btn.setEnabled(False)

    def _save_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            return
        preset = {
            "name": name,
            "model_name": self.model_name_combo.currentText().strip(),
            "model_file": self.model_file_edit.currentText().strip(),
            "speaker": self.speaker_edit.text().strip(),
            "style": self.style_edit.text().strip(),
        }
        # 同名なら上書き
        for i, p in enumerate(self._model_presets):
            if p.get("name") == name:
                self._model_presets[i] = preset
                self._refresh_preset_ui()
                self._save_config()
                return
        self._model_presets.append(preset)
        self._refresh_preset_ui()
        self._save_config()

    def _apply_preset(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._model_presets):
            return
        p = self._model_presets[idx]
        self.model_name_combo.setEditText(p.get("model_name", ""))
        self._reload_model_files()
        self.model_file_edit.setEditText(p.get("model_file", ""))
        self.speaker_edit.setText(p.get("speaker", "0"))
        self.style_edit.setText(p.get("style", "Neutral"))
        # 実行中のワーカーにも即時反映
        if self._pipeline_worker is not None:
            self._pipeline_worker._cfg.sbv2_model_name = p.get("model_name", "")
            self._pipeline_worker._cfg.sbv2_model_file = p.get("model_file", "")
            self._pipeline_worker._cfg.sbv2_speaker    = p.get("speaker", "0")
            self._pipeline_worker._cfg.sbv2_style      = p.get("style", "Neutral")
            name = p.get("name", f"({idx+1})")
            self._append_log(f"[preset] モデル切替 → {name}")

    def _apply_preset_from_combo(self) -> None:
        self._apply_preset(self.preset_list_combo.currentIndex())

    def _delete_preset(self) -> None:
        idx = self.preset_list_combo.currentIndex()
        if 0 <= idx < len(self._model_presets):
            self._model_presets.pop(idx)
            self._refresh_preset_ui()
            self._save_config()

    # ---- イベントハンドラ ----

    def _on_start_stop(self) -> None:
        if self._running:
            self._stop_all()
        else:
            self._start_all()

    def _on_pause_resume(self) -> None:
        if not self._paused:
            self._paused = True
            self.pause_btn.setText("▶ 再開")
            self._stop_recorder()
            if self._pipeline_worker:
                self._pipeline_worker.pause()
            self._append_log("[info] 一時停止")
        else:
            self._paused = False
            self.pause_btn.setText("⏸ 一時停止")
            if self._pipeline_worker:
                self._pipeline_worker.resume()
            try:
                cfg = self._build_config()
            except Exception as exc:
                self._append_log(f"[error] {exc}")
                return
            if self._is_recorder_needed(cfg):
                self._start_recorder(cfg)
            self._append_log("[info] 再開")

    def _start_all(self) -> None:
        try:
            cfg = self._build_config()
        except Exception as exc:
            self._append_log(f"[error] {exc}")
            return
        self._save_config(cfg)
        self._running = True
        self._paused = False
        self._active_runtime_cfg = cfg
        self._last_deferred_live_fields = tuple()
        self.start_btn.setText("■ 停止")
        self.pause_btn.setEnabled(True)

        # Selenium自動起動（未接続の場合）→ Workerで非同期実行後にパイプライン起動
        if self._chrome_driver is None:
            port = self.chrome_port_spin.value()
            headless = self.chrome_headless_chk.isChecked()
            self._append_log("[selenium] バックグラウンドで起動中...")
            self._pending_cfg = cfg

            def _selenium_task(**kwargs):
                from chrome_debug import launch_chrome, get_driver
                already_running = False
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                        already_running = resp.status == 200
                except Exception:
                    pass
                if already_running:
                    return "existing", get_driver(port=port)
                else:
                    profile_dir = str(self.chrome_profile_combo.currentData() or "").strip()
                    driver = launch_chrome(port=port, headless=headless, profile_dir=profile_dir)
                    return "launched", driver

            self._selenium_worker = _SeleniumWorker(_selenium_task)
            self._selenium_worker.result_ready.connect(self._on_selenium_worker_done)
            self._selenium_worker.error_occurred.connect(self._on_selenium_worker_error)
            self._selenium_worker.start()
            return

        self._continue_start_pipeline(cfg)

    def _on_selenium_worker_done(self, status, driver) -> None:
        if driver:
            self._chrome_driver = driver
            if self._find_grok_tab():
                self._append_log("[selenium] Grokタブ検出")
            else:
                self._append_log("[selenium] 接続完了（Grokタブなし）")
            self.chrome_close_btn.setEnabled(True)
            self.chrome_test_btn.setEnabled(True)
            if status == "launched":
                self.chrome_launch_btn.setEnabled(False)
        else:
            self._append_log("[selenium] プロファイル未選択、スキップ")
        if self._pending_cfg is None:
            self._append_log("[error] pending config missing")
            return
        self._continue_start_pipeline(self._pending_cfg)
        self._pending_cfg = None

    def _on_selenium_worker_error(self, err) -> None:
        self._append_log(f"[selenium] 起動失敗: {err}")
        if self._pending_cfg is None:
            self._append_log("[error] pending config missing")
            return
        self._continue_start_pipeline(self._pending_cfg)
        self._pending_cfg = None

    def _continue_start_pipeline(self, cfg: "AppConfig") -> None:
        # パイプライン起動
        self._pipeline_thread = QThread(self)
        self._pipeline_worker = PipelineWorker(cfg)
        self._active_runtime_cfg = cfg
        self._pipeline_worker.moveToThread(self._pipeline_thread)
        self._pipeline_thread.started.connect(self._pipeline_worker.run)
        self._pipeline_worker.log.connect(self._append_log)
        self._pipeline_worker.error.connect(self._on_pipeline_error)
        self._pipeline_worker.finished.connect(self._pipeline_worker.deleteLater)
        self._pipeline_thread.finished.connect(self._pipeline_thread.deleteLater)
        self._pipeline_thread.start()

        # 録音起動
        if self._is_recorder_needed(cfg):
            self._start_recorder(cfg)
        else:
            self._append_log("[info] source_mode=external: 録音は起動しません")
        self._append_log("[info] 開始")

    def _start_recorder(self, cfg: AppConfig) -> None:
        device_label = self.device_combo.currentText()
        self._append_log(f"[recorder] device index={cfg.device} label={device_label!r}")
        rec_cfg = RecorderConfig(
            output_dir=cfg.wav_dir,
            sample_rate=16000, block_ms=100,
            threshold_dbfs=cfg.threshold_dbfs,
            silence_seconds=cfg.silence_seconds,
            min_duration_seconds=cfg.min_duration_seconds,
            device=cfg.device,
            pre_roll_seconds=cfg.pre_roll_seconds,
            post_roll_seconds=cfg.post_roll_seconds,
            tcp_host="", tcp_port=17890, tcp_token="", tcp_timeout_seconds=20.0,
            external_control_enabled=False, external_control_host="127.0.0.1",
            external_control_port=17911, external_control_token="",
        )
        self._recorder_thread = QThread(self)
        self._recorder_worker = RecorderWorker(rec_cfg)
        self._recorder_worker.moveToThread(self._recorder_thread)
        self._recorder_thread.started.connect(self._recorder_worker.run)
        self._recorder_worker.log.connect(self._append_log)
        self._recorder_worker.error.connect(lambda s: self._append_log(f"[error] 録音: {s}"))
        self._recorder_worker.finished.connect(self._recorder_worker.deleteLater)
        self._recorder_thread.finished.connect(self._recorder_thread.deleteLater)
        self._recorder_thread.start()

    def _stop_recorder(self) -> None:
        if self._recorder_worker:
            self._recorder_worker.stop()
        if self._recorder_thread:
            self._recorder_thread.quit()
            self._recorder_thread.wait(2000)
        self._recorder_thread = None
        self._recorder_worker = None

    def _stop_all(self) -> None:
        self._running = False
        self._paused = False
        self._active_runtime_cfg = None
        self._pending_cfg = None
        self._last_deferred_live_fields = tuple()
        self.start_btn.setText("▶ 開始")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸ 一時停止")
        self._stop_recorder()
        if self._pipeline_worker:
            self._pipeline_worker.stop()
        if self._pipeline_thread:
            self._pipeline_thread.quit()
            self._pipeline_thread.wait(3000)
        self._pipeline_thread = None
        self._pipeline_worker = None
        self._append_log("[info] 停止")

    def _send_manual(self) -> None:
        text = self.manual_combo.currentText().strip()
        if not text:
            return
        if not self._pipeline_worker:
            self._append_log("[warn] パイプラインが起動していません")
            return
        self._pipeline_worker.send_text(text)
        self._append_log(f"[手動] {text}")
        # 履歴に追加（重複除去、先頭に挿入、上限50件）
        if text in self._manual_history:
            self._manual_history.remove(text)
        self._manual_history.insert(0, text)
        self._manual_history = self._manual_history[:50]
        idx = self.manual_combo.findText(text)
        if idx >= 0:
            self.manual_combo.removeItem(idx)
        self.manual_combo.insertItem(0, text)
        while self.manual_combo.count() > 50:
            self.manual_combo.removeItem(self.manual_combo.count() - 1)
        self.manual_combo.lineEdit().clear()

    def _on_pipeline_error(self, stack: str) -> None:
        self._append_log("[error] パイプライン例外")
        self._append_log(stack)

    def closeEvent(self, event) -> None:
        try:
            self._save_config()
        except Exception:
            pass
        try:
            if self._fw_test_stream is not None:
                self._fw_test_stream.stop()
                self._fw_test_stream.close()
                self._fw_test_stream = None
            sd.stop()
        except Exception:
            pass
        self._stop_all()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> int:
    if not _acquire_single_instance("KKS_Human2KKSPipeline"):
        print("[info] Already running.")
        return 0
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
