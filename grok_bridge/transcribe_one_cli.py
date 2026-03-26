from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path
from typing import Any

from .io_utf8 import force_stdio_utf8


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe one WAV file with faster-whisper and return JSON.")
    parser.add_argument("--audio", required=True, help="Input wav path.")
    parser.add_argument("--model", default="small", help="Model name or local model path.")
    parser.add_argument("--device", default="auto", help="Inference device: auto/cuda/cpu.")
    parser.add_argument("--compute-type", default="int8_float16", help="Compute type.")
    parser.add_argument("--language", default="ja", help="Language code. Empty string for auto.")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size.")
    parser.add_argument("--download-root", default="", help="Model cache/download root directory.")
    parser.add_argument("--condition-on-previous-text", action="store_true", help="Enable context carry-over.")
    return parser


def main() -> int:
    force_stdio_utf8()
    args = _build_arg_parser().parse_args()
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": f"Failed to import faster_whisper: {exc}",
                "text": "",
                "duration": 0.0,
            }
        )
        return 1

    try:
        audio_path = Path(args.audio).expanduser().resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        kwargs = {
            "model_size_or_path": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
        }
        download_root = args.download_root.strip()
        if download_root:
            kwargs["download_root"] = str(Path(download_root).expanduser().resolve())

        model = WhisperModel(**kwargs)

        transcribe_kwargs: dict[str, Any] = {
            "audio": str(audio_path),
            "beam_size": max(1, int(args.beam_size)),
            "condition_on_previous_text": bool(args.condition_on_previous_text),
        }
        language = args.language.strip()
        if language:
            transcribe_kwargs["language"] = language

        segments, info = model.transcribe(**transcribe_kwargs)
        text = "".join(segment.text for segment in segments).strip()
        duration = float(getattr(info, "duration", 0.0))
        _print_json(
            {
                "ok": True,
                "error": "",
                "text": text,
                "duration": duration,
                "audio_path": str(audio_path),
            }
        )
        return 0
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": str(exc),
                "text": "",
                "duration": 0.0,
                "traceback": traceback.format_exc(),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
