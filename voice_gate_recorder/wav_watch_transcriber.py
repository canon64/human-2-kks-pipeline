#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


@dataclass
class WatchTranscriberConfig:
    watch_dir: Path
    output_dir: Path
    model_size_or_path: str = "small"
    device: str = "auto"
    compute_type: str = "int8_float16"
    language: Optional[str] = "ja"
    download_root: Optional[Path] = None
    beam_size: int = 1
    send_to_grok: bool = False
    grok_script_path: Optional[Path] = None
    grok_python_path: Optional[Path] = None
    grok_timeout_seconds: float = 120.0
    queue_timeout_seconds: float = 0.5
    stable_checks: int = 3
    stable_interval_seconds: float = 0.25
    stable_timeout_seconds: float = 30.0
    text_input_enabled: bool = False
    text_input_host: str = "127.0.0.1"
    text_input_port: int = 17912
    text_input_token: str = ""
    text_replacements: list[tuple[str, str]] = field(default_factory=list)



class WavWatchTranscriber:
    def __init__(
        self,
        config: WatchTranscriberConfig,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = config
        self.log_callback = log_callback
        self._running = True
        self._queue: queue.Queue[Path | str] = queue.Queue(maxsize=1024)
        self._queued_or_done: set[str] = set()
        self._set_lock = threading.Lock()
        self._observer = None
        self._model = None
        self._text_input_socket: Optional[socket.socket] = None
        self._text_input_thread: Optional[threading.Thread] = None
        self._driver = None
        self._grok_config = None
        self._grok_logger = None

    def _log(self, message: str) -> None:
        print(message)
        if self.log_callback is not None:
            self.log_callback(message)

    def stop(self) -> None:
        self._running = False
        observer = self._observer
        if observer is not None:
            observer.stop()
        self._stop_text_input_listener()

    def _init_grok_driver(self) -> None:
        if not self.cfg.send_to_grok or self.cfg.grok_script_path is None:
            return
        src_dir = str(self.cfg.grok_script_path.parent / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        try:
            import logging
            from grok_bridge.browser import connect_existing_debug_chrome
            from grok_bridge.config import load_or_create_config
            base_dir = str(self.cfg.grok_script_path.parent)
            config_path = str(self.cfg.grok_script_path.parent / "grok_bridge_config.json")
            self._grok_config = load_or_create_config(config_path)
            self._grok_logger = logging.getLogger("grok_bridge.transcriber")
            self._driver = connect_existing_debug_chrome(self._grok_config.debug_port)
            self._log("[grok] WebDriver接続確立")
        except Exception as exc:
            self._log(f"[error] Grok WebDriver接続失敗: {exc}")
            self._driver = None
            self._grok_config = None
            self._grok_logger = None

    def _close_grok_driver(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            self._log("[grok] WebDriver切断")

    def send_text(self, text: str) -> None:
        """外部から直接テキストをキューに積む（GUIからの直接入力用）。"""
        text = text.strip()
        if text == "":
            return
        try:
            self._queue.put_nowait(text)
            self._log(f"[text_in] queued: {text[:60]}")
        except queue.Full:
            self._log("[warn] text queue full, drop")

    def _start_text_input_listener(self) -> None:
        if not self.cfg.text_input_enabled:
            return
        if self._text_input_thread is not None:
            return
        self._text_input_thread = threading.Thread(
            target=self._text_input_loop,
            name="WavWatchTranscriber.text_input",
            daemon=True,
        )
        self._text_input_thread.start()
        self._log(
            f"[text_in] UDP listener started bind={self.cfg.text_input_host}:{self.cfg.text_input_port}"
        )

    def _stop_text_input_listener(self) -> None:
        if self._text_input_thread is None:
            return
        try:
            if self._text_input_socket is not None:
                self._text_input_socket.close()
        except Exception:
            pass
        self._text_input_thread.join(timeout=2.0)
        self._text_input_thread = None
        self._text_input_socket = None

    def _text_input_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._text_input_socket = sock
        sock.settimeout(0.5)
        sock.bind((self.cfg.text_input_host, int(self.cfg.text_input_port)))
        required_token = (self.cfg.text_input_token or "").strip()

        while self._running:
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                self._log(f"[error] text_input recv failed: {exc}")
                continue

            raw = data.decode("utf-8", errors="replace").strip()
            if raw == "":
                continue

            # JSONパケット: {"text": "...", "token": "..."}
            text = ""
            token = ""
            if raw.startswith("{"):
                try:
                    payload = json.loads(raw)
                    text = str(payload.get("text", "")).strip()
                    token = str(payload.get("token", "")).strip()
                except Exception:
                    self._log(f"[warn] text_input invalid JSON from {addr[0]}")
                    continue
            else:
                # プレーンテキスト（トークンなし）
                text = raw

            if required_token != "" and token != required_token:
                self._log(f"[warn] text_input token mismatch from {addr[0]}")
                continue

            if text == "":
                continue

            self._log(f"[text_in] received from {addr[0]}:{addr[1]}: {text[:60]}")
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                self._log("[warn] text queue full, drop")

    def run(self) -> None:
        self.cfg.watch_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)

        self._model = self._create_model()
        self._observer = self._create_observer()
        self._observer.start()
        self._start_text_input_listener()
        self._init_grok_driver()

        self._log(
            "[info] Watch started: "
            f"watch_dir={self.cfg.watch_dir} "
            f"model={self.cfg.model_size_or_path} "
            f"device={self.cfg.device} compute={self.cfg.compute_type} "
            f"download_root={self.cfg.download_root if self.cfg.download_root else '(default)'} "
            f"send_to_grok={self.cfg.send_to_grok} "
            f"text_input={self.cfg.text_input_enabled}"
        )

        try:
            while self._running:
                try:
                    item = self._queue.get(timeout=self.cfg.queue_timeout_seconds)
                except queue.Empty:
                    continue
                if isinstance(item, str):
                    self._process_text(item)
                else:
                    self._process_one(item)
        finally:
            observer = self._observer
            if observer is not None:
                observer.stop()
                observer.join(timeout=3.0)
                self._observer = None
            self._stop_text_input_listener()
            self._close_grok_driver()
            self._log("[info] Watch stopped.")

    def _process_text(self, text: str) -> None:
        """FasterWhisperをスキップして、テキストを直接Grokへ送信する。"""
        self._log(f"[text] {text}")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out_path = self.cfg.output_dir / f"text_{ts}.txt"
        out_path.write_text(text + "\n", encoding="utf-8")
        self._log(f"[saved] {out_path}")
        if self.cfg.send_to_grok and text != "":
            # source_wav の代わりに out_path を渡す（ログ表示のみに使用）
            self._send_to_grok(out_path, text)

    def _create_model(self):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise RuntimeError(
                "faster-whisper が見つかりません。`pip install faster-whisper` を実行してください。"
            ) from exc

        model_kwargs = {
            "model_size_or_path": self.cfg.model_size_or_path,
            "device": self.cfg.device,
            "compute_type": self.cfg.compute_type,
        }
        if self.cfg.download_root is not None:
            model_kwargs["download_root"] = str(self.cfg.download_root)
        return WhisperModel(**model_kwargs)

    def _create_observer(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception as exc:
            raise RuntimeError("watchdog が見つかりません。`pip install watchdog` を実行してください。") from exc

        transcriber = self

        class _WavEventHandler(FileSystemEventHandler):
            def on_created(self, event) -> None:
                if event.is_directory:
                    return
                transcriber._enqueue_if_wav(Path(event.src_path))

            def on_moved(self, event) -> None:
                if event.is_directory:
                    return
                transcriber._enqueue_if_wav(Path(event.dest_path))

        observer = Observer()
        observer.schedule(_WavEventHandler(), str(self.cfg.watch_dir), recursive=False)
        return observer

    def _enqueue_if_wav(self, path: Path) -> None:
        if path.suffix.lower() != ".wav":
            return

        key = str(path.resolve())
        with self._set_lock:
            if key in self._queued_or_done:
                return
            self._queued_or_done.add(key)

        try:
            self._queue.put_nowait(path)
            self._log(f"[queue] {path}")
        except queue.Full:
            self._log(f"[warn] queue full, drop: {path}")
            with self._set_lock:
                self._queued_or_done.discard(key)

    def _wait_stable(self, path: Path) -> bool:
        stable_count = 0
        last_size = -1
        start = time.time()
        while self._running and (time.time() - start) < self.cfg.stable_timeout_seconds:
            if not path.exists():
                stable_count = 0
                last_size = -1
                time.sleep(self.cfg.stable_interval_seconds)
                continue

            current_size = path.stat().st_size
            if current_size > 0 and current_size == last_size:
                stable_count += 1
                if stable_count >= self.cfg.stable_checks:
                    return True
            else:
                stable_count = 0
                last_size = current_size

            time.sleep(self.cfg.stable_interval_seconds)
        return False

    def _process_one(self, path: Path) -> None:
        if not self._wait_stable(path):
            self._log(f"[warn] file not stable, skip: {path}")
            return
        if not path.exists():
            self._log(f"[warn] file not found, skip: {path}")
            return

        try:
            self._log(f"[transcribe] {path}")
            transcribe_kwargs = {
                "audio": str(path),
                "beam_size": self.cfg.beam_size,
                "condition_on_previous_text": False,
            }
            if self.cfg.language:
                transcribe_kwargs["language"] = self.cfg.language

            segments, info = self._model.transcribe(**transcribe_kwargs)
            text = "".join(segment.text for segment in segments).strip()
            text = self._apply_text_replacements(text)

            elapsed = float(getattr(info, "duration", 0.0))
            if text == "":
                self._log(f"[result] empty text: {path.name} (audio={elapsed:.2f}s)")
            else:
                self._log(f"[result] {path.name}: {text}")

            out_path = self.cfg.output_dir / f"{path.stem}.txt"
            if out_path.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                out_path = self.cfg.output_dir / f"{path.stem}_{stamp}.txt"
            out_path.write_text(text + "\n", encoding="utf-8")
            self._log(f"[saved] {out_path}")
            if self.cfg.send_to_grok and text != "":
                self._send_to_grok(path, text)
        except Exception as exc:
            self._log(f"[error] transcribe failed: {path} ({exc})")

    def _apply_text_replacements(self, text: str) -> str:
        converted = text
        applied_count = 0
        for source, target in self.cfg.text_replacements:
            if source == "":
                continue
            hit_count = converted.count(source)
            if hit_count <= 0:
                continue
            converted = converted.replace(source, target)
            applied_count += 1
            self._log(f"[replace] '{source}' -> '{target}' (hits={hit_count})")
        if applied_count > 0:
            self._log(f"[replace] applied_rules={applied_count}")
        return converted

    def _send_to_grok(self, source_path: Path, text: str) -> None:
        if self._driver is None or self._grok_config is None or self._grok_logger is None:
            self._log("[warn] Grok WebDriver未接続、スキップ")
            return
        if self.cfg.grok_script_path is None:
            self._log("[warn] grok_script_path未設定、スキップ")
            return

        # auto_cfgを先に読んで字幕設定を準備
        auto_cfg_path = self.cfg.grok_script_path.parent / "grok_auto_pipeline_config.json"
        try:
            auto_cfg = json.loads(auto_cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log(f"[error] auto_cfg読み込み失敗: {exc}")
            return

        subtitle_enabled = bool(auto_cfg.get("subtitle_send_enabled", False))
        subtitle_host = auto_cfg.get("subtitle_target_host", "127.0.0.1").strip() or "127.0.0.1"
        subtitle_port = int(auto_cfg.get("subtitle_target_port", 18766))
        subtitle_endpoint = auto_cfg.get("subtitle_endpoint", "/subtitle-event")
        subtitle_token = auto_cfg.get("subtitle_token", "").strip()
        subtitle_timeout = float(auto_cfg.get("subtitle_timeout_sec", 5))

        def _send_subtitle(sub_text: str, display_mode: str) -> None:
            if not subtitle_enabled:
                return
            payload_text = sub_text
            if display_mode.lower() == "stackfemale" and "<color=" not in sub_text.lower():
                payload_text = f"<color=#FF7ACDFF>{sub_text}</color>"
            payload = json.dumps({
                "text": payload_text,
                "source": "wav_watch_transcriber",
                "wav_name": source_path.name,
                "display_mode": display_mode,
                "hold_seconds": 10.0,
            }, ensure_ascii=False).encode("utf-8")
            ep = subtitle_endpoint if subtitle_endpoint.startswith("/") else "/" + subtitle_endpoint
            url = f"http://{subtitle_host}:{subtitle_port}{ep}"
            req = urllib.request.Request(url=url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if subtitle_token:
                req.add_header("X-Auth-Token", subtitle_token)
            try:
                urllib.request.urlopen(req, timeout=subtitle_timeout)
                self._log(f"[subtitle] sent mode={display_mode}")
            except Exception as exc:
                self._log(f"[subtitle] failed: {exc}")

        # 男の字幕: テキスト入力の瞬間に送信
        _send_subtitle(text, "StackMale")

        try:
            from grok_bridge.grok_client import send_text as grok_send_text
            from grok_bridge.grok_client import wait_for_response
            self._log(f"[grok] send {source_path.name}")
            baseline, stop_before = grok_send_text(self._driver, self._grok_config, text, self._grok_logger)
            response = wait_for_response(self._driver, self._grok_config, self._grok_logger, baseline, stop_before)
            response_text = str(response).strip()
            self._log(f"[grok] response_len={len(response_text)}")
        except Exception as exc:
            self._log(f"[error] Grok send failed: {source_path.name} ({exc})")
            return

        # TTS→KKSゲームへ送信
        try:
            pipeline_python = Path(auto_cfg.get("pipeline_python", "")).expanduser().resolve()
            pipeline_script = self.cfg.grok_script_path.parent / "run_grok_tts_event.py"
            sbv2_root = auto_cfg.get("sbv2_root", "")
            model_name = auto_cfg.get("model_name", "")
            model_file = auto_cfg.get("model_file", "")
            speaker = str(auto_cfg.get("speaker", "0"))
            style = auto_cfg.get("style", "Neutral")
            pipe_name = auto_cfg.get("pipe", "kks_voice_face_events")
            main_index = str(auto_cfg.get("main", 0))
            face = int(auto_cfg.get("face", -1))
            keep_face = bool(auto_cfg.get("keep_face", False))
            face_send_mode = str(auto_cfg.get("face_send_mode", "game_preset") or "game_preset").strip().lower()
            if face_send_mode not in ("game_preset", "preset_name", "preset_id"):
                face_send_mode = "game_preset"
            if face_send_mode == "preset_id":
                face_send_mode = "preset_name"
            face_preset_id = str(auto_cfg.get("face_preset_id", "") or "").strip()
            face_preset_name = str(auto_cfg.get("face_preset_name", "") or "").strip()
            face_preset_random = bool(auto_cfg.get("face_preset_random", False))
            target_host = auto_cfg.get("target_host", "").strip()
            target_port = str(auto_cfg.get("target_port", 18765))
            target_endpoint = auto_cfg.get("target_endpoint", "/voice-face-event")
            target_token = auto_cfg.get("target_token", "").strip()
            remote_http = bool(auto_cfg.get("remote_http", False))
            kks_root = Path(auto_cfg.get("kks_root", str(Path(__file__).resolve().parent.parent))).expanduser().resolve()
            output_dir = str(self.cfg.grok_script_path.parent / "outputs" / "auto_pipeline")
            event_sender = str(Path(__file__).resolve().parent.parent / "send_voice_face_event.ps1")

            cmd = [
                str(pipeline_python),
                str(pipeline_script),
                "--response-text", response_text,
                "--sbv2-root", sbv2_root,
                "--model-name", model_name,
                "--speaker", speaker,
                "--style", style,
                "--output-dir", output_dir,
                "--event-sender", event_sender,
                "--pipe-name", pipe_name,
                "--main", main_index,
                "--face-send-mode", face_send_mode,
            ]
            if model_file:
                cmd.extend(["--model-file", model_file])
            if remote_http and target_host:
                cmd.append("--remote-http")
            if target_host:
                cmd.extend(["--target-host", target_host])
                cmd.extend(["--target-port", target_port])
                cmd.extend(["--target-endpoint", target_endpoint])
                if target_token:
                    cmd.extend(["--target-token", target_token])
            if face_send_mode == "preset_name":
                if face_preset_random:
                    cmd.append("--face-preset-random")
                    self._log("[face-preset] random=1 dropdown_ignored=1")
                    face_preset_name = ""
                    face_preset_id = ""
                if face_preset_name:
                    cmd.extend(["--face-preset-name", face_preset_name])
                if face_preset_id:
                    cmd.extend(["--face-preset-id", face_preset_id])
                if (not face_preset_random) and (not face_preset_name) and (not face_preset_id):
                    self._log("[error] face_send_mode=preset_name だが face_preset_name が空")
                    return
            else:
                if keep_face:
                    cmd.append("--keep-current-face")
                elif face >= 0:
                    cmd.extend(["--face", str(face)])

            self._log(f"[tts] start pipeline")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.cfg.grok_timeout_seconds,
            )
            if result.returncode != 0:
                self._log(f"[error] pipeline failed: {(result.stderr or result.stdout or '').strip()[:200]}")
            else:
                self._log(f"[tts] done")
                # 女の字幕: TTS完了直後に送信
                _send_subtitle(response_text, "StackFemale")
        except Exception as exc:
            self._log(f"[error] TTS pipeline failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch a directory and transcribe newly created WAV files with faster-whisper."
    )
    parser.add_argument("--watch-dir", required=True, help="Directory to monitor for new WAV files.")
    parser.add_argument("--output-dir", required=True, help="Directory to save transcribed text files.")
    parser.add_argument("--model", default="small", help="Model name/path for faster-whisper.")
    parser.add_argument("--device", default="auto", help="Inference device: auto/cuda/cpu.")
    parser.add_argument("--compute-type", default="int8_float16", help="Compute type for faster-whisper.")
    parser.add_argument("--language", default="ja", help="Fixed language code. Empty string for auto.")
    parser.add_argument("--download-root", default="", help="Model download/cache directory.")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size.")
    parser.add_argument("--send-to-grok", action="store_true", help="Send transcribed text to Grok bridge.")
    parser.add_argument("--grok-script", default="", help="Path to run_grok_bridge.py.")
    parser.add_argument("--grok-python", default="", help="Python executable path for grok bridge.")
    parser.add_argument("--grok-timeout", type=float, default=120.0, help="Grok response timeout seconds.")
    parser.add_argument("--text-input-enabled", action="store_true", help="Enable UDP text input (skip FasterWhisper).")
    parser.add_argument("--text-input-host", default="127.0.0.1", help="UDP bind host for text input.")
    parser.add_argument("--text-input-port", type=int, default=17912, help="UDP bind port for text input.")
    parser.add_argument("--text-input-token", default="", help="Optional auth token for text input.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_download_root = os.getenv("FASTER_WHISPER_DOWNLOAD_ROOT", "").strip()
    env_grok_script = os.getenv("GROK_BRIDGE_SCRIPT", "").strip()
    env_grok_python = os.getenv("GROK_BRIDGE_PYTHON", "").strip()
    cli_download_root = args.download_root.strip()
    cli_grok_script = args.grok_script.strip()
    cli_grok_python = args.grok_python.strip()
    download_root_text = cli_download_root if cli_download_root != "" else env_download_root
    grok_script_text = cli_grok_script if cli_grok_script != "" else env_grok_script
    grok_python_text = cli_grok_python if cli_grok_python != "" else env_grok_python

    cfg = WatchTranscriberConfig(
        watch_dir=Path(args.watch_dir).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        model_size_or_path=args.model.strip(),
        device=args.device.strip(),
        compute_type=args.compute_type.strip(),
        language=args.language.strip() if args.language.strip() != "" else None,
        download_root=Path(download_root_text).expanduser().resolve() if download_root_text != "" else None,
        beam_size=max(1, int(args.beam_size)),
        send_to_grok=bool(args.send_to_grok),
        grok_script_path=Path(grok_script_text).expanduser().resolve() if grok_script_text != "" else None,
        grok_python_path=Path(grok_python_text).expanduser().resolve() if grok_python_text != "" else None,
        grok_timeout_seconds=max(1.0, float(args.grok_timeout)),
        text_input_enabled=bool(args.text_input_enabled),
        text_input_host=args.text_input_host.strip() or "127.0.0.1",
        text_input_port=max(1, int(args.text_input_port)),
        text_input_token=args.text_input_token,
    )
    app = WavWatchTranscriber(cfg)
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()
    except Exception as exc:
        print(f"[error] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
