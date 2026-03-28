from __future__ import annotations

import argparse
import json
import traceback

from .browser import connect_existing_debug_chrome
from .config import load_or_create_config, resolve_config_path, runtime_base_dir
from .grok_client import send_text, wait_for_response
from .io_utf8 import force_stdio_utf8
from .logging_utils import setup_logger


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send text to current Grok tab and return the response.")
    parser.add_argument("--text", required=True, help="Text to send.")
    parser.add_argument("--port", type=int, default=None, help="Chrome debug port (default from config).")
    parser.add_argument("--config", default=None, help="Config file path (relative to runtime dir or absolute).")
    parser.add_argument("--timeout", type=float, default=None, help="Response timeout seconds.")
    parser.add_argument("--poll", type=float, default=None, help="Response poll interval seconds.")
    parser.add_argument("--settle-rounds", type=int, default=None, help="Stable rounds before finish.")
    return parser


def main() -> int:
    force_stdio_utf8()
    parser = _build_arg_parser()
    args = parser.parse_args()

    base_dir = runtime_base_dir()
    config_path = resolve_config_path(base_dir, args.config)
    config = load_or_create_config(config_path)

    if args.port is not None:
        config.debug_port = int(args.port)
    if args.timeout is not None:
        config.response_timeout_seconds = float(args.timeout)
    if args.poll is not None:
        config.response_poll_seconds = float(args.poll)
    if args.settle_rounds is not None:
        config.response_settle_rounds = max(1, int(args.settle_rounds))

    logger = setup_logger(config, base_dir)
    logger.info("start config_path=%s port=%d", config_path, config.debug_port)

    try:
        driver = connect_existing_debug_chrome(config.debug_port)
        baseline, stop_before = send_text(driver, config, args.text, logger)
        response = wait_for_response(driver, config, logger, baseline, stop_before)
        _print_json(
            {
                "ok": True,
                "response": response,
                "error": "",
            }
        )
        logger.info("success response_len=%d", len(response))
        return 0
    except Exception as exc:
        logger.error("failed error=%s", exc)
        logger.debug("traceback=%s", traceback.format_exc())
        _print_json(
            {
                "ok": False,
                "response": "",
                "error": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
