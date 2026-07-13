from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


def connect_existing_debug_chrome(port: int) -> webdriver.Chrome:
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    return webdriver.Chrome(options=options)


def _url_without_fragment(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _urls_are_close(current_url: str, target_url: str) -> bool:
    current = _url_without_fragment(current_url)
    target = _url_without_fragment(target_url)
    if not current or not target:
        return False
    if current == target:
        return True
    cur_parts = urlsplit(current)
    tgt_parts = urlsplit(target)
    if cur_parts.netloc != tgt_parts.netloc:
        return False
    cur_path = cur_parts.path.rstrip("/")
    tgt_path = tgt_parts.path.rstrip("/")
    if cur_path == tgt_path:
        return True
    return bool(tgt_path) and (cur_path.startswith(tgt_path + "/") or tgt_path.startswith(cur_path + "/"))


def _document_ready_state(driver) -> str:
    try:
        return str(driver.execute_script("return document.readyState || '';") or "").strip().lower()
    except Exception:
        return ""


def _input_is_visible(driver, input_selector: str) -> bool:
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, input_selector)
    except Exception:
        return False
    for element in elements:
        try:
            if element.is_displayed():
                return True
        except Exception:
            continue
    return False


def _activate_current_tab(driver, log: Callable[[str], None] | None = None) -> None:
    try:
        handle = str(driver.current_window_handle or "")
        if handle:
            driver.execute_cdp_cmd("Target.activateTarget", {"targetId": handle})
            if log:
                log(f"open_url activated tab handle={handle}")
    except Exception as exc:
        if log:
            log(f"open_url activate tab failed error={exc}")


def _switch_to_matching_tab(
    driver,
    target_url: str,
    log: Callable[[str], None] | None = None,
) -> bool:
    handles = list(getattr(driver, "window_handles", []) or [])
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            current_url = str(driver.current_url or "")
        except Exception:
            continue
        if _urls_are_close(current_url, target_url):
            _activate_current_tab(driver, log)
            if log:
                log(f"open_url selected matching tab handle={handle} current_url={current_url}")
            return True
    return False


def _open_fresh_tab(driver, target_url: str, log: Callable[[str], None] | None = None) -> None:
    try:
        driver.switch_to.new_window("tab")
        _activate_current_tab(driver, log)
        if log:
            log(f"open_url created fresh tab handle={driver.current_window_handle} target={target_url}")
        return
    except Exception as exc:
        if log:
            log(f"open_url new_window failed error={exc}")

    try:
        before = set(getattr(driver, "window_handles", []) or [])
        driver.execute_script("window.open('about:blank', '_blank');")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            handles = list(getattr(driver, "window_handles", []) or [])
            created = [h for h in handles if h not in before]
            if created:
                driver.switch_to.window(created[-1])
                _activate_current_tab(driver, log)
                if log:
                    log(f"open_url created fresh tab by window.open handle={created[-1]} target={target_url}")
                return
            time.sleep(0.05)
        handles = list(getattr(driver, "window_handles", []) or [])
        if handles:
            driver.switch_to.window(handles[-1])
    except Exception as exc:
        if log:
            log(f"open_url window.open failed; using current tab error={exc}")


def _navigate(driver, target_url: str, attempt: int, log: Callable[[str], None] | None = None) -> None:
    if attempt == 1:
        try:
            result = driver.execute_cdp_cmd("Page.navigate", {"url": target_url})
            if log:
                log(f"open_url navigate=cdp result={result}")
            return
        except Exception as exc:
            if log:
                log(f"open_url navigate=cdp failed error={exc}")

    if attempt <= 2:
        try:
            driver.execute_script("window.location.href = arguments[0];", target_url)
            if log:
                log("open_url navigate=js_location")
            return
        except Exception as exc:
            if log:
                log(f"open_url navigate=js_location failed error={exc}")

    driver.get(target_url)
    if log:
        log("open_url navigate=driver_get")


def open_url_with_confirmed_input(
    driver,
    target_url: str,
    input_selector: str,
    *,
    attempts: int = 3,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.25,
    fresh_tab: bool = False,
    log: Callable[[str], None] | None = None,
) -> str:
    """Open a Grok URL and confirm navigation, document readiness, and visible input."""
    last_exc: Exception | None = None
    last_url = ""
    max_attempts = max(1, int(attempts))
    timeout = max(1.0, float(timeout_seconds))
    poll = max(0.05, float(poll_seconds))

    def emit(message: str) -> None:
        if log:
            log(message)

    for attempt in range(1, max_attempts + 1):
        emit(f"open_url attempt={attempt}/{max_attempts} target={target_url}")
        try:
            if fresh_tab and attempt == 1:
                if _switch_to_matching_tab(driver, target_url, emit):
                    emit("open_url reusing matching astral tab")
                else:
                    _open_fresh_tab(driver, target_url, emit)
            elif fresh_tab:
                _activate_current_tab(driver, emit)
            if not fresh_tab and attempt == 1:
                _activate_current_tab(driver, emit)
            _navigate(driver, target_url, attempt, emit)
            deadline = time.monotonic() + timeout
            url_ok = False
            ready_ok = False
            input_ok = False
            ready_state = ""
            while time.monotonic() < deadline:
                try:
                    last_url = str(driver.current_url or "")
                except Exception as exc:
                    last_exc = exc
                    last_url = ""

                url_ok = _urls_are_close(last_url, target_url)
                ready_state = _document_ready_state(driver)
                ready_ok = ready_state in ("interactive", "complete")
                input_ok = _input_is_visible(driver, input_selector) if ready_ok else False

                if url_ok and ready_ok and input_ok:
                    _activate_current_tab(driver, emit)
                    emit(
                        f"open_url success attempt={attempt}/{max_attempts} "
                        f"current_url={last_url} readyState={ready_state}"
                    )
                    return last_url

                time.sleep(poll)

            emit(
                f"open_url retry attempt={attempt}/{max_attempts} "
                f"url_ok={int(url_ok)} ready_ok={int(ready_ok)} input_ok={int(input_ok)} "
                f"readyState={ready_state or '(empty)'} current_url={last_url or '(empty)'}"
            )
        except Exception as exc:
            last_exc = exc
            try:
                last_url = str(driver.current_url or "")
            except Exception:
                last_url = ""
            emit(
                f"open_url exception attempt={attempt}/{max_attempts} "
                f"current_url={last_url or '(empty)'} error={exc}"
            )

    raise RuntimeError(
        f"open_url failed attempts={max_attempts} current_url={last_url or '(empty)'} "
        f"error={last_exc!r}"
    )
