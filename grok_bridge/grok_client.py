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
JP_VOICE_MODE = "\u97f3\u58f0\u30e2\u30fc\u30c9\u306b\u3059\u308b"
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


def _latest_response_text(driver, selectors: Sequence[str], logger: logging.Logger | None = None) -> str:
    """最後の応答要素のみを取得する（複数セレクタフォールバック付き・診断ログ付き）。"""
    _FALLBACK_SELECTORS = [
        '.response-content-markdown.markdown',
        '[class*="response"][class*="markdown"]',
        '[class*="message"][class*="assistant"]',
        '.markdown',
    ]
    try:
        result = driver.execute_script(
            """
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
            """,
            _FALLBACK_SELECTORS,
        )
        import json as _json
        report = _json.loads(result) if isinstance(result, str) else []
        if logger:
            for entry in report:
                logger.info("selector_probe sel=%s count=%d text_len=%d preview=%.60s",
                            entry.get("selector", "?"), entry.get("count", 0),
                            entry.get("len", 0), entry.get("text", "")[:60])
        # 最初にヒットしたセレクタのテキストを返す
        for entry in report:
            text = entry.get("text", "").strip()
            if text:
                if logger:
                    logger.info("selector_hit sel=%s text_len=%d", entry.get("selector", "?"), len(text))
                return text
        if logger:
            logger.warning("selector_all_miss: no text found from any selector")
    except Exception as exc:
        if logger:
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
            return ((el && (el.innerText || el.textContent)) || '').trim();
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
            try {
              document.execCommand('selectAll', false, null);
              document.execCommand('delete', false, null);
              document.execCommand('insertText', false, value);
            } catch (_) {}
            el.innerText = value;
            el.textContent = value;
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


def _is_idle_voice_mode_button_present(driver) -> bool:
    selectors = [
        'button[aria-label*="\\u97f3\\u58f0\\u30e2\\u30fc\\u30c9\\u306b\\u3059\\u308b"]',
        'button[aria-label*="voice mode"]',
        'button[aria-label*="Voice mode"]',
    ]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed():
                    return True
            except Exception:
                continue

    try:
        has_voice_mode = driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            return buttons.some(b => {
              const aria = (b.getAttribute('aria-label') || '').toLowerCase();
              if (aria.includes('\\u97f3\\u58f0\\u30e2\\u30fc\\u30c9\\u306b\\u3059\\u308b')) return true;
              if (aria.includes('voice mode')) return true;
              return false;
            });
            """
        )
        return bool(has_voice_mode)
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
    """テキスト入力と送信ボタンクリックを1回のJS実行で行う。"""
    try:
        result = driver.execute_script(
            """
            const text = arguments[0];
            const input = document.querySelector('div.ProseMirror[contenteditable="true"]');
            if (!input) return 'no_input';
            input.focus();
            try {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, text);
            } catch (_) {
                input.innerText = text;
                input.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
            }
            // 送信ボタンを探してクリック
            const btn =
                document.querySelector('button[aria-label="\\u9001\\u4fe1"]') ||
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

    time.sleep(config.wait_after_send_seconds)
    return baseline, False


def _wait_stop_button_mode(
    driver,
    config: BridgeConfig,
    logger: logging.Logger,
    baseline_text: str,
) -> str:
    """停止ボタンの出現→消滅を監視して完了を検出する（従来方式）。"""
    deadline = time.time() + config.response_timeout_seconds
    selectors = config.selectors.response_blocks

    time.sleep(0.3)

    # Phase 1: 停止ボタンが出現するまで待つ（最大15秒）
    stop_appeared = False
    stop_wait_deadline = time.time() + 15.0
    while time.time() < stop_wait_deadline:
        if _is_stop_button_present(driver, config):
            stop_appeared = True
            logger.info("stop_button_appeared")
            break
        time.sleep(0.2)

    if not stop_appeared:
        logger.warning("stop_button_never_appeared, checking for instant response")
        text = _latest_response_text(driver, selectors, logger)
        if text and text != baseline_text:
            logger.info("instant_response len=%d", len(text))
            return text
        else:
            logger.warning("instant_check: text_len=%d baseline_match=%s", len(text), text == baseline_text)

    # Phase 2: 停止ボタンが消えるまで待つ
    poll_count = 0
    while time.time() < deadline:
        if not _is_stop_button_present(driver, config):
            # 最初の数回と10回ごとに詳細ログ
            use_detail = poll_count < 3 or poll_count % 10 == 0
            text = _latest_response_text(driver, selectors, logger if use_detail else None)
            if text and text != baseline_text:
                logger.info("response_complete len=%d poll_count=%d", len(text), poll_count)
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


def _wait_text_stable_mode(
    driver,
    config: BridgeConfig,
    logger: logging.Logger,
    baseline_text: str,
) -> str:
    """テキスト変化の安定を監視して完了を検出する（汎用方式）。"""
    deadline = time.time() + config.response_timeout_seconds
    selectors = config.selectors.response_blocks
    stable_threshold = config.text_stable_seconds
    poll_interval = 0.5

    time.sleep(0.3)

    last_text = ""
    last_change_time = time.time()
    response_started = False

    poll_count = 0
    while time.time() < deadline:
        # 最初の3回と10回ごとに詳細ログ
        use_detail = poll_count < 3 or poll_count % 10 == 0
        text = _latest_response_text(driver, selectors, logger if use_detail else None)

        if text and text != baseline_text:
            if not response_started:
                logger.info("text_stable: response_started len=%d poll=%d", len(text), poll_count)
                response_started = True

            if text != last_text:
                last_text = text
                last_change_time = time.time()
                logger.info("text_stable: text_changed len=%d poll=%d", len(text), poll_count)
            elif response_started and (time.time() - last_change_time) >= stable_threshold:
                logger.info("text_stable: stable_complete len=%d (%.1fs stable) poll=%d",
                            len(text), time.time() - last_change_time, poll_count)
                return text
        elif use_detail and poll_count > 0:
            logger.debug("text_stable: no_change text_len=%d baseline_match=%s poll=%d",
                         len(text), text == baseline_text, poll_count)

        poll_count += 1
        time.sleep(poll_interval)

    # タイムアウト: 最終診断ログ
    logger.warning("text_stable: timeout_reached poll_count=%d, final probe:", poll_count)
    _latest_response_text(driver, selectors, logger)
    if last_text and last_text != baseline_text:
        logger.warning("text_stable: timeout_return_last len=%d", len(last_text))
        return last_text
    logger.error("text_stable: all_selectors_failed after %d polls", poll_count)
    raise TimeoutError("Timed out while waiting for Grok response.")


def wait_for_response(
    driver,
    config: BridgeConfig,
    logger: logging.Logger,
    baseline_text: str,
    stop_before: bool,
) -> str:
    """設定に応じた方式でGrok応答を待機する。"""
    mode = config.response_detection_mode
    logger.info("wait_for_response mode=%s timeout=%.0fs", mode, config.response_timeout_seconds)

    if mode == "text_stable":
        return _wait_text_stable_mode(driver, config, logger, baseline_text)
    else:
        return _wait_stop_button_mode(driver, config, logger, baseline_text)
