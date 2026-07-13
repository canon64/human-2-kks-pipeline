from __future__ import annotations

import json
import base64
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SD_PROMPT_INSTRUCTION = """Stable Diffusion用プロンプトが必要な場合だけ、返答の最後に次のブロックを1つだけ追加してください。

[SD_PROMPT_BEGIN]
masterpiece, best quality, ...
[SD_PROMPT_END]

制約:
- SDプロンプト不要時はこのブロックを一切出さない
- 必要時は必ずこの形式そのままで出す
- ブロック内は英語プロンプト本文のみ
- ブロック内に説明文、日本語、補足、番号、引用符を入れない
- ブロックの後ろに追加文を書かない"""

_SD_PROMPT_RE = re.compile(
    r"\[SD_PROMPT_BEGIN\](.*?)\[SD_PROMPT_END\]",
    re.IGNORECASE | re.DOTALL,
)

_DEFAULT_BEGIN_TAG = "[SD_PROMPT_BEGIN]"
_DEFAULT_END_TAG = "[SD_PROMPT_END]"
_COMMON_TAG_PAIRS = (
    (_DEFAULT_BEGIN_TAG, _DEFAULT_END_TAG),
    ("[BEGIN]", "[END]"),
)


def _tag_pairs(begin_tag: str, end_tag: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []

    def add_pair(begin: str, end: str) -> None:
        b = str(begin or "").strip()
        e = str(end or "").strip()
        if not b or not e:
            return
        key = (b.lower(), e.lower())
        if any((existing[0].lower(), existing[1].lower()) == key for existing in pairs):
            return
        pairs.append((b, e))

    add_pair(begin_tag or _DEFAULT_BEGIN_TAG, end_tag or _DEFAULT_END_TAG)
    for common_begin, common_end in _COMMON_TAG_PAIRS:
        add_pair(common_begin, common_end)
    return pairs


def _build_sd_prompt_re(begin_tag: str, end_tag: str) -> re.Pattern:
    if (begin_tag == _DEFAULT_BEGIN_TAG and end_tag == _DEFAULT_END_TAG):
        return _SD_PROMPT_RE
    return re.compile(
        re.escape(begin_tag) + r"(.*?)" + re.escape(end_tag),
        re.IGNORECASE | re.DOTALL,
    )


@dataclass(frozen=True)
class SdPromptSendResult:
    ok: bool
    status: int
    url: str
    body: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "url": self.url,
            "body": self.body,
            "error": self.error,
        }


def _decode_sd_image_text(raw_image: str) -> bytes:
    text = str(raw_image or "").strip()
    if "," in text and text[:32].lower().startswith("data:image"):
        text = text.split(",", 1)[1]
    return base64.b64decode(text)


def extract_sd_result_images(result: dict[str, Any] | SdPromptSendResult) -> list[bytes]:
    if isinstance(result, SdPromptSendResult):
        data: dict[str, Any] = result.to_dict()
    elif isinstance(result, dict):
        data = result
    else:
        return []

    images: list[bytes] = []
    body = data.get("body", "")
    if isinstance(body, str) and body.strip():
        try:
            body_data = json.loads(body)
        except Exception:
            body_data = {}
        if isinstance(body_data, dict):
            raw_images = body_data.get("images")
            if isinstance(raw_images, list):
                for raw_image in raw_images:
                    try:
                        images.append(_decode_sd_image_text(str(raw_image or "")))
                    except Exception:
                        continue
            saved_images = body_data.get("saved_images")
            if isinstance(saved_images, list):
                for saved_path in saved_images:
                    try:
                        path = Path(str(saved_path or "")).expanduser()
                        if path.is_file():
                            images.append(path.read_bytes())
                    except Exception:
                        continue

    saved_images = data.get("saved_images")
    if isinstance(saved_images, list):
        for saved_path in saved_images:
            try:
                path = Path(str(saved_path or "")).expanduser()
                if path.is_file():
                    images.append(path.read_bytes())
            except Exception:
                continue
    return images


def append_sd_prompt_instruction(text: str) -> str:
    base = str(text or "").rstrip()
    if not base:
        return SD_PROMPT_INSTRUCTION
    return base + "\n\n" + SD_PROMPT_INSTRUCTION


def append_sd_prompt_system_instruction(system_prompt: str) -> str:
    base = str(system_prompt or "").rstrip()
    if not base:
        return SD_PROMPT_INSTRUCTION
    return base + "\n\n" + SD_PROMPT_INSTRUCTION


