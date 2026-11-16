# -*- coding: utf-8 -*-
"""爬虫层：只负责「拿全 + 适配成统一模型」，不做任何筛选判断。"""

from __future__ import annotations

from crawler.http import FetchResult, HttpClient
from crawler.sources import SourceConfig, SourceCrawler, load_sources

__all__ = ["FetchResult", "HttpClient", "SourceConfig", "SourceCrawler", "load_sources"]
