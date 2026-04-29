from __future__ import annotations

import logging
import time
from typing import Sequence

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .config import BridgeConfig


JP_SEND = "\u9001\u4fe1"
JP_STOP = "\u505c\u6b62"
JP_STOP_RESPONSE = "\u5fdc\u7b54\u3092\u505c\u6b62"
JP_STOP_MODEL_RESPONSE = "\u30e2\u30c7\u30eb\u306e\u5fdc\u7b54\u3092\u505c\u6b62"
SEND_READY_SELECTORS = [
    'button[aria-label*="\\u9001\\u4fe1"]',
    'button[aria-label*="send"]',
    "button[type=\"submit\"]",
]


def _is_grok_url(url: str) -> bool:
    lower = (url or "").lower()
    return "grok.com" in lower or "x.com/i/grok" in lower


def ensure_current_tab_is_grok(driver) -> None:
    current_url = driver.current_url or ""
    if not _is_grok_url(current_url):
        raise RuntimeError(f"Current tab is not Grok: {current_url}")


_FALLBACK_SELECTORS = [
    '.response-content-markdown.markdown',
    '[class*="response"][class*="markdown"]',
    '[class*="message"][class*="assistant"]',
    '.markdown',
]

# 軽量JS: 最初にヒットしたセレクタのテキストだけ返す（診断不要時用）
_FAST_JS = """
var sels = arguments[0];
for (var i = 0; i < sels.length; i++) {
    var nodes = document.querySelectorAll(sels[i]);
    if (nodes.length > 0) {
        var t = (nodes[nodes.length - 1].innerText || '').trim();
        if (t) return t;
    }
}
return '';
"""

# 診断JS: 全セレクタの詳細を返す（診断時用）
_DIAG_JS = """
var selectors = arguments[0];
var report = [];
for (var i = 0; i < selectors.length; i++) {
    var nodes = document.querySelectorAll(selectors[i]);
    var count = nodes.length;
    var text = '';
    if (count > 0) {
        text = (nodes[nodes.length - 1].innerText || '').trim();
    }
    report.push({selector: selectors[i], count: count, len: text.length, text: text});
}
return JSON.stringify(report);
"""


def _latest_response_text(driver, selectors: Sequence[str], logger: logging.Logger | None = None) -> str:
    """最後の応答要素のみを取得する（複数セレクタフォールバック付き・診断ログ付き）。"""
    actual_selectors = list(selectors) if selectors else list(_FALLBACK_SELECTORS)

    if not logger:
        # 軽量パス: 1回のJS実行で最初にヒットしたテキストだけ返す
        try:
            text = driver.execute_script(_FAST_JS, actual_selectors)
            return (text or "").strip()
        except Exception:
            return ""

    # 診断パス: 全セレクタの詳細をログ出力
    try:
        result = driver.execute_script(_DIAG_JS, actual_selectors)
        import json as _json
        report = _json.loads(result) if isinstance(result, str) else []
        for entry in report:
            logger.info("selector_probe sel=%s count=%d text_len=%d preview=%.60s",
                        entry.get("selector", "?"), entry.get("count", 0),
                        entry.get("len", 0), entry.get("text", "")[:60])
        # 最初にヒットしたセレクタのテキストを返す
        for entry in report:
            text = entry.get("text", "").strip()
            if text:
                logger.info("selector_hit sel=%s text_len=%d", entry.get("selector", "?"), len(text))
                return text
        logger.warning("selector_all_miss: no text found from any selector")
    except Exception as exc:
        logger.error("selector_error: %s", exc)
    return ""


def _is_idle(driver) -> bool:
    """停止ボタンが存在しない = Grokがアイドル状態。"""
    try:
        result = driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            return !buttons.some(b => {
              const aria = (b.getAttribute('aria-label') || '').toLowerCase();
              if (aria.includes('\\u30e2\\u30c7\\u30eb\\u306e\\u5fdc\\u7b54\\u3092\\u505c\\u6b62')) return true;
              if (aria.includes('\\u5fdc\\u7b54\\u3092\\u505c\\u6b62')) return true;
              if (aria.includes('\\u505c\\u6b62')) return true;
              if (aria.includes('stop model response')) return true;
              if (aria.includes('stop response')) return true;
              const html = b.innerHTML || '';
              return html.includes('M4 9.2v5.6') && html.includes('fill=\"currentColor\"');
            });
            """
        )
        return bool(result)
    except Exception:
        return False


def _read_input_text(driver, input_element) -> str:
    try:
        text = driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return '';
            if (typeof el.value === 'string') return el.value.trim();
            return ((el.innerText || el.textContent) || '').trim();
            """,
            input_element,
        )
        if isinstance(text, str):
            return text.strip()
    except Exception:
        pass
    try:
        return (input_element.text or "").strip()
    except Exception:
        return ""


def _is_send_button_ready(driver, selectors: Sequence[str]) -> bool:
    for selector in selectors:
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    return True
            except Exception:
                continue
    return False


