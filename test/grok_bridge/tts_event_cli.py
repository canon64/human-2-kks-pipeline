from __future__ import annotations

import argparse
import json
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
from .logging_utils import setup_logger


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _split_response_lines(response: str) -> list[str]:
    lines = [(line or "").strip() for line in response.splitlines()]
    lines = [line for line in lines if line]
    if lines:
        return lines
    compact = (response or "").strip()
    return [compact] if compact else []


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _apply_conversion_rules(response: str, rules: list[dict[str, Any]], display_only: bool = False) -> str:
    converted = response
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if display_only and not _parse_bool(entry.get("display_apply", False)):
            continue
        from_str = str(entry.get("from", ""))
        to_str = str(entry.get("to", ""))
        if not from_str:
            continue
        converted = converted.replace(from_str, to_str)
    return converted


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


def _run_subprocess(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        env=with_utf8_env(),
        check=True,
    )


def _tts_via_http_server(
    server_url: str,
    text: str,
    model_name: str,
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
    url = server_url.rstrip("/") + "/voice?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        wav_bytes = resp.read()
    output_path.write_bytes(wav_bytes)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send text to Grok, synthesize JP-Extra speech per response line, merge WAV, and send event."
    )
    parser.add_argument("--text", default="", help="Text to send to Grok.")
    parser.add_argument("--response-text", default="", help="Use this as Grok response directly (skip Grok).")
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
    parser.add_argument("--main", type=int, default=0, help="Main index for event payload.")
    parser.add_argument("--face", type=int, default=-1, help="Face id for event payload. -1 to keep default behavior.")
    parser.add_argument("--keep-current-face", action="store_true", help="Send keepCurrentFace flag with event.")
    parser.add_argument("--no-send-event", action="store_true", help="Do not send KKS event after wav merge.")
    parser.add_argument("--conversion-json", default="", help="変換辞書JSON ([{\"from\":\"...\",\"to\":\"...\"}])。TTS前にレスポンスに適用。")
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

        if args.response_text.strip():
            response = args.response_text.strip()
            logger.info("grok_skipped response_len=%d", len(response))
        else:
            from .browser import connect_existing_debug_chrome
            from .grok_client import send_text, wait_for_response

            driver = connect_existing_debug_chrome(config.debug_port)
            baseline, stop_before = send_text(driver, config, args.text, logger)
            response = wait_for_response(driver, config, logger, baseline, stop_before)

        # 変換辞書の適用
        response_original = response  # 変換前のGrokレスポンスを保存
        conversion_dict: list[dict[str, Any]] = []
        if args.conversion_json.strip():
            try:
                conversion_dict = json.loads(args.conversion_json)
            except Exception:
                logger.warning("conversion_json parse failed, skipping")
        response = _apply_conversion_rules(response_original, conversion_dict, display_only=False)
        response_display = _apply_conversion_rules(response_original, conversion_dict, display_only=True)

        lines = _split_response_lines(response)
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

        sbv2_server_url = (args.sbv2_server_url or "").strip()

        if sbv2_server_url:
            # ── HTTPサーバー経由（モデルロード済み・高速） ──────────────────
            logger.info("tts_via_server url=%s model=%s", sbv2_server_url, args.model_name)
            expected_part_names = [f"line_{i+1:03d}.wav" for i in range(len(lines))]
            for i, line_text in enumerate(lines):
                out_path = parts_dir / expected_part_names[i]
                _tts_via_http_server(
                    server_url=sbv2_server_url,
                    text=line_text,
                    model_name=args.model_name,
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
            model_file_name = args.model_file or "(server)"
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

        merged_wav_path = run_dir / "merged.wav"
        _concat_wavs(part_paths, merged_wav_path, args.line_gap_ms)

        event_stdout = ""
        event_stderr = ""
        event_sent = False
        if not args.no_send_event:
            sender_path = Path(args.event_sender).resolve()
            if not sender_path.exists():
                raise FileNotFoundError(f"Event sender script not found: {sender_path}")

            event_command = [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(sender_path),
                "-PipeName",
                args.pipe_name,
                "-Main",
                str(args.main),
                "-AudioPath",
                str(merged_wav_path),
            ]
            if args.remote_http or args.target_host.strip():
                event_command.append("-RemoteHttp")
            if args.target_host.strip():
                event_command.extend(["-TargetHost", args.target_host.strip()])
                event_command.extend(["-TargetPort", str(args.target_port)])
                event_command.extend(["-TargetEndpoint", args.target_endpoint])
                if args.target_token.strip():
                    event_command.extend(["-TargetToken", args.target_token.strip()])
            if args.face >= 0:
                event_command.extend(["-Face", str(args.face)])
            if args.keep_current_face:
                event_command.append("-KeepCurrentFace")
            if args.voice_volume >= 0:
                event_command.extend(["-Volume", str(args.voice_volume)])
            if args.voice_pitch >= 0:
                event_command.extend(["-Pitch", str(args.voice_pitch)])

            event_result = _run_subprocess(event_command)
            event_stdout = event_result.stdout
            event_stderr = event_result.stderr
            event_sent = True

        _print_json(
            {
                "ok": True,
                "error": "",
                "response": response,
                "response_original": response_original,
                "response_display": response_display,
                "line_count": len(lines),
                "line_texts": lines,
                "line_wavs": [str(p) for p in part_paths],
                "merged_wav": str(merged_wav_path),
                "response_file": str(response_path),
                "event_sent": event_sent,
                "event_stdout": event_stdout,
                "event_stderr": event_stderr,
                "model_name": args.model_name,
                "model_file": model_file_name,
            }
        )
        return 0
    except Exception as exc:
        logger.error("tts_event_failed error=%s", exc)
        logger.debug("traceback=%s", traceback.format_exc())
        _print_json(
            {
                "ok": False,
                "error": str(exc),
                "response": "",
                "response_display": "",
                "line_count": 0,
                "line_texts": [],
                "line_wavs": [],
                "merged_wav": "",
                "response_file": "",
                "event_sent": False,
                "event_stdout": "",
                "event_stderr": "",
                "model_name": args.model_name,
                "model_file": args.model_file,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
