"""
undetected-chromedriverでChromeを起動し、Seleniumで操作する
Cloudflare/Bot検知を回避する
"""
import json
import logging
import os
import time

log = logging.getLogger(__name__)

# Chrome関連のパス
CHROME_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

# 起動中のdriver管理
_uc_driver = None


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

    log.info(f"Chrome起動 (undetected): port={port}")

    _uc_driver = uc.Chrome(
        options=options,
        user_data_dir=user_data,
        port=port,
    )

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
