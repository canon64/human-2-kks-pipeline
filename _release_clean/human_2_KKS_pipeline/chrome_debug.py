"""Chrome debug launcher utilities."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

log = logging.getLogger(__name__)

CHROME_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
_uc_driver = None


def _parse_major(version_text: str) -> int | None:
    if not version_text:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", version_text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _read_chrome_version_from_registry() -> str:
    try:
        import winreg  # type: ignore
    except Exception:
        return ""

    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", "version"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon", "version"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon", "version"),
    ]

    for root, sub_key, value_name in keys:
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            if value:
                return str(value)
        except Exception:
            continue

    return ""


def _read_chrome_version_from_exe() -> str:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    if local_app:
        candidates.append(os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"))

    for exe_path in candidates:
        if not os.path.isfile(exe_path):
            continue
        try:
            proc = subprocess.run(
                [exe_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=5,
                check=False,
            )
            text = (proc.stdout or proc.stderr or "").strip()
            if text:
                return text
        except Exception:
            continue

    return ""


def _detect_chrome_major() -> int | None:
    reg_ver = _read_chrome_version_from_registry()
    major = _parse_major(reg_ver)
    if major is not None:
        return major

    exe_ver = _read_chrome_version_from_exe()
    major = _parse_major(exe_ver)
    if major is not None:
        return major

    return None


def _extract_browser_major_from_error(err_text: str) -> int | None:
    if not err_text:
        return None
    m = re.search(r"Current browser version is\s+(\d+)\.", err_text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _launch_uc(uc_mod, options, user_data: str, port: int, version_main: int | None):
    kwargs = {
        "options": options,
        "user_data_dir": user_data,
        "port": port,
    }
    if version_main is not None:
        kwargs["version_main"] = version_main
    return uc_mod.Chrome(**kwargs)


def _remove_stale_locks(user_data: str) -> None:
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(user_data, lock_name)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass


def get_profiles() -> list[dict]:
    profiles: list[dict] = []

    if not os.path.isdir(CHROME_USER_DATA):
        log.warning("Chrome User Data not found: %s", CHROME_USER_DATA)
        return profiles

    for item in os.listdir(CHROME_USER_DATA):
        item_path = os.path.join(CHROME_USER_DATA, item)
        if not os.path.isdir(item_path):
            continue
        if item != "Default" and not item.startswith("Profile "):
            continue

        prefs_path = os.path.join(item_path, "Preferences")
        if not os.path.exists(prefs_path):
            continue

        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)

            account_info = prefs.get("account_info") or []
            first = account_info[0] if account_info and isinstance(account_info[0], dict) else {}
            profiles.append(
                {
                    "profile_dir": item,
                    "name": str(first.get("full_name", "")),
                    "email": str(first.get("email", "")),
                }
            )
        except Exception as e:
            log.warning("Profile read failed: %s - %s", item, e)

    def sort_key(p: dict) -> tuple[int, int | str]:
        if p["profile_dir"] == "Default":
            return (0, 0)
        try:
            return (1, int(str(p["profile_dir"]).replace("Profile ", "")))
        except Exception:
            return (2, str(p["profile_dir"]))

    profiles.sort(key=sort_key)
    return profiles


def launch_chrome(
    port: int = 9222,
    headless: bool = False,
    extra_args: list[str] | None = None,
    data_dir: str = "",
    profile_dir: str = "",
):
    """Launch Chrome with undetected_chromedriver and keep major versions aligned."""
    global _uc_driver
    import undetected_chromedriver as uc

    user_data = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), f"chrome_debug_data_{port}")
    os.makedirs(user_data, exist_ok=True)
    _remove_stale_locks(user_data)

    options = uc.ChromeOptions()
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-host=127.0.0.1")
    options.add_argument(f"--remote-debugging-port={port}")
    options.add_argument(f"--user-data-dir={user_data}")
    if profile_dir:
        options.add_argument(f"--profile-directory={profile_dir}")
    if headless:
        options.add_argument("--headless=new")
    if extra_args:
        for arg in extra_args:
            options.add_argument(arg)

    detected_major = _detect_chrome_major()
    log.info(
        "Chrome launch (undetected): port=%s, detected_major=%s, user_data_dir=%s",
        port,
        detected_major if detected_major is not None else "auto",
        user_data,
    )

    try:
        _uc_driver = _launch_uc(
            uc_mod=uc,
            options=options,
            user_data=user_data,
            port=port,
            version_main=detected_major,
        )
    except Exception as first_exc:
        first_err = str(first_exc)
        retry_major = _extract_browser_major_from_error(first_err)
        if retry_major is not None and retry_major != detected_major:
            log.warning(
                "ChromeDriver major mismatch detected: detected=%s, retry=%s",
                detected_major,
                retry_major,
            )
            _uc_driver = _launch_uc(
                uc_mod=uc,
                options=options,
                user_data=user_data,
                port=port,
                version_main=retry_major,
            )
        else:
            raise

    return _uc_driver


def get_driver(port: int = 9222, wait: float = 0):
    global _uc_driver
    if wait > 0:
        time.sleep(wait)

    if _uc_driver:
        return _uc_driver

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    log.info("Selenium connect (fallback): port=%s", port)
    return webdriver.Chrome(options=options)


def close_chrome() -> None:
    global _uc_driver
    if _uc_driver:
        log.info("Chrome close")
        try:
            _uc_driver.quit()
        except Exception:
            pass
        _uc_driver = None