def _guess_send_block_reason(driver) -> str:
    try:
        body_text = driver.execute_script("return (document.body && document.body.innerText) ? document.body.innerText : '';")
    except Exception:
        body_text = ""
    text = (body_text or "").strip()
    if "SuperGrok" in text or "\u30a2\u30c3\u30d7\u30b0\u30ec\u30fc\u30c9" in text:
        return "Grok input is blocked by plan/quota state (upgrade prompt visible)."
    if "\u5fdc\u7b54\u304c\u3042\u308a\u307e\u305b\u3093" in text:
        return "Previous response failed and input is currently blocked."
    return "Send button is disabled after text input."


def _set_input_text(driver, input_element, text: str) -> bool:
    input_element.click()
    try:
        input_element.send_keys(Keys.CONTROL, "a")
        input_element.send_keys(Keys.BACKSPACE)
    except Exception:
        pass

    try:
        input_element.send_keys(text)
        current = _read_input_text(driver, input_element)
        if current and _is_send_button_ready(driver, SEND_READY_SELECTORS):
            return True
    except Exception:
        pass

    # Retry with chunked typing.
    try:
        input_element.send_keys(Keys.CONTROL, "a")
        input_element.send_keys(Keys.BACKSPACE)
    except Exception:
        pass
    try:
        chunk_size = 50
        for i in range(0, len(text), chunk_size):
            input_element.send_keys(text[i : i + chunk_size])
        current = _read_input_text(driver, input_element)
        if current and _is_send_button_ready(driver, SEND_READY_SELECTORS):
            return True
    except Exception:
        pass

    # Last fallback: force DOM text and events.
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            if (!el) return;
            el.focus();
            if (typeof el.value === 'string') {
              el.value = value;
            } else {
              try {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, value);
              } catch (_) {}
              el.innerText = value;
              el.textContent = value;
            }
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            input_element,
            text,
        )
    except Exception:
        pass

    current = _read_input_text(driver, input_element)
    return bool(current) and _is_send_button_ready(driver, SEND_READY_SELECTORS)


