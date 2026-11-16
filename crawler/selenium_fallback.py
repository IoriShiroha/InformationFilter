# -*- coding: utf-8 -*-
"""Selenium 兜底渲染（沿用原 main.py 里无头 Chrome 的思路）。

只在「正文过短，疑似 JS 渲染」或源配置显式要求时调用；
渲染失败一律降级为空字符串，不阻断流水线。
"""

from __future__ import annotations

import time
from typing import Callable

from settings import SeleniumConfig

_CHROME_ARGS = (
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
    "--log-level=3",
    "--disable-blink-features=AutomationControlled",
    "--lang=zh-CN",
    "--disable-notifications",
    "--mute-audio",
)


def fetch_html(
    url: str,
    config: SeleniumConfig,
    *,
    log: Callable[[str], None] | None = None,
    user_agent: str = "",
) -> str:
    log = log or (lambda _msg: None)
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except Exception as exc:  # 未安装 selenium
        log(f"Selenium 不可用（未安装或导入失败）: {exc}")
        return ""

    options = Options()
    if config.headless:
        options.add_argument("--headless=new")
    for arg in _CHROME_ARGS:
        options.add_argument(arg)
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    # 隐藏自动化特征（常规做法，不做任何对抗性破解）
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    try:
        if config.driver_path:
            driver = webdriver.Chrome(service=Service(config.driver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(config.page_load_timeout)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception:
            pass
        driver.get(url)
        time.sleep(max(0.0, config.wait_seconds))
        return driver.page_source or ""
    except Exception as exc:
        log(f"Selenium 渲染失败: {type(exc).__name__}: {exc}")
        return ""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
