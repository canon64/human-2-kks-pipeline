from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any


CONFIG_FILENAME = "grok_bridge_config.json"


@dataclass
class SelectorConfig:
    input: str = 'div.ProseMirror[contenteditable="true"]'
    send_buttons: list[str] = field(
        default_factory=lambda: [
            'button[aria-label="送信"]',
            'button[aria-label*="送信"]',
            'button[aria-label*="send"]',
            'button[type="submit"]',
        ]
    )
    stop_buttons: list[str] = field(
        default_factory=lambda: [
            'button[aria-label*="モデルの応答を停止"]',
            'button[aria-label*="応答を停止"]',
            'button[aria-label*="停止"]',
            'button[aria-label*="stop model response"]',
            'button[aria-label*="stop response"]',
        ]
    )
    response_blocks: list[str] = field(
        default_factory=lambda: [
            "div.items-start .response-content-markdown.markdown",
            '[class*="message"][class*="assistant"]',
            ".markdown",
        ]
    )


@dataclass
class LogConfig:
    level: str = "INFO"
    directory: str = "_logs"
    file_prefix: str = "grok_bridge"


@dataclass
class BridgeConfig:
    debug_port: int = 9222
    wait_after_send_seconds: float = 1.0
    response_timeout_seconds: float = 60.0
    response_poll_seconds: float = 0.2
    response_settle_rounds: int = 2
    wait_for_icon_change: bool = True
    response_detection_mode: str = "stop_button"  # "stop_button" or "text_stable"
    text_stable_seconds: float = 1.5  # text_stableモード: 変化なし判定秒数
    selectors: SelectorConfig = field(default_factory=SelectorConfig)
    log: LogConfig = field(default_factory=LogConfig)


def runtime_base_dir() -> str:
    env_home = os.environ.get("GROK_BRIDGE_HOME", "").strip()
    if env_home:
        return os.path.abspath(env_home)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def resolve_config_path(base_dir: str, config_path_arg: str | None) -> str:
    if config_path_arg:
        if os.path.isabs(config_path_arg):
            return config_path_arg
        return os.path.abspath(os.path.join(base_dir, config_path_arg))
    return os.path.abspath(os.path.join(base_dir, CONFIG_FILENAME))


def _default_config_dict() -> dict[str, Any]:
    cfg = BridgeConfig()
    return {
        "debug_port": cfg.debug_port,
        "wait_after_send_seconds": cfg.wait_after_send_seconds,
        "response_timeout_seconds": cfg.response_timeout_seconds,
        "response_poll_seconds": cfg.response_poll_seconds,
        "response_settle_rounds": cfg.response_settle_rounds,
        "wait_for_icon_change": cfg.wait_for_icon_change,
        "response_detection_mode": cfg.response_detection_mode,
        "text_stable_seconds": cfg.text_stable_seconds,
        "selectors": {
            "input": cfg.selectors.input,
            "send_buttons": list(cfg.selectors.send_buttons),
            "stop_buttons": list(cfg.selectors.stop_buttons),
            "response_blocks": list(cfg.selectors.response_blocks),
        },
        "log": {
            "level": cfg.log.level,
            "directory": cfg.log.directory,
            "file_prefix": cfg.log.file_prefix,
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def load_or_create_config(config_path: str) -> BridgeConfig:
    defaults = _default_config_dict()

    if not os.path.exists(config_path):
        _ensure_parent_dir(config_path)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=2)
            f.write("\n")
        merged = defaults
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Config root must be an object: {config_path}")
        merged = _deep_merge(defaults, loaded)

    selectors = merged.get("selectors", {})
    log = merged.get("log", {})

    return BridgeConfig(
        debug_port=int(merged["debug_port"]),
        wait_after_send_seconds=float(merged["wait_after_send_seconds"]),
        response_timeout_seconds=float(merged["response_timeout_seconds"]),
        response_poll_seconds=float(merged["response_poll_seconds"]),
        response_settle_rounds=max(1, int(merged["response_settle_rounds"])),
        wait_for_icon_change=bool(merged["wait_for_icon_change"]),
        response_detection_mode=str(merged.get("response_detection_mode", "stop_button")),
        text_stable_seconds=float(merged.get("text_stable_seconds", 1.5)),
        selectors=SelectorConfig(
            input=str(selectors["input"]),
            send_buttons=[str(x) for x in selectors.get("send_buttons", [])],
            stop_buttons=[str(x) for x in selectors.get("stop_buttons", [])],
            response_blocks=[str(x) for x in selectors.get("response_blocks", [])],
        ),
        log=LogConfig(
            level=str(log["level"]),
            directory=str(log["directory"]),
            file_prefix=str(log["file_prefix"]),
        ),
    )