def _is_stop_button_present(driver, config: BridgeConfig) -> bool:
    for selector in config.selectors.stop_buttons:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            continue

    try:
        has_stop_state = driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            return buttons.some(b => {
              const aria = (b.getAttribute('aria-label') || '').toLowerCase();
              if (aria.includes('\\u30e2\\u30c7\\u30eb\\u306e\\u5fdc\\u7b54\\u3092\\u505c\\u6b62')) return true;
              if (aria.includes('\\u5fdc\\u7b54\\u3092\\u505c\\u6b62')) return true;
              if (aria.includes('\\u505c\\u6b62')) return true;
              if (aria.includes('stop model response')) return true;
              if (aria.includes('stop response')) return true;
              const html = b.innerHTML || '';
              return html.includes('M4 9.2v5.6') && html.includes('fill=\"currentColor\"');
            });
            """
        )
        return bool(has_stop_state)
    except Exception:
        return False


def _click_send_button(driver, config: BridgeConfig, logger: logging.Logger) -> None:
    for selector in config.selectors.send_buttons:
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for button in buttons:
            try:
                if not button.is_displayed() or not button.is_enabled():
                    continue
                driver.execute_script("arguments[0].click();", button)
                logger.info("send_clicked selector=%s", selector)
                return
            except Exception:
                continue

    try:
        button = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'button[aria-label*="\\u9001\\u4fe1"], button[aria-label*="send"], button[type="submit"]')
            )
        )
        driver.execute_script("arguments[0].click();", button)
        logger.info("send_clicked selector=fallback_any_send_button")
        return
    except Exception:
        pass

    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for button in buttons:
            aria = (button.get_attribute("aria-label") or "")
            aria_lower = aria.lower()
            if "send" not in aria_lower and JP_SEND not in aria:
                continue
            if not button.is_displayed() or not button.is_enabled():
                continue
            driver.execute_script("arguments[0].click();", button)
            logger.info("send_clicked by aria=%s", aria)
            return
    except Exception:
        pass

    try:
        input_element = driver.find_element(By.CSS_SELECTOR, config.selectors.input)
        input_element.send_keys(Keys.ENTER)
        logger.info("send_clicked by enter")
        return
    except Exception:
        pass

    try:
        input_element = driver.find_element(By.CSS_SELECTOR, config.selectors.input)
        input_element.send_keys(Keys.CONTROL, Keys.ENTER)
        logger.info("send_clicked by ctrl+enter")
        return
    except Exception as exc:
        raise RuntimeError(f"Failed to send text: {exc}") from exc


def _set_and_send(driver, text: str) -> bool:
    """旧ProseMirror入力欄だけ、テキスト入力と送信クリックを1回のJS実行で行う。"""
    try:
        result = driver.execute_script(
            """
            const text = arguments[0];
            const input = document.querySelector('div.ProseMirror[contenteditable="true"]');
            if (!input) return 'no_fast_input';
            input.focus();
            try {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, text);
            } catch (_) {
                input.innerText = text;
            }
            input.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            // 送信ボタンを探してクリック
            const btn =
                document.querySelector('button[aria-label="\\u9001\\u4fe1"]') ||
                document.querySelector('button[aria-label*="\\u9001\\u4fe1"]') ||
                document.querySelector('button[aria-label*="send"]') ||
                document.querySelector('button[type="submit"]');
            if (btn && !btn.disabled && btn.offsetParent !== null) {
                btn.click();
                return 'sent';
            }
            return 'no_button';
            """,
            text,
        )
        return str(result) == "sent"
    except Exception:
        return False


def send_text(driver, config: BridgeConfig, text: str, logger: logging.Logger) -> tuple[str, bool]:
    ensure_current_tab_is_grok(driver)
    logger.info("baseline_capture_start")
    baseline = _latest_response_text(driver, config.selectors.response_blocks, logger)
    logger.info("baseline_response_len=%d preview=%.60s", len(baseline), baseline[:60])

    result = _set_and_send(driver, text)
    logger.info("set_and_send=%s", result)

    if not result:
        # フォールバック: 従来方式
        logger.warning("set_and_send failed, falling back to WebDriver send")
        try:
            input_element = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, config.selectors.input))
            )
        except Exception as exc:
            raise RuntimeError(f"Grok input not found: {exc}") from exc
        if not _set_input_text(driver, input_element, text):
            reason = _guess_send_block_reason(driver)
            raise RuntimeError(f"Failed to set input text. {reason}")
        _click_send_button(driver, config, logger)

    t_send = time.time()
    logger.info("[timing] wait_after_send start (%.1fs)", config.wait_after_send_seconds)
    time.sleep(config.wait_after_send_seconds)
    logger.info("[timing] wait_after_send done (+%.2fs)", time.time() - t_send)
    return baseline, False


def _wait_stop_button_mode(
    driver,
    config: BridgeConfig,
    logger: logging.Logger,
    baseline_text: str,
) -> str:
    """停止ボタンの出現→消滅を監視して完了を検出する（従来方式）。"""
    t0 = time.time()
    deadline = t0 + config.response_timeout_seconds
    selectors = config.selectors.response_blocks

    logger.info("[timing] phase0: initial sleep 0.3s")
    time.sleep(0.3)
    logger.info("[timing] phase0 done (+%.2fs)", time.time() - t0)

    # Phase 1: 停止ボタンが出現するまで待つ（最大5秒、高頻度ポーリング）
    stop_appeared = False
    phase1_deadline = time.time() + 1.0
    t_phase1 = time.time()
    logger.info("[timing] phase1: waiting for stop_button (max 1s)")
    while time.time() < phase1_deadline:
        if _is_stop_button_present(driver, config):
            stop_appeared = True
            logger.info("[timing] phase1: stop_button_appeared (+%.2fs)", time.time() - t0)
            break
        time.sleep(0.05)

    if not stop_appeared:
        logger.warning("[timing] phase1: stop_button_never_appeared (waited %.2fs)", time.time() - t_phase1)

    # Phase 2: stopボタンが消えるまで待つ OR stopを見逃した場合はテキスト変化を直接待つ
    poll_count = 0
    t_phase2 = time.time()
    logger.info("[timing] phase2: waiting for response (stop_appeared=%s)", stop_appeared)
    while time.time() < deadline:
        if stop_appeared:
            # stopボタンが消えたらテキスト取得
            if not _is_stop_button_present(driver, config):
                text = _latest_response_text(driver, selectors, logger if poll_count < 3 else None)
                if text:
                    logger.info("[timing] phase2: response_complete len=%d poll=%d (+%.2fs total)", len(text), poll_count, time.time() - t0)
                    return text
            else:
                if poll_count < 5 or poll_count % 10 == 0:
                    logger.info("[timing] phase2: stop_button still present poll=%d (+%.2fs)", poll_count, time.time() - t_phase2)
        else:
            # stopを見逃した場合はテキスト変化を直接ポーリング
            text = _latest_response_text(driver, selectors, logger if poll_count < 3 else None)
            if text and text != baseline_text:
                logger.info("[timing] phase2: text_changed len=%d poll=%d (+%.2fs total)", len(text), poll_count, time.time() - t0)
                return text
        poll_count += 1
        time.sleep(config.response_poll_seconds)

    logger.warning("timeout_reached poll_count=%d, final probe:", poll_count)
    text = _latest_response_text(driver, selectors, logger)
    if text and text != baseline_text:
        logger.warning("response_timeout_return_last len=%d", len(text))
        return text
    logger.error("all_selectors_failed: response empty after %d polls", poll_count)
    raise TimeoutError("Timed out while waiting for Grok response.")


def wait_for_response(
    driver,
    config: BridgeConfig,
    logger: logging.Logger,
    baseline_text: str,
    stop_before: bool,
) -> str:
    """Grok応答を待機する。"""
    logger.info("wait_for_response timeout=%.0fs", config.response_timeout_seconds)
    return _wait_stop_button_mode(driver, config, logger, baseline_text)
