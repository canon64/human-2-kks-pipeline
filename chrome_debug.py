"""
undetected-chromedriverでChromeを起動し、Seleniumで操作する
Cloudflare/Bot検知を回避する
"""
import json
import logging
import os
import re
import subprocess
import time

log = logging.getLogger(__name__)

# Chrome関連のパス
CHROME_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

# 起動中のdriver管理
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

    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", "version"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon", "version"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon", "version"),
    ]

    for root, sub_key, value_name in candidates:
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            if value:
                return str(value)
        except Exception:
            continue

    return ""


def _read_chrome_version_from_exe() -> str:
    candidates: list[str] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LOCALAPPDATA", "")

    candidates.append(os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"))
    candidates.append(os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"))
    if local_app:
        candidates.append(os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"))

    for exe_path in candidates:
        if not os.path.exists(exe_path):
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


def get_profiles() -> list[dict]:
    """利用可能なChromeプロファイル一覧を取得する"""
    profiles = []

    if not os.path.exists(CHROME_USER_DATA):
        log.warning(f"Chrome User Dataが見つかりません: {CHROME_USER_DATA}")
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

            account_info = prefs.get("account_info", [{}])
            if account_info:
                info = account_info[0]
                email = info.get("email", "")
                name = info.get("full_name", "")
            else:
                email = ""
                name = ""

            profiles.append({
                "profile_dir": item,
                "name": name,
                "email": email,
            })
        except Exception as e:
            log.warning(f"プロファイル読み込み失敗: {item} - {e}")
            continue

    def sort_key(p):
        if p["profile_dir"] == "Default":
            return (0, 0)
        try:
            num = int(p["profile_dir"].replace("Profile ", ""))
            return (1, num)
        except Exception:
            return (2, p["profile_dir"])

    profiles.sort(key=sort_key)
    return profiles


def launch_chrome(
    port: int = 9222,
    headless: bool = False,
    extra_args: list[str] = None,
    data_dir: str = "",
):
    """undetected-chromedriverでChromeを起動し、driverを返す

    初回は空プロファイルで起動。grok.comに手動ログインすれば
    Cookieがdata_dirに保存され、2回目以降は自動ログイン。
    """
    global _uc_driver
    import undetected_chromedriver as uc

    if data_dir:
        user_data = data_dir
    else:
        user_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"chrome_debug_data_{port}")

    # ロックファイル残存対策
    if os.path.isdir(user_data):
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(user_data, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except OSError:
                    pass

    options = uc.ChromeOptions()
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    if headless:
        options.add_argument("--headless=new")

    if extra_args:
        for arg in extra_args:
            options.add_argument(arg)

    detected_major = _detect_chrome_major()
    if detected_major is not None:
        log.info("Chrome起動 (undetected): port=%s, chrome_major=%s", port, detected_major)
    else:
        log.info("Chrome起動 (undetected): port=%s, chrome_major=auto", port)

    try:
        _uc_driver = _launch_uc(
            uc_mod=uc,
            options=options,
            user_data=user_data,
            port=port,
            version_main=detected_major,
        )
    except Exception as first_exc:
        # 版ズレ時はブラウザ側majorを例外文から拾って1回だけリトライする。
        first_err = str(first_exc)
        retry_major = _extract_browser_major_from_error(first_err)
        if retry_major is not None and retry_major != detected_major:
            log.warning(
                "ChromeDriver版ズレ検知: detected=%s, retry=%s",
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
    """起動済みのdriverを返す"""
    global _uc_driver
    if wait > 0:
        time.sleep(wait)

    if _uc_driver:
        return _uc_driver

    # フォールバック: 既存のデバッグChromeに通常seleniumで接続
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    log.info(f"Selenium接続 (fallback): port={port}")
    return webdriver.Chrome(options=options)


def close_chrome():
    """起動したChromeを終了する"""
    global _uc_driver

    if _uc_driver:
        log.info("Chrome終了")
        try:
            _uc_driver.quit()
        except Exception:
            pass
        _uc_driver = None