def extract_sd_prompt_block(
    text: str,
    begin_tag: str = _DEFAULT_BEGIN_TAG,
    end_tag: str = _DEFAULT_END_TAG,
) -> tuple[str, str]:
    source = str(text or "")
    prompt_parts: list[str] = []

    for active_begin, active_end in _tag_pairs(begin_tag, end_tag):
        pattern = _build_sd_prompt_re(active_begin, active_end)
        matches = list(pattern.finditer(source))
        if matches:
            prompt_parts.extend(m.group(1).strip() for m in matches if m.group(1).strip())
            source = pattern.sub("", source).strip()
            continue

        begin_match = re.search(re.escape(active_begin), source, re.IGNORECASE)
        if begin_match:
            prompt = source[begin_match.end() :].strip()
            if prompt:
                prompt_parts.append(prompt)
            source = source[: begin_match.start()].strip()

    return source.strip(), "\n".join(part for part in prompt_parts if part).strip()


def strip_sd_prompt_blocks_for_kks(
    text: str,
    begin_tag: str = _DEFAULT_BEGIN_TAG,
    end_tag: str = _DEFAULT_END_TAG,
) -> str:
    """KKSへ送る会話/字幕からSDプロンプトブロックを丸ごと落とす。"""
    cleaned, _ = extract_sd_prompt_block(text, begin_tag=begin_tag, end_tag=end_tag)
    for active_begin, active_end in _tag_pairs(begin_tag, end_tag):
        cleaned = re.sub(re.escape(active_begin), "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(re.escape(active_end), "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def apply_sd_prompt_rewrite_rules(
    prompt: str,
    rules: list[dict[str, Any]] | None,
) -> str:
    """AIが出したSDプロンプト本文に、登録順でワード書き換えルールを適用する。

    1ルール = {"enabled": bool, "mode": "replace"|"append", "from": str, "to": str}
    - enabled=false / from空 はスキップ
    - replace: from を大小無視で全置換（to はリテラル）
    - append: from を大小無視で含むなら末尾に ", {to}"（既に含む時は足さない）
    rules が空/None なら prompt をそのまま返す（＝従来動作）。
    """
    text = str(prompt or "")
    if not rules:
        return text
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("enabled", True)):
            continue
        from_str = str(entry.get("from", "") or "")
        if not from_str:
            continue
        to_str = str(entry.get("to", "") or "")
        mode = str(entry.get("mode", "replace") or "replace").strip().lower()
        if mode == "append":
            if re.search(re.escape(from_str), text, re.IGNORECASE):
                if to_str and to_str.lower() not in text.lower():
                    joiner = ", " if text.strip() else ""
                    text = f"{text}{joiner}{to_str}"
        else:  # replace
            text = re.sub(re.escape(from_str), lambda _m: to_str, text, flags=re.IGNORECASE)
    return text


def normalize_endpoint(endpoint: str, default: str = "/sdapi/v1/txt2img") -> str:
    value = str(endpoint or "").strip()
    if not value:
        return default
    if not value.startswith("/"):
        return "/" + value
    return value


def build_sd_prompt_url(host: str, port: int, endpoint: str) -> str:
    endpoint_path = normalize_endpoint(endpoint)
    host_value = str(host or "").strip() or "127.0.0.1"
    if host_value.startswith("http://") or host_value.startswith("https://"):
        return host_value.rstrip("/") + endpoint_path
    safe_port = max(1, min(65535, int(port)))
    return f"http://{host_value}:{safe_port}{endpoint_path}"


