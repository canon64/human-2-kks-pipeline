from __future__ import annotations

import hashlib
import json
import socket
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _log_default(_: str) -> None:
    return


def _sanitize_filename(name: str) -> str:
    text = (name or "").strip()
    if not text:
        text = f"audio_{_now_stamp()}.wav"

    safe_chars: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe_chars.append(ch)
        else:
            safe_chars.append("_")
    safe_name = "".join(safe_chars).strip("._")
    if not safe_name:
        safe_name = f"audio_{_now_stamp()}"
    if not safe_name.lower().endswith(".wav"):
        safe_name += ".wav"
    return safe_name


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
        raise RuntimeError("header line too large")
    return bytes(buf)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _unique_target_path(output_dir: Path, name: str) -> Path:
    candidate = output_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    return output_dir / f"{stem}_{_now_stamp()}{suffix}"


def send_wav_file(
    host: str,
    port: int,
    wav_path: Path,
    token: str = "",
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    host_text = (host or "").strip()
    if not host_text:
        raise ValueError("host is empty")

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

    with socket.create_connection((host_text, int(port)), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        writer = sock.makefile("wb")
        try:
            line = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            writer.write(line)
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
                raise RuntimeError("empty response from receiver")

            payload = json.loads(response_line.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid response payload: {payload}")
            return payload
        finally:
            writer.close()


@dataclass
class TcpWavReceiverConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 17890
    output_dir: Path = Path(".")
    token: str = ""
    max_file_bytes: int = 64 * 1024 * 1024
    io_timeout_seconds: float = 30.0


class TcpWavReceiver:
    def __init__(
        self,
        config: TcpWavReceiverConfig,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = config
        self.log = log_callback or _log_default
        self._running = False
        self._server: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._client_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return

        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.cfg.bind_host, int(self.cfg.bind_port)))
        server.listen(16)

        self._server = server
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, name="TcpWavReceiver.accept", daemon=True)
        self._accept_thread.start()

        self.log(
            "[tcp_recv] listening "
            + f"{self.cfg.bind_host}:{int(self.cfg.bind_port)} "
            + f"out={self.cfg.output_dir} "
            + f"auth={'on' if self.cfg.token else 'off'}"
        )

    def stop(self) -> None:
        self._running = False

        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1.5)
            self._accept_thread = None

        with self._lock:
            threads = list(self._client_threads)
            self._client_threads.clear()

        for t in threads:
            t.join(timeout=1.5)

        self.log("[tcp_recv] stopped")

    def _accept_loop(self) -> None:
        while self._running:
            server = self._server
            if server is None:
                break
            try:
                client, addr = server.accept()
            except OSError:
                break
            except Exception as exc:
                if self._running:
                    self.log(f"[tcp_recv] accept error: {exc}")
                continue

            t = threading.Thread(
                target=self._handle_client,
                args=(client, addr),
                name="TcpWavReceiver.client",
                daemon=True,
            )
            with self._lock:
                self._client_threads.append(t)
            t.start()

    def _send_response(self, client: socket.socket, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        client.sendall(line)

    def _handle_client(self, client: socket.socket, addr) -> None:
        host = f"{addr[0]}:{addr[1]}"
        temp_path: Optional[Path] = None
        final_path: Optional[Path] = None

        try:
            client.settimeout(float(self.cfg.io_timeout_seconds))
            reader = client.makefile("rb")
            try:
                header_line = _read_line_bytes(reader, 1024 * 256)
                if not header_line:
                    raise RuntimeError("missing header")

                header = json.loads(header_line.decode("utf-8", errors="replace"))
                if not isinstance(header, dict):
                    raise RuntimeError("header is not object")

                if str(header.get("type", "")) != "wav_upload":
                    raise RuntimeError("unsupported packet type")

                recv_token = str(header.get("token", ""))
                if self.cfg.token and recv_token != self.cfg.token:
                    self._send_response(client, {"ok": False, "error": "unauthorized"})
                    return

                raw_size = int(header.get("size", -1))
                if raw_size < 0:
                    raise RuntimeError("invalid size")
                if raw_size > int(self.cfg.max_file_bytes):
                    raise RuntimeError(f"file too large: {raw_size} > {self.cfg.max_file_bytes}")

                name = _sanitize_filename(str(header.get("name", "")))
                expected_sha = str(header.get("sha256", "")).strip().lower()

                final_path = _unique_target_path(self.cfg.output_dir, name)
                temp_path = final_path.with_suffix(final_path.suffix + ".part")

                remaining = raw_size
                h = hashlib.sha256()
                with temp_path.open("wb") as out_f:
                    while remaining > 0:
                        chunk = reader.read(min(1024 * 256, remaining))
                        if not chunk:
                            raise RuntimeError("unexpected EOF while receiving wav")
                        out_f.write(chunk)
                        h.update(chunk)
                        remaining -= len(chunk)

                actual_sha = h.hexdigest().lower()
                if expected_sha and actual_sha != expected_sha:
                    raise RuntimeError("sha256 mismatch")

                temp_path.replace(final_path)
                temp_path = None

                self._send_response(
                    client,
                    {
                        "ok": True,
                        "saved_path": str(final_path),
                        "size": raw_size,
                        "sha256": actual_sha,
                    },
                )
                self.log(f"[tcp_recv] saved {final_path} from {host} size={raw_size}")
            finally:
                reader.close()
        except Exception as exc:
            try:
                self._send_response(client, {"ok": False, "error": str(exc)})
            except Exception:
                pass
            self.log(f"[tcp_recv] error from {host}: {exc}")
        finally:
            if temp_path is not None:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
            try:
                client.close()
            except Exception:
                pass

            current = threading.current_thread()
            with self._lock:
                self._client_threads = [t for t in self._client_threads if t is not current]
