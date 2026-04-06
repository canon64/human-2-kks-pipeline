#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import queue
import signal
import socket
import sys
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import sounddevice as sd


@dataclass
class RecorderConfig:
    output_dir: Path
    sample_rate: int
    block_ms: int
    threshold_dbfs: float
    silence_seconds: float
    min_duration_seconds: float
    device: Optional[int | str]
    pre_roll_seconds: float = 0.5
    post_roll_seconds: float = 0.5
    tcp_host: str = ""
    tcp_port: int = 17890
    tcp_token: str = ""
    tcp_timeout_seconds: float = 20.0
    external_control_enabled: bool = False
    external_control_host: str = "127.0.0.1"
    external_control_port: int = 17911
    external_control_token: str = ""


def get_input_devices() -> list[tuple[int, str]]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    results: list[tuple[int, str]] = []
    for idx, device in enumerate(devices):
        max_input_channels = int(device.get("max_input_channels", 0))
        if max_input_channels <= 0:
            continue
        host_api_idx = int(device.get("hostapi", -1))
        host_api_name = ""
        if 0 <= host_api_idx < len(host_apis):
            host_api_name = str(host_apis[host_api_idx].get("name", ""))
        name = str(device.get("name", f"Device {idx}"))
        label = f"[{idx}] {name}" if host_api_name == "" else f"[{idx}] {name} ({host_api_name})"
        results.append((idx, label))
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Threshold-based microphone recorder. "
            "Start on threshold, stop after silence, discard short clips."
        )
    )
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parent.parent / "outputs" / "wav"), help="Saved WAV directory.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Sample rate (Hz).")
    parser.add_argument("--block-ms", type=int, default=100, help="Chunk size in milliseconds.")
    parser.add_argument(
        "--threshold-dbfs",
        type=float,
        default=-35.0,
        help="Start/voice threshold in dBFS. Example: -35",
    )
    parser.add_argument(
        "--silence-seconds",
        type=float,
        default=2.0,
        help="Stop recording after this much continuous silence.",
    )
    parser.add_argument(
        "--min-duration-seconds",
        type=float,
        default=3.0,
        help="Discard clips with effective duration <= this value.",
    )
    parser.add_argument(
        "--pre-roll-seconds",
        type=float,
        default=0.5,
        help="Keep this much audio before threshold crossing.",
    )
    parser.add_argument(
        "--post-roll-seconds",
        type=float,
        default=0.5,
        help="Keep this much trailing audio when stopping by silence.",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Input device index or name. Default uses OS default input device.",
    )
    parser.add_argument("--tcp-host", default="", help="Send saved wav to this TCP receiver host.")
    parser.add_argument("--tcp-port", type=int, default=17890, help="TCP receiver port.")
    parser.add_argument("--tcp-token", default="", help="Optional auth token for TCP receiver.")
    parser.add_argument("--tcp-timeout", type=float, default=20.0, help="TCP send timeout seconds.")
    parser.add_argument("--external-control-enabled", action="store_true", help="Enable UDP external start/stop control.")
    parser.add_argument("--external-control-host", default="127.0.0.1", help="UDP bind host for external control.")
    parser.add_argument("--external-control-port", type=int, default=17911, help="UDP bind port for external control.")
    parser.add_argument("--external-control-token", default="", help="Optional auth token for external control.")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print available devices and exit.",
    )
    return parser.parse_args()


def dbfs_from_int16(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    normalized = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(normalized * normalized)))
    if rms <= 1e-12:
        return -120.0
    return 20.0 * math.log10(rms)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_line_bytes(sock_file, max_bytes: int) -> bytes:
    buf = bytearray()
    while len(buf) < max_bytes:
        ch = sock_file.read(1)
        if not ch:
            break
        if ch == b"\n":
            break
        buf.extend(ch)
    if len(buf) >= max_bytes:
        raise RuntimeError("tcp response too long")
    return bytes(buf)