def _clean_dict(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        cleaned[key] = value
    return cleaned


def build_a1111_txt2img_payload(
    *,
    prompt: str,
    prompt_rewrite_rules: list[dict[str, Any]] | None = None,
    append_prompt: str = "",
    negative_prompt: str = "",
    steps: int = 20,
    width: int = 512,
    height: int = 768,
    cfg_scale: float = 7.0,
    sampler_name: str = "",
    scheduler: str = "",
    seed: int = -1,
    subseed: int = -1,
    subseed_strength: float = 0.0,
    batch_size: int = 1,
    n_iter: int = 1,
    restore_faces: bool = False,
    tiling: bool = False,
    save_images: bool = True,
    send_images: bool = False,
    enable_hr: bool = False,
    hr_scale: float = 2.0,
    hr_upscaler: str = "Latent",
    hr_second_pass_steps: int = 0,
    denoising_strength: float = 0.45,
    hr_resize_x: int = 0,
    hr_resize_y: int = 0,
    hr_sampler_name: str = "",
    hr_scheduler: str = "",
    hr_checkpoint_name: str = "",
    hr_prompt: str = "",
    hr_negative_prompt: str = "",
    extra_payload_json: str = "",
) -> dict[str, Any]:
    prompt_text = apply_sd_prompt_rewrite_rules(prompt, prompt_rewrite_rules).strip()
    append_text = str(append_prompt or "").strip()
    if append_text:
        joiner = ", " if prompt_text else ""
        prompt_text = f"{prompt_text}{joiner}{append_text}"

    payload: dict[str, Any] = _clean_dict(
        {
            "prompt": prompt_text,
            "negative_prompt": str(negative_prompt or ""),
            "steps": max(1, int(steps)),
            "width": max(64, int(width)),
            "height": max(64, int(height)),
            "cfg_scale": float(cfg_scale),
            "sampler_name": str(sampler_name or "").strip(),
            "scheduler": str(scheduler or "").strip(),
            "seed": int(seed),
            "subseed": int(subseed),
            "subseed_strength": float(subseed_strength),
            "batch_size": max(1, int(batch_size)),
            "n_iter": max(1, int(n_iter)),
            "restore_faces": bool(restore_faces),
            "tiling": bool(tiling),
            "save_images": bool(save_images),
            "send_images": bool(send_images),
        }
    )

    if bool(enable_hr):
        payload.update(
            _clean_dict(
                {
                    "enable_hr": True,
                    "hr_scale": float(hr_scale),
                    "hr_upscaler": str(hr_upscaler or "").strip(),
                    "hr_second_pass_steps": max(0, int(hr_second_pass_steps)),
                    "denoising_strength": float(denoising_strength),
                    "hr_resize_x": max(0, int(hr_resize_x)),
                    "hr_resize_y": max(0, int(hr_resize_y)),
                    "hr_sampler_name": str(hr_sampler_name or "").strip(),
                    "hr_scheduler": str(hr_scheduler or "").strip(),
                    "hr_checkpoint_name": str(hr_checkpoint_name or "").strip(),
                    "hr_prompt": str(hr_prompt or "").strip(),
                    "hr_negative_prompt": str(hr_negative_prompt or "").strip(),
                }
            )
        )
        payload["override_settings"] = {"save_images_before_highres_fix": False}
        payload["override_settings_restore_afterwards"] = True
    else:
        payload["enable_hr"] = False

    extra_text = str(extra_payload_json or "").strip()
    if extra_text:
        extra = json.loads(extra_text)
        if not isinstance(extra, dict):
            raise ValueError("extra payload JSON must be an object")
        payload.update(extra)
        payload["prompt"] = prompt_text
    return payload


def build_a1111_options_payload(
    *,
    model_checkpoint: str = "",
    vae: str = "",
    clip_skip: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    model = str(model_checkpoint or "").strip()
    if model:
        payload["sd_model_checkpoint"] = model
    vae_value = str(vae or "").strip()
    if vae_value:
        payload["sd_vae"] = vae_value
    safe_clip_skip = int(clip_skip)
    if safe_clip_skip > 0:
        payload["CLIP_stop_at_last_layers"] = safe_clip_skip
    return payload


def post_a1111_options(
    *,
    host: str,
    port: int,
    token: str = "",
    timeout_sec: float = 5.0,
    model_checkpoint: str = "",
    vae: str = "",
    clip_skip: int = 0,
) -> SdPromptSendResult:
    payload = build_a1111_options_payload(
        model_checkpoint=model_checkpoint,
        vae=vae,
        clip_skip=clip_skip,
    )
    url = build_sd_prompt_url(host, port, "/sdapi/v1/options")
    if not payload:
        return SdPromptSendResult(ok=True, status=0, url=url, body="{}", error="")

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    auth_token = str(token or "").strip()
    if auth_token:
        req.add_header("X-Auth-Token", auth_token)

    timeout = max(0.2, float(timeout_sec))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
        return SdPromptSendResult(ok=200 <= status < 300, status=status, url=url, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return SdPromptSendResult(ok=False, status=int(exc.code), url=url, body=body, error=f"HTTP {exc.code}")
    except Exception as exc:
        return SdPromptSendResult(ok=False, status=0, url=url, error=str(exc))


def send_sd_prompt(
    *,
    prompt: str,
    host: str,
    port: int,
    endpoint: str,
    token: str = "",
    timeout_sec: float = 5.0,
    source: str = "human_2_kks",
    event_id: str = "",
    meta: dict[str, Any] | None = None,
    a1111_payload: dict[str, Any] | None = None,
) -> SdPromptSendResult:
    prompt_text = str(prompt or "").strip()
    url = build_sd_prompt_url(host, port, endpoint)
    if not prompt_text:
        return SdPromptSendResult(ok=False, status=0, url=url, error="prompt is empty")

    if a1111_payload is not None:
        payload = dict(a1111_payload)
        payload["prompt"] = prompt_text
    else:
        payload = {
            "prompt": prompt_text,
            "source": str(source or "human_2_kks"),
            "event_id": str(event_id or ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if meta:
            payload["meta"] = meta

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    auth_token = str(token or "").strip()
    if auth_token:
        req.add_header("X-Auth-Token", auth_token)

    timeout = max(0.2, float(timeout_sec))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
        return SdPromptSendResult(ok=200 <= status < 300, status=status, url=url, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return SdPromptSendResult(ok=False, status=int(exc.code), url=url, body=body, error=f"HTTP {exc.code}")
    except Exception as exc:
        return SdPromptSendResult(ok=False, status=0, url=url, error=str(exc))


def post_a1111_interrupt(
    *,
    host: str,
    port: int,
    token: str = "",
    timeout_sec: float = 3.0,
) -> SdPromptSendResult:
    url = build_sd_prompt_url(host, port, "/sdapi/v1/interrupt")
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    auth_token = str(token or "").strip()
    if auth_token:
        req.add_header("X-Auth-Token", auth_token)
    timeout = max(0.2, float(timeout_sec))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
        return SdPromptSendResult(ok=200 <= status < 300, status=status, url=url, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return SdPromptSendResult(ok=False, status=int(exc.code), url=url, body=body, error=f"HTTP {exc.code}")
    except Exception as exc:
        return SdPromptSendResult(ok=False, status=0, url=url, error=str(exc))


def send_a1111_txt2img(
    *,
    prompt: str,
    prompt_rewrite_rules: list[dict[str, Any]] | None = None,
    host: str,
    port: int,
    endpoint: str,
    token: str = "",
    timeout_sec: float = 120.0,
    model_checkpoint: str = "",
    vae: str = "",
    clip_skip: int = 0,
    append_prompt: str = "",
    negative_prompt: str = "",
    steps: int = 20,
    width: int = 512,
    height: int = 768,
    cfg_scale: float = 7.0,
    sampler_name: str = "",
    scheduler: str = "",
    seed: int = -1,
    subseed: int = -1,
    subseed_strength: float = 0.0,
    batch_size: int = 1,
    n_iter: int = 1,
    restore_faces: bool = False,
    tiling: bool = False,
    save_images: bool = True,
    send_images: bool = False,
    enable_hr: bool = False,
    hr_scale: float = 2.0,
    hr_upscaler: str = "Latent",
    hr_second_pass_steps: int = 0,
    denoising_strength: float = 0.45,
    hr_resize_x: int = 0,
    hr_resize_y: int = 0,
    hr_sampler_name: str = "",
    hr_scheduler: str = "",
    hr_checkpoint_name: str = "",
    hr_prompt: str = "",
    hr_negative_prompt: str = "",
    extra_payload_json: str = "",
) -> SdPromptSendResult:
    options_result = post_a1111_options(
        host=host,
        port=port,
        token=token,
        timeout_sec=timeout_sec,
        model_checkpoint=model_checkpoint,
        vae=vae,
        clip_skip=clip_skip,
    )
    if not options_result.ok:
        return options_result

    payload = build_a1111_txt2img_payload(
        prompt=prompt,
        prompt_rewrite_rules=prompt_rewrite_rules,
        append_prompt=append_prompt,
        negative_prompt=negative_prompt,
        steps=steps,
        width=width,
        height=height,
        cfg_scale=cfg_scale,
        sampler_name=sampler_name,
        scheduler=scheduler,
        seed=seed,
        subseed=subseed,
        subseed_strength=subseed_strength,
        batch_size=batch_size,
        n_iter=n_iter,
        restore_faces=restore_faces,
        tiling=tiling,
        save_images=save_images,
        send_images=send_images,
        enable_hr=enable_hr,
        hr_scale=hr_scale,
        hr_upscaler=hr_upscaler,
        hr_second_pass_steps=hr_second_pass_steps,
        denoising_strength=denoising_strength,
        hr_resize_x=hr_resize_x,
        hr_resize_y=hr_resize_y,
        hr_sampler_name=hr_sampler_name,
        hr_scheduler=hr_scheduler,
        hr_checkpoint_name=hr_checkpoint_name,
        hr_prompt=hr_prompt,
        hr_negative_prompt=hr_negative_prompt,
        extra_payload_json=extra_payload_json,
    )
    return send_sd_prompt(
        prompt=str(payload.get("prompt", prompt) or prompt),
        host=host,
        port=port,
        endpoint=endpoint,
        token=token,
        timeout_sec=timeout_sec,
        a1111_payload=payload,
    )
