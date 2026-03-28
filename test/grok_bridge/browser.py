from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def connect_existing_debug_chrome(port: int) -> webdriver.Chrome:
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    return webdriver.Chrome(options=options)
