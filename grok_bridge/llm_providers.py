from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import BridgeConfig


@dataclass(frozen=True)
class LlmRequestConfig:
    backend: str = "grok_browser"
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = ""
    api_key: str = "lm-studio"
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_seconds: float = 120.0


def normalize_backend(value: str) -> str:
    token = str(value or "").strip().lower()
    aliases = {
        "": "grok_browser",
        "grok": "grok_browser",
        "browser": "grok_browser",
        "grok_browser": "grok_browser",
        "local": "local_openai",
        "local_llm": "local_openai",
        "local_openai": "local_openai",
        "openai_compatible": "local_openai",
        "lmstudio": "local_openai",
        "lm_studio": "local_openai",
        "ollama": "local_openai",
        "ollama_openai": "local_openai",
    }
    if token not in aliases:
        raise ValueError(f"unsupported llm backend: {value}")
    return aliases[token]


def generate_llm_response(
    text: str,
    *,
    llm_config: LlmRequestConfig,
    bridge_config: BridgeConfig,
    logger: logging.Logger,
) -> tuple[str, str]:
    backend = normalize_backend(llm_config.backend)
    if backend == "grok_browser":
        return backend, _generate_grok_response(text, bridge_config=bridge_config, logger=logger)
    if backend == "local_openai":
        return backend, _generate_openai_compatible_response(text, llm_config=llm_config, logger=logger)
    raise ValueError(f"unsupported llm backend: {backend}")


def _generate_grok_response(text: str, *, bridge_config: BridgeConfig, logger: logging.Logger) -> str:
    from .browser import connect_existing_debug_chrome
    from .grok_client import send_text, wait_for_response

    driver = connect_existing_debug_chrome(bridge_config.debug_port)
    baseline, stop_before = send_text(driver, bridge_config, text, logger)
    return wait_for_response(driver, bridge_config, logger, baseline, stop_before)


def _generate_openai_compatible_response(
    text: str,
    *,
    llm_config: LlmRequestConfig,
    logger: logging.Logger,
) -> str:
    base_url = str(llm_config.base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("local_openai requires llm_base_url")
    model = str(llm_config.model or "").strip()
    if not model:
        raise RuntimeError("local_openai requires llm_model")

    messages: list[dict[str, str]] = []
    system_prompt = str(llm_config.system_prompt or "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": str(text or "")})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(llm_config.temperature),
        "max_tokens": max(1, int(llm_config.max_tokens)),
        "stream": False,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{base_url}/chat/completions"
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    api_key = str(llm_config.api_key or "").strip()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    timeout = max(1.0, float(llm_config.timeout_seconds))
    logger.info(
        "llm_local_openai_request base_url=%s model=%s text_len=%d timeout=%.1f",
        base_url,
        model,
        len(text or ""),
        timeout,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"local_openai HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"local_openai connection failed: {exc}") from exc

    try:
        data: Any = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"local_openai returned invalid JSON: {body[:500]}") from exc

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"local_openai response has no choices: {body[:500]}")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError(f"local_openai invalid first choice: {body[:500]}")
    message = first.get("message")
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "").strip()
    if not content:
        content = str(first.get("text") or "").strip()
    if not content:
        raise RuntimeError(f"local_openai response content is empty: {body[:500]}")

    logger.info("llm_local_openai_response len=%d", len(content))
    return content
