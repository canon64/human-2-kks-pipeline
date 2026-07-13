from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import wave
from typing import Callable, Optional

import numpy as np


CLIENT_ROOT = Path(r"J:\tools\scripts\rtfw_lan_client")
COMMON_ENV = Path(r"J:\tools\api-scripts\runtime\.env")


def _load_shared_token() -> None:
    if os.getenv("RTFW_LAN_TOKEN") or not COMMON_ENV.exists():
        return
    for raw in COMMON_ENV.read_text(encoding="utf-8-sig").splitlines():
        if raw.strip().startswith("RTFW_LAN_TOKEN="):
            os.environ["RTFW_LAN_TOKEN"] = raw.split("=", 1)[1].strip().strip('"').strip("'")
            return


def _client_types():
    if str(CLIENT_ROOT) not in sys.path:
        sys.path.insert(0, str(CLIENT_ROOT))
    from rtfw_lan_client.config import ClientConfig
    from rtfw_lan_client.lan import LanTranscriptionClient
    return ClientConfig, LanTranscriptionClient


def wav_to_pcm16_mono_16k(path: Path) -> bytes:
    target = Path(path).expanduser().resolve()
    with wave.open(str(target), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    if sample_width != 2:
        raise ValueError(f"RTFW requires PCM16 WAV: sample_width={sample_width}")
    samples = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if sample_rate != 16000:
        if samples.size == 0:
            raise ValueError("WAV contains no audio")
        old_positions = np.arange(samples.size, dtype=np.float64)
        new_length = max(1, round(samples.size * 16000 / sample_rate))
        new_positions = np.linspace(0, samples.size - 1, new_length)
        samples = np.interp(new_positions, old_positions, samples).astype(np.int16)
    return samples.astype("<i2", copy=False).tobytes()


async def _transcribe_remote(
    *,
    host: str,
    port: int,
    pcm: bytes,
    timeout: float,
    status: Callable[[dict], None],
) -> dict:
    _load_shared_token()
    ClientConfig, LanTranscriptionClient = _client_types()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        if str(event.get("type") or "") == "transcript.partial":
            status({"stage": "partial", "partialChars": len(str(event.get("text") or ""))})
        events.append(dict(event))

    config = ClientConfig(
        host=str(host).strip(),
        port=int(port),
        source="mic",
        mode="remote",
        stream_id="human-2-kks-clip",
    )
    config.validate()
    client = LanTranscriptionClient(config, on_event)
    status({"stage": "connect", "connected": False, "authorized": False})
    await client.start()
    try:
        await client.wait_authorized(min(10.0, timeout))
        status({"stage": "auth", "connected": True, "authorized": True})
        result = await client.transcribe_pcm_clip(
            pcm,
            source="mic",
            timeout=timeout,
            progress=lambda payload: status(dict(payload)),
        )
        client_status = client.status()
        status({
            "stage": "final",
            "connected": client_status.get("connected", False),
            "authorized": client_status.get("authorized", False),
            "acked": client_status.get("acked", 0),
            "dropped": client_status.get("dropped", 0),
            "sequence": result.get("sequence"),
            "eventId": result.get("eventId", ""),
            "textChars": len(str(result.get("text") or "")),
        })
        return {
            "ok": True,
            "text": str(result.get("text") or "").strip(),
            "event": result,
            "backend": "rtfw_lan",
            "acked": client_status.get("acked", 0),
            "dropped": client_status.get("dropped", 0),
        }
    finally:
        await client.stop()


def transcribe_wav_rtfw(
    wav_path: Path,
    *,
    host: str,
    port: int,
    timeout: float = 150.0,
    status_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    status = status_callback or (lambda _payload: None)
    pcm = wav_to_pcm16_mono_16k(wav_path)
    status({"stage": "wav_ready", "pcmBytes": len(pcm)})
    try:
        return asyncio.run(_transcribe_remote(
            host=host,
            port=port,
            pcm=pcm,
            timeout=max(1.0, float(timeout)),
            status=status,
        ))
    except Exception as exc:
        status({"stage": "error", "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "backend": "rtfw_lan"}


def probe_rtfw(*, host: str, port: int, timeout: float = 10.0) -> dict:
    _load_shared_token()
    ClientConfig, LanTranscriptionClient = _client_types()

    async def run() -> dict:
        async def ignore(_event: dict) -> None:
            return None

        config = ClientConfig(host=str(host).strip(), port=int(port), source="mic", mode="remote", stream_id="human-2-kks-probe")
        config.validate()
        client = LanTranscriptionClient(config, ignore)
        await client.start()
        try:
            await client.wait_authorized(timeout)
            return {"ok": True, "connected": client.connected, "authorized": client.authorized}
        finally:
            await client.stop()

    try:
        return asyncio.run(run())
    except Exception as exc:
        return {"ok": False, "connected": False, "authorized": False, "error": f"{type(exc).__name__}: {exc}"}


def dispatch_transcription(
    cfg,
    wav_path: Path,
    *,
    local_transcriber: Callable[[Path], dict],
    status_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    backend = str(getattr(cfg, "fw_backend", "local") or "local")
    if backend == "rtfw_lan":
        return transcribe_wav_rtfw(
            wav_path,
            host=str(getattr(cfg, "rtfw_host", "192.168.11.6")),
            port=int(getattr(cfg, "rtfw_port", 8766)),
            status_callback=status_callback,
        )
    return local_transcriber(Path(wav_path))
