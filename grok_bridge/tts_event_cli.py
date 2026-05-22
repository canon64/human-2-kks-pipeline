from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_or_create_config, resolve_config_path, runtime_base_dir
from .io_utf8 import force_stdio_utf8, with_utf8_env
from .llm_providers import LlmRequestConfig, generate_llm_response, normalize_backend
from .logging_utils import setup_logger
from core.sd_prompt_bridge import extract_sd_prompt_block, send_a1111_txt2img


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _split_long_line(line: str, max_chars: int) -> list[str]:
    text = (line or "").strip()
    if not text:
        return []

    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return [text]

    break_chars = "。！？!?、,，；;：:」』）)] "
    min_break = max(1, int(limit * 0.45))
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = 0
        scan_to = min(limit, len(rest) - 1)
        for idx in range(scan_to, min_break - 1, -1):
            if rest[idx - 1] in break_chars:
                cut = idx
                break
        if cut <= 0:
            cut = limit
        chunk = rest[:cut].strip()
        if chunk:
            chunks.append(chunk)
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def _split_response_lines(response: str, max_line_chars: int = 0) -> list[str]:
    lines = [(line or "").strip() for line in response.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        compact = (response or "").strip()
        lines = [compact] if compact else []
    if max_line_chars and max_line_chars > 0:
        split_lines: list[str] = []
        for line in lines:
            split_lines.extend(_split_long_line(line, max_line_chars))
        return split_lines
    return lines


def _limit_response_text(
    response: str,
    *,
    max_chars: int,
    logger,
    source: str,
) -> tuple[str, int, int, bool]:
    requested_max = int(max_chars)
    safe_max = max(1, requested_max)
    raw_len = len(response or "")
    if requested_max <= 0:
        logger.info("grok_response_limit_config source=%s max=off raw_len=%d", source, raw_len)
        logger.info("grok_response_unlimited source=%s raw_len=%d", source, raw_len)
        logger.info("grok_response_preview source=%s text=%r", source, str(response or "")[:80])
        return str(response or ""), raw_len, raw_len, False
    logger.info("grok_response_limit_config source=%s max=%d raw_len=%d", source, safe_max, raw_len)
    if raw_len > safe_max:
        capped = str(response or "")[:safe_max]
        logger.info("grok_response_preview_before source=%s text=%r", source, str(response or "")[:80])
        logger.warning(
            "grok_response_truncated source=%s raw_len=%d max=%d cut=%d",
            source,
            raw_len,
            safe_max,
            raw_len - safe_max,
        )
        logger.info("grok_response_preview_after source=%s text=%r", source, capped[:80])
        return capped, raw_len, safe_max, True
    logger.info("grok_response_within_limit source=%s raw_len=%d max=%d", source, raw_len, safe_max)
    logger.info("grok_response_preview source=%s text=%r", source, str(response or "")[:80])
    return str(response or ""), raw_len, raw_len, False


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _normalize_face_send_mode(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token == "preset_id":
        return "preset_name"
    if token in {"game_preset", "preset_name"}:
        return token
    return "game_preset"


def _normalize_pipe_name(value: Any) -> str:
    pipe_name = str(value or "").strip()
    prefix = "\\\\.\\pipe\\"
    if pipe_name.lower().startswith(prefix.lower()):
        pipe_name = pipe_name[len(prefix) :].strip()
    if not pipe_name or pipe_name.lower() == "kks_voice_face_events_diag_0423":
        return "kks_voice_face_events"
    return pipe_name


def _safe_normalize_llm_backend(value: Any) -> str:
    try:
        return normalize_backend(str(value or "grok_browser"))
    except Exception:
        return str(value or "").strip()


def _translate_text(text: str, source: str, target: str, logger=None) -> str:
    value = str(text or "")
    if not value.strip() or not str(target or "").strip():
        return value
    try:
        from deep_translator import GoogleTranslator

        translated = GoogleTranslator(source=source or "auto", target=target).translate(value)
        return translated if translated else value
    except Exception as exc:
        if logger is not None:
            logger.warning("subtitle_translate_failed source=%s target=%s error=%s", source, target, exc)
        return value


def _apply_conversion_rules(
    response: str,
    rules: list[dict[str, Any]],
    display_only: bool = False,
    random_pick_cache: dict[str, str] | None = None,
    logger=None,
) -> str:
    converted = response
    mode = "display" if display_only else "send"
    pick_cache = random_pick_cache if random_pick_cache is not None else {}
    ordered_rules: list[tuple[int, str, dict[str, Any]]] = []
    for idx, entry in enumerate(rules):
        if not isinstance(entry, dict):
            continue
        if not _parse_bool(entry.get("enabled", True)):
            continue
        from_str = str(entry.get("from", ""))
        if not from_str:
            continue
        ordered_rules.append((idx, from_str, entry))

    ordered_rules.sort(key=lambda x: (-len(x[1]), x[0]))
    for idx, from_str, entry in ordered_rules:
        cache_key = f"{idx}:{from_str}"
        if display_only:
            if "to_display" in entry:
                to_str = str(entry.get("to_display", ""))
                # 表示用が空なら、display_applyのON/OFFに関係なく送信用と同じ候補を使う。
                if to_str == "":
                    if cache_key in pick_cache:
                        to_str = pick_cache[cache_key]
                    else:
                        fallback_value = entry.get("to_sbv2", entry.get("to_grok", entry.get("to", "")))
                        candidates = _parse_random_candidates(fallback_value)
                        to_str = random.choice(candidates) if candidates else ""
                        pick_cache[cache_key] = to_str
                    if logger is not None:
                        logger.info(
                            "conversion_display_fallback mode=%s from=%r to=%r display_apply=%s",
                            mode,
                            from_str,
                            to_str,
                            _parse_bool(entry.get("display_apply", False)),
                        )
                elif not _parse_bool(entry.get("display_apply", False)):
                    continue
            else:
                if not _parse_bool(entry.get("display_apply", False)):
                    continue
                to_str = str(entry.get("to", ""))
        else:
            if "to_sbv2" in entry:
                to_value = entry.get("to_sbv2", "")
            elif "to_grok" in entry:
                to_value = entry.get("to_grok", "")
            else:
                to_value = entry.get("to", "")
            candidates = _parse_random_candidates(to_value)
            if candidates:
                to_str = random.choice(candidates)
            else:
                to_str = ""
            pick_cache[cache_key] = to_str
            if logger is not None and len(candidates) > 1:
                logger.info(
                    "conversion_random_pick mode=%s from=%r picked=%r choices=%d",
                    mode,
                    from_str,
                    to_str,
                    len(candidates),
                )
        hit = converted.count(from_str)
        if hit <= 0:
            continue
        if logger is not None:
            if to_str == "":
                logger.warning("conversion_empty_dst mode=%s from=%r hits=%d", mode, from_str, hit)
            else:
                logger.info("conversion_applied mode=%s from=%r to=%r hits=%d", mode, from_str, to_str, hit)
        converted = converted.replace(from_str, to_str)
    return converted


def _parse_random_candidates(value: Any) -> list[str]:
    if isinstance(value, list):
        rows = [str(v).strip() for v in value]
        rows = [r for r in rows if r != ""]
        return rows

    raw = str(value or "").strip()
    if raw == "":
        return [""]

    # JSON配列形式: ["a","b","c"]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                rows = [str(v).strip() for v in parsed]
                rows = [r for r in rows if r != ""]
                if rows:
                    return rows
        except Exception:
            pass

    # 1行1候補 または "|" 区切り
    if "\n" in raw:
        rows = [r.strip() for r in raw.splitlines()]
        rows = [r for r in rows if r != ""]
        return rows if rows else [""]
    if "|" in raw:
        rows = [r.strip() for r in raw.split("|")]
        rows = [r for r in rows if r != ""]
        return rows if rows else [""]

    return [raw]


def _pick_model_file(model_dir: Path, explicit_model_file: str | None) -> Path:
    model_files = [p for p in model_dir.iterdir() if p.is_file() and p.suffix in [".safetensors", ".pth", ".pt"]]
    if not model_files:
        raise FileNotFoundError(f"No model files found: {model_dir}")

    if explicit_model_file:
        direct = model_dir / explicit_model_file
        if direct.exists():
            return direct
        for candidate in model_files:
            if candidate.name == explicit_model_file:
                return candidate
        raise FileNotFoundError(f"Requested model file not found: {explicit_model_file}")

    def score(path: Path) -> tuple[int, float]:
        match = re.search(r"_s(\d+)", path.stem)
        step = int(match.group(1)) if match else -1
        return (step, path.stat().st_mtime)

    model_files.sort(key=score, reverse=True)
    return model_files[0]


def _list_available_models(model_assets_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model_dir in sorted([d for d in model_assets_root.iterdir() if d.is_dir()], key=lambda p: p.name.lower()):
        model_files = sorted(
            [p for p in model_dir.iterdir() if p.is_file() and p.suffix in [".safetensors", ".pth", ".pt"]],
            key=lambda p: p.name.lower(),
        )
        if not model_files:
            continue
        config_path = model_dir / "config.json"
        if not config_path.exists():
            continue
        rows.append(
            {
                "name": model_dir.name,
                "file_count": len(model_files),
                "default_file": _pick_model_file(model_dir, None).name,
                "files": [p.name for p in model_files],
            }
        )
    return rows


def _write_tts_request_json(
    path: Path,
    lines: list[str],
    speaker: str,
    style: str,
    style_weight: float,
    sdp_ratio: float,
    noise: float,
    noise_w: float,
    length: float,
) -> list[Path]:
    part_names: list[str] = [f"line_{idx:03d}.wav" for idx, _ in enumerate(lines, start=1)]
    payload = {
        "defaults": {
            "language": "JP",
            "speaker": speaker,
            "style": style,
            "style_weight": style_weight,
            "sdp_ratio": sdp_ratio,
            "noise": noise,
            "noise_w": noise_w,
            "length": length,
            "line_split": False,
        },
        "items": [
            {
                "text": line,
                "output": part_name,
            }
            for line, part_name in zip(lines, part_names)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [Path(name) for name in part_names]


def _concat_wavs(input_paths: list[Path], output_path: Path, gap_ms: int) -> None:
    if not input_paths:
        raise RuntimeError("No wav files to merge")

    with wave.open(str(input_paths[0]), "rb") as first:
        channels = first.getnchannels()
        sample_width = first.getsampwidth()
        sample_rate = first.getframerate()

    gap_frames = int(sample_rate * max(0, gap_ms) / 1000.0)
    silence = b"\x00" * gap_frames * channels * sample_width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as out_wav:
        out_wav.setnchannels(channels)
        out_wav.setsampwidth(sample_width)
        out_wav.setframerate(sample_rate)

        for idx, wav_path in enumerate(input_paths):
            with wave.open(str(wav_path), "rb") as in_wav:
                in_channels = in_wav.getnchannels()
                in_width = in_wav.getsampwidth()
                in_rate = in_wav.getframerate()
                if (in_channels, in_width, in_rate) != (channels, sample_width, sample_rate):
                    raise RuntimeError(
                        f"WAV format mismatch: {wav_path} got {(in_channels, in_width, in_rate)} expected {(channels, sample_width, sample_rate)}"
                    )
                out_wav.writeframes(in_wav.readframes(in_wav.getnframes()))

            if idx < len(input_paths) - 1 and gap_frames > 0:
                out_wav.writeframes(silence)


def _wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        if rate <= 0:
            return 0.0
        return frames / float(rate)


def _round3(value: float) -> float:
    return round(float(value), 3)


def _run_subprocess(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=with_utf8_env(),
        check=True,
    )


def _send_sequence_line_event(
    *,
    event_command_base: list[str],
    run_dir: Path,
    session_id: str,
    line_index: int,
    wav_path: Path,
    display_text: str,
    duration: float,
    args: argparse.Namespace,
    event_face_send_mode: str,
    event_face_preset_name: str,
    event_face_preset_id: str,
    event_face_preset_random: bool,
    event_face: int,
    event_keep_current_face: bool,
    include_face: bool,
) -> tuple[str, str, str]:
    line_no = max(1, int(line_index))
    payload: dict[str, Any] = {
        "type": "speak_sequence" if line_no == 1 else "speak_sequence_append",
        "sessionId": session_id,
        "main": int(args.main),
        "interrupt": 1 if line_no == 1 else 0,
        "deleteAfterPlay": 0,
        "responseText": display_text,
        "lineTexts": [display_text],
        "lineDurations": [_round3(duration)],
        "lineIndexOffset": line_no - 1,
        "items": [
            {
                "index": line_no,
                "audioPath": str(wav_path),
                "subtitle": display_text,
                "durationSeconds": _round3(duration),
                "holdSeconds": _round3(max(0.1, duration + 0.2)),
            }
        ],
    }
    if args.voice_volume >= 0:
        payload["volume"] = float(args.voice_volume)
    if args.voice_pitch >= 0:
        payload["pitch"] = float(args.voice_pitch)

    if include_face:
        if event_face_send_mode == "preset_name":
            if event_face_preset_name:
                payload["facePresetName"] = event_face_preset_name
            if event_face_preset_id:
                payload["facePresetId"] = event_face_preset_id
            if event_face_preset_random:
                payload["facePresetRandom"] = 1
            if (not event_face_preset_random) and (not event_face_preset_name) and (not event_face_preset_id):
                raise RuntimeError("face_send_mode=preset_name but face_preset_name is empty")
        else:
            if event_face >= 0:
                payload["face"] = event_face
            if event_keep_current_face:
                payload["keepCurrentFace"] = 1

    event_path = run_dir / f"voice_sequence_event_{line_no:03d}.json"
    event_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result = _run_subprocess(event_command_base + ["-JsonFile", str(event_path)])
    return str(event_path), result.stdout, result.stderr


def _tts_via_http_server(
    server_url: str,
    text: str,
    model_name: str,
    model_file: str,
    speaker: str,
    style: str,
    style_weight: float,
    sdp_ratio: float,
    noise: float,
    noise_w: float,
    length: float,
    output_path: Path,
) -> None:
    """SBV2 HTTPサーバーの /voice エンドポイントを呼び出してWAVを保存する。"""
    try:
        speaker_id = int(speaker)
    except (ValueError, TypeError):
        speaker_id = 0
    params = {
        "text": text,
        "model_name": model_name,
        "speaker_id": speaker_id,
        "style": style,
        "style_weight": style_weight,
        "sdp_ratio": sdp_ratio,
        "noisew": noise_w,
        "noise": noise,
        "length": length,
    }
    model_file_name = (model_file or "").strip()
    if model_file_name:
        params["model_file"] = model_file_name
    url = server_url.rstrip("/") + "/voice?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        wav_bytes = resp.read()
    output_path.write_bytes(wav_bytes)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send text to an LLM, synthesize JP-Extra speech per response line, merge WAV, and send event."
    )
    parser.add_argument("--text", default="", help="Text to send to the selected LLM.")
    parser.add_argument("--response-text", default="", help="Use this as LLM response directly (skip LLM).")
    parser.add_argument("--max-response-chars", type=int, default=3000, help="Maximum LLM response characters to process. Set 0 to disable limit.")
    parser.add_argument("--llm-backend", default="grok_browser", help="LLM backend: grok_browser or local_openai.")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1", help="OpenAI-compatible local LLM base URL.")
    parser.add_argument("--llm-model", default="", help="OpenAI-compatible local LLM model id.")
    parser.add_argument("--llm-api-key", default="lm-studio", help="API key for local OpenAI-compatible server.")
    parser.add_argument("--llm-system-prompt", default="", help="System prompt for local OpenAI-compatible server.")
    parser.add_argument("--llm-temperature", type=float, default=0.7, help="Local LLM temperature.")
    parser.add_argument("--llm-max-tokens", type=int, default=512, help="Local LLM max_tokens.")
    parser.add_argument("--llm-timeout", type=float, default=120.0, help="Local LLM request timeout seconds.")
    parser.add_argument("--port", type=int, default=None, help="Chrome debug port (default from config).")
    parser.add_argument("--config", default=None, help="Grok bridge config path.")
    parser.add_argument("--timeout", type=float, default=None, help="Grok response timeout seconds.")
    parser.add_argument("--poll", type=float, default=None, help="Grok response poll interval seconds.")
    parser.add_argument("--settle-rounds", type=int, default=None, help="Grok stable rounds before finish.")


    parser.add_argument(
        "--sbv2-root",
        default="",
        help="Style-Bert-VITS2 root directory.",
    )
    parser.add_argument(
        "--sbv2-python",
        default="",
        help="Python executable for SBV2. Default: <sbv2-root>/venv/Scripts/python.exe",
    )
    parser.add_argument("--list-models", action="store_true", help="List available model directories and files, then exit.")
    parser.add_argument("--model-name", default="", help="SBV2 model directory name under model_assets.")
    parser.add_argument("--model-file", default="", help="SBV2 model checkpoint file name.")
    parser.add_argument("--device", default="cuda", help="SBV2 inference device (cuda/cpu/mps).")
    parser.add_argument("--sbv2-server-url", default="", help="SBV2 HTTPサーバーURL (例: http://127.0.0.1:5000)。指定するとサブプロセス起動を省略。")
    parser.add_argument("--speaker", default="0", help="SBV2 speaker id or speaker name.")
    parser.add_argument("--style", default="Neutral", help="SBV2 style name.")
    parser.add_argument("--style-weight", type=float, default=1.0, help="SBV2 style weight.")
    parser.add_argument("--sdp-ratio", type=float, default=0.2, help="SBV2 sdp ratio.")
    parser.add_argument("--noise", type=float, default=0.6, help="SBV2 noise.")
    parser.add_argument("--noise-w", type=float, default=0.8, help="SBV2 noise_w.")
    parser.add_argument("--length", type=float, default=1.0, help="SBV2 length scale.")

    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output base directory (relative to GROK_BRIDGE_HOME/runtime dir or absolute).",
    )
    parser.add_argument("--line-gap-ms", type=int, default=300, help="Gap milliseconds between merged line wavs.")
    parser.add_argument("--max-line-chars", type=int, default=280, help="Split long response lines around punctuation before TTS. 0 disables.")
    parser.add_argument("--voice-volume", type=float, default=-1.0, help="External voice playback volume (0-1, -1=bridge default).")
    parser.add_argument("--voice-pitch", type=float, default=-1.0, help="External voice playback pitch (0.1-3, -1=bridge default).")

    _default_sender = str(Path(__file__).resolve().parent.parent / "send_voice_face_event.ps1")
    parser.add_argument(
        "--event-sender",
        default=_default_sender,
        help="PowerShell sender script path.",
    )
    parser.add_argument("--pipe-name", default="kks_voice_face_events", help="Named pipe name.")
    parser.add_argument("--target-host", default="", help="Remote bridge host. Empty uses local named pipe send.")
    parser.add_argument("--target-port", type=int, default=18765, help="Remote bridge port.")
    parser.add_argument("--target-endpoint", default="/voice-face-event", help="Remote bridge endpoint path.")
    parser.add_argument("--target-token", default="", help="Remote bridge token sent via X-Auth-Token header.")
    parser.add_argument("--remote-http", action="store_true", help="Force HTTP bridge transport mode.")
    parser.add_argument("--sd-prompt-begin-tag", default="[SD_PROMPT_BEGIN]", help="Begin marker for SD prompt block in LLM response.")
    parser.add_argument("--sd-prompt-end-tag", default="[SD_PROMPT_END]", help="End marker for SD prompt block in LLM response.")
    parser.add_argument("--sd-prompt-send-enabled", action="store_true", help="Send extracted Stable Diffusion prompt to a remote receiver.")
    parser.add_argument("--sd-skip-send", action="store_true", help="Skip the in-process SD txt2img call. Used when pipeline_worker handles Generate forever loop.")
    parser.add_argument("--sd-prompt-target-host", default="192.168.11.10", help="Stable Diffusion WebUI API host.")
    parser.add_argument("--sd-prompt-target-port", type=int, default=7860, help="Stable Diffusion WebUI API port.")
    parser.add_argument("--sd-prompt-endpoint", default="/sdapi/v1/txt2img", help="Stable Diffusion WebUI txt2img endpoint path.")
    parser.add_argument("--sd-prompt-token", default="", help="SD prompt receiver token sent via X-Auth-Token header.")
    parser.add_argument("--sd-prompt-timeout", type=float, default=5.0, help="SD prompt receiver timeout seconds.")
    parser.add_argument("--sd-prompt-model-checkpoint", default="", help="A1111 sd_model_checkpoint to set before txt2img. Empty keeps current model.")
    parser.add_argument("--sd-prompt-vae", default="", help="A1111 sd_vae to set before txt2img. Empty keeps current VAE.")
    parser.add_argument("--sd-prompt-clip-skip", type=int, default=0, help="A1111 CLIP_stop_at_last_layers. 0 keeps current setting.")
    parser.add_argument("--sd-prompt-append-prompt", default="", help="Prompt text appended to extracted SD prompt before txt2img.")
    parser.add_argument("--sd-prompt-negative-prompt", default="", help="A1111 negative_prompt.")
    parser.add_argument("--sd-prompt-steps", type=int, default=20, help="A1111 txt2img steps.")
    parser.add_argument("--sd-prompt-width", type=int, default=512, help="A1111 txt2img width.")
    parser.add_argument("--sd-prompt-height", type=int, default=768, help="A1111 txt2img height.")
    parser.add_argument("--sd-prompt-cfg-scale", type=float, default=7.0, help="A1111 cfg_scale.")
    parser.add_argument("--sd-prompt-sampler-name", default="", help="A1111 sampler_name. Empty keeps default.")
    parser.add_argument("--sd-prompt-scheduler", default="", help="A1111 scheduler. Empty keeps default.")
    parser.add_argument("--sd-prompt-seed", type=int, default=-1, help="A1111 seed.")
    parser.add_argument("--sd-prompt-subseed", type=int, default=-1, help="A1111 subseed.")
    parser.add_argument("--sd-prompt-subseed-strength", type=float, default=0.0, help="A1111 subseed_strength.")
    parser.add_argument("--sd-prompt-batch-size", type=int, default=1, help="A1111 batch_size.")
    parser.add_argument("--sd-prompt-n-iter", type=int, default=1, help="A1111 n_iter.")
    parser.add_argument("--sd-prompt-restore-faces", action="store_true", help="A1111 restore_faces.")
    parser.add_argument("--sd-prompt-tiling", action="store_true", help="A1111 tiling.")
    parser.add_argument("--sd-prompt-save-images", action="store_true", help="A1111 save_images.")
    parser.add_argument("--sd-prompt-send-images", action="store_true", help="A1111 send_images.")
    parser.add_argument("--sd-prompt-enable-hr", action="store_true", help="A1111 Hires.fix enable_hr.")
    parser.add_argument("--sd-prompt-hr-scale", type=float, default=2.0, help="A1111 hr_scale.")
    parser.add_argument("--sd-prompt-hr-upscaler", default="Latent", help="A1111 hr_upscaler.")
    parser.add_argument("--sd-prompt-hr-second-pass-steps", type=int, default=0, help="A1111 hr_second_pass_steps.")
    parser.add_argument("--sd-prompt-denoising-strength", type=float, default=0.45, help="A1111 denoising_strength.")
    parser.add_argument("--sd-prompt-hr-resize-x", type=int, default=0, help="A1111 hr_resize_x.")
    parser.add_argument("--sd-prompt-hr-resize-y", type=int, default=0, help="A1111 hr_resize_y.")
    parser.add_argument("--sd-prompt-hr-sampler-name", default="", help="A1111 hr_sampler_name.")
    parser.add_argument("--sd-prompt-hr-scheduler", default="", help="A1111 hr_scheduler.")
    parser.add_argument("--sd-prompt-hr-checkpoint-name", default="", help="A1111 hr_checkpoint_name.")
    parser.add_argument("--sd-prompt-hr-prompt", default="", help="A1111 hr_prompt.")
    parser.add_argument("--sd-prompt-hr-negative-prompt", default="", help="A1111 hr_negative_prompt.")
    parser.add_argument("--sd-prompt-extra-payload-json", default="", help="Extra A1111 txt2img payload JSON object merged last.")
    parser.add_argument("--main", type=int, default=0, help="Main index for event payload.")
    parser.add_argument("--face", type=int, default=-1, help="Face id for event payload. -1 to keep default behavior.")
    parser.add_argument("--keep-current-face", action="store_true", help="Send keepCurrentFace flag with event.")
    parser.add_argument(
        "--face-send-mode",
        default="game_preset",
        choices=["game_preset", "preset_name", "preset_id"],
        help="Face send mode. game_preset=keep/face, preset_name=facePresetName/preset random, preset_id=legacy alias.",
    )
    parser.add_argument("--face-preset-id", default="", help="FacePresetTool preset id (legacy/fallback).")
    parser.add_argument("--face-preset-name", default="", help="FacePresetTool preset name to send when face-send-mode=preset_name.")
    parser.add_argument("--face-preset-random", action="store_true", help="Attach random flag when face-send-mode=preset_name.")
    parser.add_argument(
        "--event-send-mode",
        default="sequence",
        choices=["sequence", "stream", "merged"],
        help="sequence sends all line wavs in one command; stream sends each line command as soon as its wav is ready; merged keeps legacy merged.wav behavior.",
    )
    parser.add_argument("--no-send-event", action="store_true", help="Do not send KKS event after wav merge.")
    parser.add_argument(
        "--conversion-json",
        default="",
        help="変換辞書JSON ([{\"from\":\"...\",\"to_sbv2\":\"...\",\"to_display\":\"...\",\"display_apply\":true}])。to_sbv2は単一文字列/改行区切り/|区切り/JSON配列を受け付け、送信時にランダム1件を使用。",
    )
    parser.add_argument("--subtitle-translate-enabled", action="store_true", help="Translate display subtitles from SBV2 send text before event send.")
    parser.add_argument("--subtitle-translate-source", default="auto", help="Subtitle translation source language.")
    parser.add_argument("--subtitle-translate-target", default="", help="Subtitle translation target language.")
    return parser


def main() -> int:
    force_stdio_utf8()
    parser = _build_arg_parser()
    args = parser.parse_args()

    base_dir = Path(runtime_base_dir())
    config_path = resolve_config_path(str(base_dir), args.config)
    config = load_or_create_config(config_path)
    if args.port is not None:
        config.debug_port = int(args.port)
    if args.timeout is not None:
        config.response_timeout_seconds = float(args.timeout)
    if args.poll is not None:
        config.response_poll_seconds = float(args.poll)
    if args.settle_rounds is not None:
        config.response_settle_rounds = max(1, int(args.settle_rounds))


    logger = setup_logger(config, str(base_dir))
    logger.info("tts_event_start config_path=%s port=%d", config_path, config.debug_port)

    try:
        sbv2_root = Path(args.sbv2_root).resolve()
        if not sbv2_root.exists():
            raise FileNotFoundError(f"SBV2 root not found: {sbv2_root}")

        model_assets_root = sbv2_root / "model_assets"
        if not model_assets_root.exists():
            raise FileNotFoundError(f"model_assets not found: {model_assets_root}")

        if args.list_models:
            _print_json(
                {
                    "ok": True,
                    "error": "",
                    "models": _list_available_models(model_assets_root),
                }
            )
            return 0

        if not args.list_models and not args.text.strip() and not args.response_text.strip():
            raise RuntimeError("--text or --response-text is required unless --list-models is used.")

        args.pipe_name = _normalize_pipe_name(args.pipe_name)

        if args.response_text.strip():
            source = "response-text"
            response_raw = args.response_text.strip()
            logger.info("llm_skipped response_len=%d", len(response_raw))
        else:
            llm_cfg = LlmRequestConfig(
                backend=args.llm_backend,
                base_url=args.llm_base_url,
                model=args.llm_model,
                api_key=args.llm_api_key,
                system_prompt=args.llm_system_prompt,
                temperature=float(args.llm_temperature),
                max_tokens=int(args.llm_max_tokens),
                timeout_seconds=float(args.llm_timeout),
            )
            source, response_raw = generate_llm_response(
                args.text,
                llm_config=llm_cfg,
                bridge_config=config,
                logger=logger,
            )

        response_without_sd, sd_prompt = extract_sd_prompt_block(
            response_raw,
            begin_tag=getattr(args, "sd_prompt_begin_tag", "[SD_PROMPT_BEGIN]"),
            end_tag=getattr(args, "sd_prompt_end_tag", "[SD_PROMPT_END]"),
        )
        sd_prompt_send_result: dict[str, Any] = {}
        if sd_prompt:
            logger.info("sd_prompt_detected len=%d", len(sd_prompt))
            if args.sd_prompt_send_enabled and not args.sd_skip_send:
                result = send_a1111_txt2img(
                    prompt=sd_prompt,
                    host=args.sd_prompt_target_host,
                    port=int(args.sd_prompt_target_port),
                    endpoint=args.sd_prompt_endpoint,
                    token=args.sd_prompt_token,
                    timeout_sec=float(args.sd_prompt_timeout),
                    model_checkpoint=args.sd_prompt_model_checkpoint,
                    vae=args.sd_prompt_vae,
                    clip_skip=int(args.sd_prompt_clip_skip),
                    append_prompt=args.sd_prompt_append_prompt,
                    negative_prompt=args.sd_prompt_negative_prompt,
                    steps=int(args.sd_prompt_steps),
                    width=int(args.sd_prompt_width),
                    height=int(args.sd_prompt_height),
                    cfg_scale=float(args.sd_prompt_cfg_scale),
                    sampler_name=args.sd_prompt_sampler_name,
                    scheduler=args.sd_prompt_scheduler,
                    seed=int(args.sd_prompt_seed),
                    subseed=int(args.sd_prompt_subseed),
                    subseed_strength=float(args.sd_prompt_subseed_strength),
                    batch_size=int(args.sd_prompt_batch_size),
                    n_iter=int(args.sd_prompt_n_iter),
                    restore_faces=bool(args.sd_prompt_restore_faces),
                    tiling=bool(args.sd_prompt_tiling),
                    save_images=bool(args.sd_prompt_save_images),
                    send_images=bool(args.sd_prompt_send_images),
                    enable_hr=bool(args.sd_prompt_enable_hr),
                    hr_scale=float(args.sd_prompt_hr_scale),
                    hr_upscaler=args.sd_prompt_hr_upscaler,
                    hr_second_pass_steps=int(args.sd_prompt_hr_second_pass_steps),
                    denoising_strength=float(args.sd_prompt_denoising_strength),
                    hr_resize_x=int(args.sd_prompt_hr_resize_x),
                    hr_resize_y=int(args.sd_prompt_hr_resize_y),
                    hr_sampler_name=args.sd_prompt_hr_sampler_name,
                    hr_scheduler=args.sd_prompt_hr_scheduler,
                    hr_checkpoint_name=args.sd_prompt_hr_checkpoint_name,
                    hr_prompt=args.sd_prompt_hr_prompt,
                    hr_negative_prompt=args.sd_prompt_hr_negative_prompt,
                    extra_payload_json=args.sd_prompt_extra_payload_json,
                )
                sd_prompt_send_result = result.to_dict()
                logger.info(
                    "sd_prompt_send ok=%d status=%d url=%s error=%s",
                    int(result.ok),
                    result.status,
                    result.url,
                    result.error,
                )
        response_raw_for_tts = response_without_sd
        response, response_raw_len, response_capped_len, response_truncated = _limit_response_text(
            response_raw_for_tts,
            max_chars=args.max_response_chars,
            logger=logger,
            source=source,
        )

        # 変換辞書の適用
        response_original = response  # 変換前のGrokレスポンスを保持
        conversion_dict: list[dict[str, Any]] = []
        if args.conversion_json.strip():
            try:
                conversion_dict = json.loads(args.conversion_json)
            except Exception:
                logger.warning("conversion_json parse failed, skipping")
        random_pick_cache: dict[str, str] = {}
        response = _apply_conversion_rules(
            response_original,
            conversion_dict,
            display_only=False,
            random_pick_cache=random_pick_cache,
            logger=logger,
        )
        response_display = _apply_conversion_rules(
            response_original,
            conversion_dict,
            display_only=True,
            random_pick_cache=random_pick_cache,
            logger=logger,
        )

        max_line_chars = max(0, int(args.max_line_chars or 0))
        lines = _split_response_lines(response, max_line_chars=max_line_chars)
        response_display_translated = False
        if args.subtitle_translate_enabled and str(args.subtitle_translate_target or "").strip():
            display_lines = [
                _translate_text(line, args.subtitle_translate_source, args.subtitle_translate_target, logger)
                for line in lines
            ]
            response_display = "\n".join(display_lines)
            response_display_translated = True
        else:
            display_lines = _split_response_lines(response_display, max_line_chars=max_line_chars)
            if len(display_lines) != len(lines):
                logger.warning(
                    "display_line_count_mismatch send_lines=%d display_lines=%d fallback=send_lines",
                    len(lines),
                    len(display_lines),
                )
                display_lines = list(lines)
        logger.info(
            "grok_response_processed raw_len=%d capped_len=%d send_len=%d display_len=%d line_count=%d max_line_chars=%d",
            response_raw_len,
            response_capped_len,
            len(response),
            len(response_display),
            len(lines),
            max_line_chars,
        )
        if not lines and sd_prompt:
            _print_json(
                {
                    "ok": True,
                    "error": "",
                    "response": "",
                    "response_original": "",
                    "response_display": "",
                    "response_display_translated": False,
                    "response_raw_length": response_raw_len,
                    "response_capped_length": response_capped_len,
                    "response_truncated": response_truncated,
                    "max_response_chars": int(args.max_response_chars),
                    "line_count": 0,
                    "line_texts": [],
                    "display_line_texts": [],
                    "line_wavs": [],
                    "line_durations": [],
                    "total_wav_duration": 0.0,
                    "merged_wav": "",
                    "response_file": "",
                    "event_sent": False,
                    "event_send_mode": args.event_send_mode,
                    "sequence_sent": False,
                    "sequence_session_id": "",
                    "sequence_event_file": "",
                    "event_stdout": "",
                    "event_stderr": "",
                    "event_face_send_mode": _normalize_face_send_mode(args.face_send_mode),
                    "event_face_preset_name": str(args.face_preset_name or "").strip(),
                    "event_face_preset_id": str(args.face_preset_id or "").strip(),
                    "event_face_preset_random": bool(args.face_preset_random),
                    "event_face_selected_name": "",
                    "event_face_selected_id": "",
                    "event_face": int(args.face),
                    "event_keep_current_face": bool(args.keep_current_face),
                    "model_name": args.model_name,
                    "model_file": args.model_file,
                    "llm_backend": _safe_normalize_llm_backend(args.llm_backend),
                    "sd_prompt_detected": True,
                    "sd_prompt": sd_prompt,
                    "sd_prompt_length": len(sd_prompt),
                    "sd_prompt_send_enabled": bool(args.sd_prompt_send_enabled),
                    "sd_prompt_send_result": sd_prompt_send_result,
                }
            )
            return 0
        if not lines:
            raise RuntimeError("Grok response is empty.")

        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = (base_dir / output_dir).resolve()
        run_dir = output_dir / datetime.now().strftime("grok_tts_%Y%m%d_%H%M%S")
        parts_dir = run_dir / "parts"
        run_dir.mkdir(parents=True, exist_ok=True)
        parts_dir.mkdir(parents=True, exist_ok=True)

        response_path = run_dir / "response.txt"
        response_path.write_text(response, encoding="utf-8")

        if not args.model_name.strip():
            raise RuntimeError("--model-name is required unless --list-models is used.")

        sequence_session_id = run_dir.name
        sequence_event_file = ""
        sequence_sent = False
        event_stdout = ""
        event_stderr = ""
        event_sent = False
        event_face_send_mode = _normalize_face_send_mode(args.face_send_mode)
        event_face_preset_id = str(args.face_preset_id or "").strip()
        event_face_preset_name = str(args.face_preset_name or "").strip()
        event_face_preset_random = bool(args.face_preset_random)
        event_face = int(args.face)
        event_keep_current_face = bool(args.keep_current_face)
        event_face_selected_name = event_face_preset_name
        event_face_selected_id = event_face_preset_id
        event_command_base: list[str] | None = None
        stream_event_files: list[str] = []
        stream_event_sent = False
        if not args.no_send_event:
            sender_path = Path(args.event_sender).resolve()
            if not sender_path.exists():
                raise FileNotFoundError(f"Event sender script not found: {sender_path}")

            event_command_base = [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(sender_path),
                "-PipeName",
                args.pipe_name,
            ]
            logger.info(
                "event_send_start mode=%s audio_mode=%s preset_name=%s preset_id=%s random=%d face=%d keep_current_face=%d remote_http=%d host=%s port=%d endpoint=%s",
                event_face_send_mode,
                args.event_send_mode,
                event_face_preset_name or "(empty)",
                event_face_preset_id or "(empty)",
                int(event_face_preset_random),
                event_face,
                int(event_keep_current_face),
                int(bool(args.remote_http)),
                str(args.target_host or "").strip() or "(pipe-local)",
                int(args.target_port),
                args.target_endpoint,
            )
            if args.remote_http or args.target_host.strip():
                event_command_base.append("-RemoteHttp")
            if args.target_host.strip():
                event_command_base.extend(["-TargetHost", args.target_host.strip()])
                event_command_base.extend(["-TargetPort", str(args.target_port)])
                event_command_base.extend(["-TargetEndpoint", args.target_endpoint])
                if args.target_token.strip():
                    event_command_base.extend(["-TargetToken", args.target_token.strip()])

        sbv2_server_url = (args.sbv2_server_url or "").strip()

        if sbv2_server_url:
            # ── HTTPサーバー経由（モデルロード済み・高速） ──────────────────
            requested_model_file = (args.model_file or "").strip()
            logger.info(
                "tts_via_server url=%s model=%s model_file=%s",
                sbv2_server_url,
                args.model_name,
                requested_model_file or "(server-auto)",
            )
            expected_part_names = [f"line_{i+1:03d}.wav" for i in range(len(lines))]
            for i, line_text in enumerate(lines):
                out_path = parts_dir / expected_part_names[i]
                _tts_via_http_server(
                    server_url=sbv2_server_url,
                    text=line_text,
                    model_name=args.model_name,
                    model_file=requested_model_file,
                    speaker=args.speaker,
                    style=args.style,
                    style_weight=args.style_weight,
                    sdp_ratio=args.sdp_ratio,
                    noise=args.noise,
                    noise_w=args.noise_w,
                    length=args.length,
                    output_path=out_path,
                )
                logger.info("tts_line_done line=%d/%d file=%s", i + 1, len(lines), out_path.name)
                if args.event_send_mode == "stream" and not args.no_send_event and event_command_base is not None:
                    duration = _wav_duration_sec(out_path)
                    subtitle = display_lines[i] if i < len(display_lines) else line_text
                    event_path, line_stdout, line_stderr = _send_sequence_line_event(
                        event_command_base=event_command_base,
                        run_dir=run_dir,
                        session_id=sequence_session_id,
                        line_index=i + 1,
                        wav_path=out_path,
                        display_text=subtitle,
                        duration=duration,
                        args=args,
                        event_face_send_mode=event_face_send_mode,
                        event_face_preset_name=event_face_preset_name,
                        event_face_preset_id=event_face_preset_id,
                        event_face_preset_random=event_face_preset_random,
                        event_face=event_face,
                        event_keep_current_face=event_keep_current_face,
                        include_face=(i == 0),
                    )
                    stream_event_files.append(event_path)
                    event_stdout = (event_stdout + "\n" + line_stdout).strip() if event_stdout else line_stdout
                    event_stderr = (event_stderr + "\n" + line_stderr).strip() if event_stderr else line_stderr
                    event_sent = True
                    sequence_sent = True
                    stream_event_sent = True
                    logger.info(
                        "event_stream_line_sent line=%d/%d file=%s stdout_len=%d stderr_len=%d",
                        i + 1,
                        len(lines),
                        Path(event_path).name,
                        len(line_stdout or ""),
                        len(line_stderr or ""),
                    )
            model_file_name = requested_model_file or "(server-auto)"
        else:
            # ── サブプロセス経由（従来方式） ──────────────────────────────────
            sbv2_python = Path(args.sbv2_python).resolve() if args.sbv2_python else (sbv2_root / "venv" / "Scripts" / "python.exe")
            if not sbv2_python.exists():
                raise FileNotFoundError(f"SBV2 python not found: {sbv2_python}")

            model_dir = model_assets_root / args.model_name
            if not model_dir.exists():
                raise FileNotFoundError(f"Model directory not found: {model_dir}")

            model_file = _pick_model_file(model_dir, args.model_file.strip() or None)
            model_file_name = model_file.name
            logger.info("tts_model_selected model=%s file=%s", args.model_name, model_file_name)

            request_json_path = run_dir / "tts_request.json"
            expected_part_names = _write_tts_request_json(
                request_json_path,
                lines,
                speaker=args.speaker,
                style=args.style,
                style_weight=args.style_weight,
                sdp_ratio=args.sdp_ratio,
                noise=args.noise,
                noise_w=args.noise_w,
                length=args.length,
            )

            batch_script = sbv2_root / "tools" / "batch_tts_json.py"
            if not batch_script.exists():
                raise FileNotFoundError(f"batch_tts_json.py not found: {batch_script}")

            tts_command = [
                str(sbv2_python), str(batch_script),
                "--json", str(request_json_path),
                "--model_name", args.model_name,
                "--model_file", model_file_name,
                "--assets_root", str(sbv2_root / "model_assets"),
                "--output_dir", str(parts_dir),
                "--device", args.device,
            ]
            tts_result = _run_subprocess(tts_command, cwd=sbv2_root)
            logger.info("tts_done stdout_len=%d stderr_len=%d", len(tts_result.stdout), len(tts_result.stderr))

        part_paths = [parts_dir / name for name in expected_part_names]
        missing_parts = [str(p) for p in part_paths if not p.exists()]
        if missing_parts:
            raise RuntimeError(f"TTS output missing files: {missing_parts}")

        line_durations = [_wav_duration_sec(path) for path in part_paths]
        total_wav_duration = sum(line_durations)
        merged_wav_path: Path | None = None
        if args.no_send_event or args.event_send_mode == "merged":
            merged_wav_path = run_dir / "merged.wav"
            _concat_wavs(part_paths, merged_wav_path, args.line_gap_ms)

        if not args.no_send_event and event_command_base is not None and not stream_event_sent:
            if args.event_send_mode == "sequence":
                items: list[dict[str, Any]] = []
                for idx, wav_path in enumerate(part_paths):
                    duration = line_durations[idx] if idx < len(line_durations) else 0.0
                    subtitle = display_lines[idx] if idx < len(display_lines) else lines[idx]
                    items.append(
                        {
                            "index": idx + 1,
                            "audioPath": str(wav_path),
                            "subtitle": subtitle,
                            "durationSeconds": _round3(duration),
                            "holdSeconds": _round3(max(0.1, duration + 0.2)),
                        }
                    )
                sequence_payload: dict[str, Any] = {
                    "type": "speak_sequence",
                    "sessionId": sequence_session_id,
                    "main": int(args.main),
                    "interrupt": 1,
                    "deleteAfterPlay": 0,
                    "responseText": response_display,
                    "lineTexts": [str(v) for v in display_lines],
                    "lineDurations": [_round3(v) for v in line_durations],
                    "items": items,
                }
                if args.voice_volume >= 0:
                    sequence_payload["volume"] = float(args.voice_volume)
                if args.voice_pitch >= 0:
                    sequence_payload["pitch"] = float(args.voice_pitch)
                if event_face_send_mode == "preset_name":
                    if event_face_preset_name:
                        sequence_payload["facePresetName"] = event_face_preset_name
                        event_face_selected_name = event_face_preset_name
                    if event_face_preset_id:
                        sequence_payload["facePresetId"] = event_face_preset_id
                        event_face_selected_id = event_face_preset_id
                    if event_face_preset_random:
                        sequence_payload["facePresetRandom"] = 1
                    if (not event_face_preset_random) and (not event_face_preset_name) and (not event_face_preset_id):
                        raise RuntimeError("face_send_mode=preset_name but face_preset_name is empty")
                else:
                    if event_face >= 0:
                        sequence_payload["face"] = event_face
                    if event_keep_current_face:
                        sequence_payload["keepCurrentFace"] = 1

                sequence_event_path = run_dir / "voice_sequence_event.json"
                sequence_event_path.write_text(
                    json.dumps(sequence_payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                sequence_event_file = str(sequence_event_path)
                event_command = event_command_base + ["-JsonFile", str(sequence_event_path)]
            elif args.event_send_mode == "stream":
                event_command = []
                for idx, wav_path in enumerate(part_paths):
                    duration = line_durations[idx] if idx < len(line_durations) else 0.0
                    subtitle = display_lines[idx] if idx < len(display_lines) else lines[idx]
                    event_path, line_stdout, line_stderr = _send_sequence_line_event(
                        event_command_base=event_command_base,
                        run_dir=run_dir,
                        session_id=sequence_session_id,
                        line_index=idx + 1,
                        wav_path=wav_path,
                        display_text=subtitle,
                        duration=duration,
                        args=args,
                        event_face_send_mode=event_face_send_mode,
                        event_face_preset_name=event_face_preset_name,
                        event_face_preset_id=event_face_preset_id,
                        event_face_preset_random=event_face_preset_random,
                        event_face=event_face,
                        event_keep_current_face=event_keep_current_face,
                        include_face=(idx == 0),
                    )
                    stream_event_files.append(event_path)
                    event_stdout = (event_stdout + "\n" + line_stdout).strip() if event_stdout else line_stdout
                    event_stderr = (event_stderr + "\n" + line_stderr).strip() if event_stderr else line_stderr
                    logger.info(
                        "event_stream_line_sent line=%d/%d file=%s stdout_len=%d stderr_len=%d",
                        idx + 1,
                        len(part_paths),
                        Path(event_path).name,
                        len(line_stdout or ""),
                        len(line_stderr or ""),
                    )
                sequence_event_file = stream_event_files[0] if stream_event_files else ""
                event_sent = True
                sequence_sent = True
                stream_event_sent = True
            else:
                if merged_wav_path is None:
                    merged_wav_path = run_dir / "merged.wav"
                    _concat_wavs(part_paths, merged_wav_path, args.line_gap_ms)
                event_command = event_command_base + [
                    "-Main",
                    str(args.main),
                    "-AudioPath",
                    str(merged_wav_path),
                ]
                if event_face_send_mode == "preset_name":
                    if event_face_preset_random:
                        event_command.append("-FacePresetRandom")
                    if event_face_preset_name:
                        event_command.extend(["-FacePresetName", event_face_preset_name])
                        event_face_selected_name = event_face_preset_name
                    if event_face_preset_id:
                        event_command.extend(["-FacePresetId", event_face_preset_id])
                        event_face_selected_id = event_face_preset_id
                    if (not event_face_preset_random) and (not event_face_preset_name) and (not event_face_preset_id):
                        raise RuntimeError("face_send_mode=preset_name but face_preset_name is empty")
                else:
                    if event_face >= 0:
                        event_command.extend(["-Face", str(event_face)])
                    if event_keep_current_face:
                        event_command.append("-KeepCurrentFace")
                if args.voice_volume >= 0:
                    event_command.extend(["-Volume", str(args.voice_volume)])
                if args.voice_pitch >= 0:
                    event_command.extend(["-Pitch", str(args.voice_pitch)])

            if args.event_send_mode == "stream":
                logger.info(
                    "event_send_result status=ok mode=%s audio_mode=%s stdout_len=%d stderr_len=%d",
                    event_face_send_mode,
                    args.event_send_mode,
                    len(event_stdout or ""),
                    len(event_stderr or ""),
                )
            else:
                try:
                    event_result = _run_subprocess(event_command)
                    event_stdout = event_result.stdout
                    event_stderr = event_result.stderr
                    event_sent = True
                    sequence_sent = args.event_send_mode == "sequence"
                    logger.info(
                        "event_send_result status=ok mode=%s audio_mode=%s stdout_len=%d stderr_len=%d",
                        event_face_send_mode,
                        args.event_send_mode,
                        len(event_stdout or ""),
                        len(event_stderr or ""),
                    )
                except subprocess.CalledProcessError as exc:
                    event_stdout = str(exc.stdout or "")
                    event_stderr = str(exc.stderr or "")
                    logger.error(
                        "event_send_result status=ng mode=%s returncode=%s stdout=%r stderr=%r",
                        event_face_send_mode,
                        str(exc.returncode),
                        event_stdout[:240],
                        event_stderr[:240],
                    )
                    raise RuntimeError(f"event sender failed returncode={exc.returncode}") from exc

        if args.event_send_mode == "stream" and stream_event_files and not sequence_event_file:
            sequence_event_file = stream_event_files[0]

        _print_json(
            {
                "ok": True,
                "error": "",
                "response": response,
                "response_original": response_original,
                "response_display": response_display,
                "response_display_translated": response_display_translated,
                "response_raw_length": response_raw_len,
                "response_capped_length": response_capped_len,
                "response_truncated": response_truncated,
                "max_response_chars": int(args.max_response_chars),
                "line_count": len(lines),
                "line_texts": lines,
                "display_line_texts": display_lines,
                "line_wavs": [str(p) for p in part_paths],
                "line_durations": [_round3(v) for v in line_durations],
                "total_wav_duration": _round3(total_wav_duration),
                "merged_wav": str(merged_wav_path) if merged_wav_path is not None else "",
                "response_file": str(response_path),
                "event_sent": event_sent,
                "event_send_mode": args.event_send_mode,
                "sequence_sent": sequence_sent,
                "sequence_session_id": sequence_session_id,
                "sequence_event_file": sequence_event_file,
                "event_stdout": event_stdout,
                "event_stderr": event_stderr,
                "event_face_send_mode": event_face_send_mode,
                "event_face_preset_name": event_face_preset_name,
                "event_face_preset_id": event_face_preset_id,
                "event_face_preset_random": event_face_preset_random,
                "event_face_selected_name": event_face_selected_name,
                "event_face_selected_id": event_face_selected_id,
                "event_face": event_face,
                "event_keep_current_face": event_keep_current_face,
                "model_name": args.model_name,
                "model_file": model_file_name,
                "llm_backend": _safe_normalize_llm_backend(args.llm_backend),
                "sd_prompt_detected": bool(sd_prompt),
                "sd_prompt": sd_prompt,
                "sd_prompt_length": len(sd_prompt),
                "sd_prompt_send_enabled": bool(args.sd_prompt_send_enabled),
                "sd_prompt_send_result": sd_prompt_send_result,
            }
        )
        return 0
    except Exception as exc:
        logger.error("tts_event_failed error=%s", exc)
        logger.debug("traceback=%s", traceback.format_exc())
        event_stdout_value = str(locals().get("event_stdout", "") or "")
        event_stderr_value = str(locals().get("event_stderr", "") or "")
        _print_json(
            {
                "ok": False,
                "error": str(exc),
                "response": "",
                "response_display": "",
                "response_display_translated": False,
                "response_raw_length": 0,
                "response_capped_length": 0,
                "response_truncated": False,
                "max_response_chars": int(args.max_response_chars),
                "line_count": 0,
                "line_texts": [],
                "line_wavs": [],
                "merged_wav": "",
                "response_file": "",
                "event_sent": False,
                "event_send_mode": str(getattr(args, "event_send_mode", "") or ""),
                "sequence_sent": bool(locals().get("sequence_sent", False)),
                "sequence_session_id": str(locals().get("sequence_session_id", "") or ""),
                "sequence_event_file": str(locals().get("sequence_event_file", "") or ""),
                "event_stdout": event_stdout_value,
                "event_stderr": event_stderr_value,
                "event_face_send_mode": _normalize_face_send_mode(args.face_send_mode),
                "event_face_preset_name": str(args.face_preset_name or "").strip(),
                "event_face_preset_id": str(args.face_preset_id or "").strip(),
                "event_face_preset_random": bool(args.face_preset_random),
                "event_face_selected_name": "",
                "event_face_selected_id": "",
                "event_face": int(args.face),
                "event_keep_current_face": bool(args.keep_current_face),
                "model_name": args.model_name,
                "model_file": args.model_file,
                "llm_backend": _safe_normalize_llm_backend(getattr(args, "llm_backend", "grok_browser")),
                "sd_prompt_detected": bool(locals().get("sd_prompt", "")),
                "sd_prompt": str(locals().get("sd_prompt", "") or ""),
                "sd_prompt_length": len(str(locals().get("sd_prompt", "") or "")),
                "sd_prompt_send_enabled": bool(getattr(args, "sd_prompt_send_enabled", False)),
                "sd_prompt_send_result": dict(locals().get("sd_prompt_send_result", {}) or {}),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