def send_wav_tcp(
    wav_path: Path,
    host: str,
    port: int,
    token: str = "",
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    target_host = (host or "").strip()
    if target_host == "":
        raise ValueError("tcp host is empty")

    path = Path(wav_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"wav not found: {path}")

    size = int(path.stat().st_size)
    sha256 = _sha256_file(path)
    header = {
        "type": "wav_upload",
        "name": path.name,
        "size": size,
        "sha256": sha256,
        "token": token,
        "sent_at": datetime.now().isoformat(timespec="seconds"),
    }

    with socket.create_connection((target_host, int(port)), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        writer = sock.makefile("wb")
        try:
            header_line = (json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            writer.write(header_line)
            writer.flush()

            with path.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    sock.sendall(chunk)
            sock.shutdown(socket.SHUT_WR)

            reader = sock.makefile("rb")
            try:
                response_line = _read_line_bytes(reader, 1024 * 256)
            finally:
                reader.close()

            if not response_line:
                raise RuntimeError("empty tcp receiver response")
            payload = json.loads(response_line.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid response payload: {payload}")
            return payload
        finally:
            writer.close()


class VoiceGateRecorder:
    def __init__(
        self,
        config: RecorderConfig,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = config
        self.log_callback = log_callback
        # Unbounded queue to avoid dropping captured chunks.
        self.audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self._running = True

        self.recording = False
        self.segment_frames: list[np.ndarray] = []
        self.segment_total_samples = 0
        self.trailing_silence_samples = 0
        self.segment_peak_dbfs = -120.0
        self.segment_started_at: Optional[datetime] = None

        self.silence_limit_samples = int(self.cfg.silence_seconds * self.cfg.sample_rate)
        self.pre_roll_limit_samples = int(self.cfg.pre_roll_seconds * self.cfg.sample_rate)
        self.post_roll_keep_samples = int(self.cfg.post_roll_seconds * self.cfg.sample_rate)
        self.block_samples = int(self.cfg.sample_rate * self.cfg.block_ms / 1000.0)
        self.pre_roll_frames: deque[np.ndarray] = deque()
        self.pre_roll_total_samples = 0

        # 音声レベル定期ログ用
        self._level_log_next_time: float = 0.0
        self._level_log_interval_sec: float = 5.0
        self._level_window_peak: float = -120.0

        self._tcp_send_queue: queue.Queue[Optional[Path]] = queue.Queue(maxsize=256)
        self._tcp_send_thread: Optional[threading.Thread] = None
        self._control_q: queue.Queue[str] = queue.Queue(maxsize=128)
        self._control_thread: Optional[threading.Thread] = None
        self._control_socket: Optional[socket.socket] = None
        self.external_hold_active = False

    def _log(self, message: str) -> None:
        print(message)
        if self.log_callback is not None:
            self.log_callback(message)

    def _tcp_enabled(self) -> bool:
        return (self.cfg.tcp_host or "").strip() != ""

    def _external_control_enabled(self) -> bool:
        return bool(self.cfg.external_control_enabled)

    def _start_tcp_sender(self) -> None:
        if not self._tcp_enabled():
            return
        if self._tcp_send_thread is not None:
            return

        self._tcp_send_thread = threading.Thread(target=self._tcp_send_loop, name="VoiceGateRecorder.tcp_send", daemon=True)
        self._tcp_send_thread.start()
        self._log(
            f"[tcp] sender started host={self.cfg.tcp_host} port={self.cfg.tcp_port} timeout={self.cfg.tcp_timeout_seconds}s"
        )

    def _stop_tcp_sender(self) -> None:
        if self._tcp_send_thread is None:
            return
        try:
            self._tcp_send_queue.put_nowait(None)
        except queue.Full:
            pass
        self._tcp_send_thread.join(timeout=2.0)
        self._tcp_send_thread = None
        self._log("[tcp] sender stopped")

    def _enqueue_tcp_send(self, wav_path: Path) -> None:
        if not self._tcp_enabled():
            return
        try:
            self._tcp_send_queue.put_nowait(wav_path)
            self._log(f"[tcp] queued {wav_path.name}")
        except queue.Full:
            self._log(f"[warn] tcp queue full, drop send: {wav_path.name}")

    def _start_external_control_listener(self) -> None:
        if not self._external_control_enabled():
            return
        if self._control_thread is not None:
            return

        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="VoiceGateRecorder.external_control",
            daemon=True,
        )
        self._control_thread.start()
        self._log(
            "[ctrl] listener started "
            f"bind={self.cfg.external_control_host}:{self.cfg.external_control_port}"
        )

    def _stop_external_control_listener(self) -> None:
        if self._control_thread is None:
            return
        try:
            if self._control_socket is not None:
                self._control_socket.close()
        except Exception:
            pass
        self._control_thread.join(timeout=2.0)
        self._control_thread = None
        self._control_socket = None
        self._log("[ctrl] listener stopped")

    def _parse_control_packet(self, data: bytes) -> tuple[str, str]:
        text = data.decode("utf-8", errors="replace").strip()
        if text == "":
            return "", ""

        # Preferred packet format:
        # {"cmd":"start|stop","token":"..."}
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except Exception:
                return "", ""
            cmd = str(payload.get("cmd", "")).strip().lower()
            token = str(payload.get("token", "")).strip()
            return cmd, token

        # Fallback plain-text command: "start" / "stop".
        return text.lower(), ""

    def _control_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._control_socket = sock
        sock.settimeout(0.5)
        sock.bind((self.cfg.external_control_host, int(self.cfg.external_control_port)))

        required_token = (self.cfg.external_control_token or "").strip()
        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                self._log(f"[error] control recv failed: {exc}")
                continue

            cmd, token = self._parse_control_packet(data)
            if cmd not in ("start", "stop"):
                continue

            if required_token != "" and token != required_token:
                self._log(f"[warn] control token mismatch from {addr[0]}")
                continue

            try:
                self._control_q.put_nowait(cmd)
                self._log(f"[ctrl] queued {cmd} from {addr[0]}:{addr[1]}")
            except queue.Full:
                self._log(f"[warn] control queue full, drop: {cmd}")

    def _drain_control_queue(self) -> None:
        if not self._external_control_enabled():
            return
        while True:
            try:
                cmd = self._control_q.get_nowait()
            except queue.Empty:
                break

            if cmd == "start":
                if not self.external_hold_active:
                    self.external_hold_active = True
                    self._log("[ctrl] start accepted")
            elif cmd == "stop":
                if self.external_hold_active:
                    self.external_hold_active = False
                    self._log("[ctrl] stop accepted")
                if self.recording:
                    self.finalize_segment(force=True)

    def _tcp_send_loop(self) -> None:
        while self._running:
            try:
                item = self._tcp_send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break

            try:
                payload = send_wav_tcp(
                    wav_path=item,
                    host=self.cfg.tcp_host,
                    port=self.cfg.tcp_port,
                    token=self.cfg.tcp_token,
                    timeout_seconds=self.cfg.tcp_timeout_seconds,
                )
                if bool(payload.get("ok", False)):
                    saved_path = str(payload.get("saved_path", "")).strip()
                    self._log(f"[tcp] sent {item.name} -> {saved_path if saved_path else '(ok)'}")
                else:
                    self._log(f"[error] tcp send rejected: {item.name} ({payload.get('error', '')})")
            except Exception as exc:
                self._log(f"[error] tcp send failed: {item.name} ({exc})")

    def stop(self) -> None:
        self._running = False

    def audio_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            self._log(f"[warn] stream status: {status}")
        mono = indata[:, 0].copy()
        self.audio_q.put_nowait(mono)

    def _log_device_info(self) -> None:
        """選択デバイスの情報をログ出力する。"""
        device = self.cfg.device
        try:
            if device is None:
                info = sd.query_devices(kind="input")
                self._log(f"[recorder] device=system_default name={info.get('name', '?')!r} "
                          f"default_sr={info.get('default_samplerate', '?')} "
                          f"max_in_ch={info.get('max_input_channels', '?')}")
            else:
                info = sd.query_devices(device)
                host_api_idx = int(info.get("hostapi", -1))
                host_apis = sd.query_hostapis()
                host_api_name = ""
                if 0 <= host_api_idx < len(host_apis):
                    host_api_name = str(host_apis[host_api_idx].get("name", ""))
                self._log(f"[recorder] device={device} name={info.get('name', '?')!r} "
                          f"host_api={host_api_name!r} "
                          f"default_sr={info.get('default_samplerate', '?')} "
                          f"max_in_ch={info.get('max_input_channels', '?')}")
                requested_sr = self.cfg.sample_rate
                default_sr = int(info.get("default_samplerate", 0))
                if default_sr > 0 and default_sr != requested_sr:
                    self._log(
                        f"[recorder][warn] requested sample_rate={requested_sr} "
                        f"differs from device default {default_sr}. "
                        f"WASAPI devices may reject non-native rates. "
                        f"If open fails, try changing to {default_sr}."
                    )
        except Exception as exc:
            self._log(f"[recorder][warn] could not query device info: {exc}")

    @staticmethod
    def _classify_portaudio_error(exc: Exception) -> str:
        msg = str(exc).lower()
        if "invalid sample rate" in msg or "sample rate" in msg:
            return (
                "サンプルレートがデバイスの対応レートと一致しません。"
                "WASAPIデバイスの場合、デバイスのデフォルトレート（通常48000）に変更してください。"
            )
        if "invalid number of channels" in msg or "channels" in msg:
            return (
                "チャンネル数がデバイスに対応していません。"
                "デバイスがステレオ専用の場合があります。"
            )
        if "unanticipated host error" in msg or "host error" in msg:
            return (
                "ホストAPIエラー。Windowsのマイクプライバシー設定で"
                "「デスクトップアプリがマイクにアクセスできるようにする」がOFFの可能性があります。"
                "（設定 → プライバシーとセキュリティ → マイク）"
            )
        if "no default input" in msg or "no input" in msg or "invalid device" in msg:
            return "指定したデバイスが見つかりません。デバイスリストを更新してください。"
        if "device unavailable" in msg or "busy" in msg:
            return "デバイスが他のアプリに占有されています。"
        return ""

    def run(self) -> None:
        if self.block_samples <= 0:
            raise ValueError("block size must be positive.")
        if self.silence_limit_samples <= 0:
            raise ValueError("silence limit must be positive.")
        if self.pre_roll_limit_samples < 0:
            raise ValueError("pre-roll must be non-negative.")
        if self.post_roll_keep_samples < 0:
            raise ValueError("post-roll must be non-negative.")

        self._log_device_info()
        self._log(
            f"[recorder] opening stream: device={self.cfg.device!r} "
            f"sample_rate={self.cfg.sample_rate} channels=1 dtype=int16 "
            f"blocksize={self.block_samples} latency=low"
        )

        self._start_tcp_sender()
        self._start_external_control_listener()
        try:
            try:
                stream = sd.InputStream(
                    samplerate=self.cfg.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self.block_samples,
                    device=self.cfg.device,
                    callback=self.audio_callback,
                    latency="low",
                )
            except Exception as open_exc:
                hint = self._classify_portaudio_error(open_exc)
                self._log(f"[recorder][error] ストリームのオープンに失敗しました: {open_exc}")
                if hint:
                    self._log(f"[recorder][hint] {hint}")
                raise

            with stream:
                self._log(
                    f"[recorder] ストリームオープン成功 active={stream.active} "
                    f"device={stream.device} samplerate={stream.samplerate}"
                )
                self._log(
                    "[info] Listening. "
                    f"threshold={self.cfg.threshold_dbfs} dBFS, "
                    f"silence={self.cfg.silence_seconds}s, min={self.cfg.min_duration_seconds}s, "
                    f"pre-roll={self.cfg.pre_roll_seconds}s, post-roll={self.cfg.post_roll_seconds}s"
                )
                if self._tcp_enabled():
                    self._log(f"[info] TCP send target={self.cfg.tcp_host}:{self.cfg.tcp_port}")
                if self._external_control_enabled():
                    self._log("[info] External control mode is enabled (push-to-talk by start/stop command).")
                self._log("[info] Press Ctrl+C to stop.")
                import time as _time
                self._level_log_next_time = _time.monotonic() + self._level_log_interval_sec
                while self._running:
                    self._drain_control_queue()
                    try:
                        chunk = self.audio_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    self.process_chunk(chunk)

            # Finalize pending segment on shutdown.
            if self.recording:
                self.finalize_segment(force=True)
        finally:
            self._stop_tcp_sender()
            self._stop_external_control_listener()

    def append_pre_roll(self, chunk: np.ndarray) -> None:
        if self.pre_roll_limit_samples <= 0:
            return
        self.pre_roll_frames.append(chunk)
        self.pre_roll_total_samples += len(chunk)
        while self.pre_roll_total_samples > self.pre_roll_limit_samples and self.pre_roll_frames:
            removed = self.pre_roll_frames.popleft()
            self.pre_roll_total_samples -= len(removed)

    def _maybe_log_level(self, current_dbfs: float) -> None:
        import time as _time
        now = _time.monotonic()
        if current_dbfs > self._level_window_peak:
            self._level_window_peak = current_dbfs
        if now >= self._level_log_next_time:
            bar_len = 20
            ratio = max(0.0, min(1.0, (self._level_window_peak - (-80.0)) / 80.0))
            bar = "#" * int(ratio * bar_len) + "." * (bar_len - int(ratio * bar_len))
            status = "録音中" if self.recording else "待機中"
            self._log(
                f"[level] {status} peak={self._level_window_peak:.1f}dBFS "
                f"threshold={self.cfg.threshold_dbfs:.1f}dBFS [{bar}]"
            )
            self._level_window_peak = -120.0
            self._level_log_next_time = now + self._level_log_interval_sec

    def process_chunk(self, chunk: np.ndarray) -> None:
        current_dbfs = dbfs_from_int16(chunk)
        is_voice = current_dbfs >= self.cfg.threshold_dbfs

        self._maybe_log_level(current_dbfs)

        if self._external_control_enabled():
            self._process_chunk_external(chunk, current_dbfs)
            return

        if not self.recording:
            self.append_pre_roll(chunk)
            if is_voice:
                self.recording = True
                if self.pre_roll_frames:
                    self.segment_frames = list(self.pre_roll_frames)
                    self.segment_total_samples = self.pre_roll_total_samples
                else:
                    self.segment_frames = [chunk]
                    self.segment_total_samples = len(chunk)
                self.trailing_silence_samples = 0
                self.segment_peak_dbfs = current_dbfs
                self.segment_started_at = datetime.now()
                self._log(f"[start] {self.segment_started_at:%H:%M:%S} {current_dbfs:.1f} dBFS")
                self.pre_roll_frames.clear()
                self.pre_roll_total_samples = 0
            return

        self.segment_frames.append(chunk)
        self.segment_total_samples += len(chunk)
        if current_dbfs > self.segment_peak_dbfs:
            self.segment_peak_dbfs = current_dbfs

        if is_voice:
            self.trailing_silence_samples = 0
            return

        self.trailing_silence_samples += len(chunk)
        if self.trailing_silence_samples >= self.silence_limit_samples:
            self.finalize_segment(force=False)

    def _process_chunk_external(self, chunk: np.ndarray, current_dbfs: float) -> None:
        if not self.external_hold_active:
            # Keep pre-roll so that first phoneme is less likely to be clipped
            # even if START arrives slightly late.
            self.append_pre_roll(chunk)
            return

        if not self.recording:
            self.recording = True
            if self.pre_roll_frames:
                self.segment_frames = list(self.pre_roll_frames)
                self.segment_total_samples = self.pre_roll_total_samples
            else:
                self.segment_frames = []
                self.segment_total_samples = 0

            self.trailing_silence_samples = 0
            self.segment_peak_dbfs = current_dbfs
            self.segment_started_at = datetime.now()
            self.pre_roll_frames.clear()
            self.pre_roll_total_samples = 0
            self._log(f"[start][external] {self.segment_started_at:%H:%M:%S} {current_dbfs:.1f} dBFS")

        self.segment_frames.append(chunk)
        self.segment_total_samples += len(chunk)
        if current_dbfs > self.segment_peak_dbfs:
            self.segment_peak_dbfs = current_dbfs

    def finalize_segment(self, force: bool) -> None:
        data = np.concatenate(self.segment_frames) if self.segment_frames else np.array([], dtype=np.int16)
        if data.size == 0:
            self.reset_segment()
            return

        # Remove trailing silence when stopped by silence detector.
        effective_samples = data
        if not force and self.trailing_silence_samples > 0:
            trim_samples = max(0, self.trailing_silence_samples - self.post_roll_keep_samples)
            keep = max(0, len(data) - trim_samples)
            effective_samples = data[:keep]

        duration = len(effective_samples) / float(self.cfg.sample_rate)
        if duration <= self.cfg.min_duration_seconds:
            self._log(
                f"[discard] duration={duration:.2f}s <= {self.cfg.min_duration_seconds:.2f}s "
                f"(peak={self.segment_peak_dbfs:.1f} dBFS)"
            )
            self.reset_segment()
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out_path = self.cfg.output_dir / f"voice_{ts}_{duration:.2f}s.wav"
        write_wav(out_path, effective_samples, self.cfg.sample_rate)
        self._log(f"[saved] {out_path} ({duration:.2f}s, peak={self.segment_peak_dbfs:.1f} dBFS)")
        self._enqueue_tcp_send(out_path)
        self.reset_segment()

    def reset_segment(self) -> None:
        self.recording = False
        self.segment_frames = []
        self.segment_total_samples = 0
        self.trailing_silence_samples = 0
        self.segment_peak_dbfs = -120.0
        self.segment_started_at = None


def parse_device(device_arg: Optional[str]) -> Optional[int | str]:
    if device_arg is None:
        return None
    stripped = device_arg.strip()
    if stripped == "":
        return None
    if stripped.isdigit():
        return int(stripped)
    return stripped


def list_devices() -> None:
    print("Input devices:")
    print("  default: System default input device")
    for idx, label in get_input_devices():
        print(f"  {idx}: {label}")


def main() -> int:
    args = parse_args()
    if args.list_devices:
        list_devices()
        return 0

    cfg = RecorderConfig(
        output_dir=Path(args.output_dir),
        sample_rate=args.sample_rate,
        block_ms=args.block_ms,
        threshold_dbfs=args.threshold_dbfs,
        silence_seconds=args.silence_seconds,
        min_duration_seconds=args.min_duration_seconds,
        device=parse_device(args.device),
        pre_roll_seconds=max(0.0, args.pre_roll_seconds),
        post_roll_seconds=max(0.0, args.post_roll_seconds),
        tcp_host=args.tcp_host.strip(),
        tcp_port=max(1, int(args.tcp_port)),
        tcp_token=args.tcp_token,
        tcp_timeout_seconds=max(1.0, float(args.tcp_timeout)),
        external_control_enabled=bool(args.external_control_enabled),
        external_control_host=args.external_control_host.strip() or "127.0.0.1",
        external_control_port=max(1, int(args.external_control_port)),
        external_control_token=args.external_control_token,
    )

    recorder = VoiceGateRecorder(cfg)

    def _signal_handler(sig, frame) -> None:
        recorder.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        recorder.run()
    except KeyboardInterrupt:
        recorder.stop()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
