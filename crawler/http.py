# -*- coding: utf-8 -*-
"""带反爬策略的 HTTP 客户端。

反爬措施（都属于常规、克制的礼貌抓取）：
    * 每次请求轮换 User-Agent（fake_useragent 优先，回退内置池）；
    * 浏览器化请求头（Accept / Accept-Language / Referer / Upgrade-Insecure-Requests）；
    * 指数退避 + 抖动的重试，403/429/5xx 时换 UA 重试；
    * 同站点请求限速（crawl.delay_seconds）；
    * 复用 Session 保持 Cookie；
    * 可选代理（crawl.proxies）与 TLS 校验开关；
    * 正文过短（疑似 JS 渲染）时用 Selenium 兜底渲染一次。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

import requests

from pipeline.preprocess import host_of, visible_text_length
from settings import CrawlConfig

#: fake_useragent 不可用时的兜底 UA 池
_FALLBACK_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)

_RETRY_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int = 0
    text: str = ""
    final_url: str = ""
    from_selenium: bool = False
    attempts: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    @property
    def visible_chars(self) -> int:
        return visible_text_length(self.text)


class HttpClient:
    """串行抓取客户端（流水线是单进程顺序跑，不需要并发）。"""

    def __init__(self, config: CrawlConfig, *, log: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.log = log or (lambda _msg: None)
        self.session = requests.Session()
        self.session.trust_env = config.trust_env
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Connection": "keep-alive",
            }
        )
        self._ua_source = self._build_ua_source()
        self._last_request: dict[str, float] = {}
        self._page_cache: dict[str, FetchResult] = {}
        self._stats = {
            "requests": 0,
            "retries": 0,
            "selenium": 0,
            "errors": 0,
            "cache_hits": 0,
        }

    # ---------------------------------------------------------------- UA
    def _build_ua_source(self):
        if self.config.user_agent_pool:
            pool = list(self.config.user_agent_pool)
            return lambda: random.choice(pool)
        if self.config.prefer_fake_useragent:
            try:
                from fake_useragent import UserAgent

                ua = UserAgent()

                def pick() -> str:
                    try:
                        value = ua.random
                    except Exception:  # fake_useragent 偶发数据源失败
                        value = ""
                    return value or random.choice(_FALLBACK_USER_AGENTS)

                return pick
            except Exception:
                pass
        return lambda: random.choice(_FALLBACK_USER_AGENTS)

    def _throttle(self, url: str) -> None:
        delay = max(0.0, self.config.delay_seconds)
        if delay <= 0:
            return
        host = host_of(url)
        last = self._last_request.get(host, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, delay * 0.3))
        self._last_request[host] = time.monotonic()

    def _headers(self, referer: str, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": self._ua_source(),
            "Referer": referer or "https://www.baidu.com/",
        }
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _proxies(self) -> dict[str, str] | None:
        proxy = (self.config.proxies or "").strip()
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    @staticmethod
    def _decode(response: requests.Response, forced: str = "") -> str:
        encoding = forced or response.encoding
        # requests 对没有 charset 的 text/html 会默认 ISO-8859-1，这里改用探测结果
        if not encoding or encoding.lower() in ("iso-8859-1", "latin-1"):
            encoding = response.apparent_encoding or "utf-8"
        try:
            return response.content.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            return response.content.decode("utf-8", errors="replace")

    # ---------------------------------------------------------------- 抓取
    @staticmethod
    def _cache_key(
        url: str,
        *,
        encoding: str,
        use_selenium: bool,
        headers: Mapping[str, str] | None,
    ) -> str:
        extra = "&".join(f"{k}={headers[k]}" for k in sorted(headers or {}))
        return f"{url}\x00{encoding}\x00{use_selenium}\x00{extra}"

    def clear_cache(self) -> None:
        self._page_cache.clear()

    def get(
        self,
        url: str,
        *,
        referer: str = "",
        headers: Mapping[str, str] | None = None,
        use_selenium: bool = False,
        encoding: str = "",
        use_cache: bool | None = None,
    ) -> FetchResult:
        """抓一页。网络层失败不抛异常，返回带 error 的 FetchResult，由调用方决定降级。

        use_cache=None 时跟随 crawl.page_cache：同一次运行内相同页面只真正请求一次
        （规则解析、条目抽取、兜底路径可能重复取同一列表页）。
        """
        if not url:
            return FetchResult(url=url, error="url 为空")

        effective_cache = self.config.page_cache if use_cache is None else use_cache
        key = self._cache_key(
            url, encoding=encoding, use_selenium=use_selenium, headers=headers
        )
        if effective_cache and key in self._page_cache:
            cached = self._page_cache[key]
            self._stats["cache_hits"] += 1
            self.log(f"页面缓存命中（本次运行内已抓过）: {url}")
            return cached

        attempts = 0
        last_error = ""
        result = FetchResult(url=url)

        for attempt in range(self.config.max_retries + 1):
            attempts += 1
            if attempt:
                self._stats["retries"] += 1
            self._throttle(url)
            self._stats["requests"] += 1
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(referer, headers),
                    timeout=self.config.timeout_seconds,
                    verify=self.config.verify_tls,
                    proxies=self._proxies(),
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.config.max_retries:
                    self._backoff(attempt, reason=type(exc).__name__)
                    continue
                break

            text = self._decode(response, encoding)
            result = FetchResult(
                url=url,
                status=response.status_code,
                text=text,
                final_url=str(response.url),
                attempts=attempts,
            )
            if response.status_code in _RETRY_STATUS and attempt < self.config.max_retries:
                last_error = f"HTTP {response.status_code}"
                self.log(
                    f"抓取返回 HTTP {response.status_code}，{self._backoff(attempt, reason='status')}"
                    f"后重试: {url}"
                )
                continue
            if response.status_code >= 400:
                result.error = f"HTTP {response.status_code}"
                self._stats["errors"] += 1
                self.log(f"抓取失败 HTTP {response.status_code}: {url}")
                return result
            break

        if result.error or not result.text:
            if last_error:
                result.error = last_error
            self._stats["errors"] += 1
            self.log(f"抓取最终失败（{attempts} 次尝试）: {url} | {result.error}")
            if not result.text:
                return result

        # 疑似 JS 渲染空壳 -> Selenium 兜底
        if use_selenium or (
            self.config.selenium_fallback.enabled
            and visible_text_length(result.text) < self.config.content_min_chars
        ):
            fallback = self._selenium_fallback(url)
            if fallback and len(fallback) > len(result.text) * 0.5:
                result.text = fallback
                result.from_selenium = True
                self._stats["selenium"] += 1

        if effective_cache:
            self._page_cache[key] = result
        return result

    def _backoff(self, attempt: int, *, reason: str = "") -> str:
        base = max(0.0, self.config.retry_backoff_seconds)
        delay = base * (2**attempt) + random.uniform(0, max(0.2, base * 0.5))
        self.log(f"退避 {delay:.1f}s 后重试（第 {attempt + 1} 次，原因 {reason or 'unknown'}）")
        time.sleep(delay)
        return f"{delay:.1f}s"

    def _selenium_fallback(self, url: str) -> str:
        config = self.config.selenium_fallback
        if not config.enabled:
            return ""
        try:
            from crawler.selenium_fallback import fetch_html
        except Exception as exc:  # pragma: no cover
            self.log(f"Selenium 兜底不可用: {exc}")
            return ""
        self.log(f"正文过短，尝试 Selenium 渲染: {url}")
        html = fetch_html(url, config, log=self.log, user_agent=self._ua_source())
        if not html:
            self.log(f"Selenium 渲染未取到内容: {url}")
        return html

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
