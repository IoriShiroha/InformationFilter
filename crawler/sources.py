# -*- coding: utf-8 -*-
"""站点源：配置加载 + 列表页/详情页抓取。

一份 config/sources.yaml 描述多个源；每个源由「列表页 XPath + 详情页正文 XPath」组成，
换站点/换主题只改 YAML，不改代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

try:  # 缺依赖时给出可读提示
    _ = yaml.safe_load
except AttributeError:  # pragma: no cover
    raise ModuleNotFoundError(
        "PyYAML 版本不可用：请执行 python -m pip install -r requirements.txt"
    ) from None

from crawler.css import CssError, css_to_relative_xpath, css_to_xpath
from crawler.extract import ExtractError, extract_container_text, extract_list, parse_selectors
from crawler.feed import FeedError, parse_feed, parse_json_items, parse_json_text
from crawler.http import HttpClient
from models import RawItem
from settings import ConfigError, CrawlConfig

SUPPORTED_SOURCE_TYPES = ("html_list", "json_api", "rss")


@dataclass(slots=True)
class SourceConfig:
    name: str
    url: str = ""
    enabled: bool = False
    type: str = "html_list"
    encoding: str = ""
    referer: str = ""
    use_selenium: bool = False
    fetch_detail: bool = True
    max_items: int = 10
    #: 只填 URL，让 AI 生成并缓存解析规则（仅 html_list）
    auto_selectors: bool = False
    #: 选择器语法：xpath（默认）或 css（加载时自动转 XPath）
    selector_kind: str = "xpath"
    headers: dict[str, str] = field(default_factory=dict)
    selectors: dict[str, str] = field(default_factory=dict)
    #: json_api 源的字段路径映射（items/title/url/...）
    json_paths: dict[str, str] = field(default_factory=dict)

    def with_selectors(self, selectors: dict[str, str]) -> "SourceConfig":
        """返回填好选择器的新源配置（不改动原对象，便于缓存/日志对比）。"""
        return SourceConfig(
            name=self.name,
            url=self.url,
            enabled=self.enabled,
            type=self.type,
            encoding=self.encoding,
            referer=self.referer,
            use_selenium=self.use_selenium,
            fetch_detail=self.fetch_detail,
            max_items=self.max_items,
            auto_selectors=self.auto_selectors,
            selector_kind=self.selector_kind,
            headers=dict(self.headers),
            selectors=dict(selectors),
            json_paths=dict(self.json_paths),
        )

    @property
    def has_list_selector(self) -> bool:
        return bool((self.selectors or {}).get("list_item"))

    @property
    def needs_rules(self) -> bool:
        """是否依赖解析规则（只有 html_list 需要）。"""
        return self.type == "html_list"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"源配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"源配置 YAML 解析失败 {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"源配置根节点应为字典: {path}")
    return {str(k): v for k, v in data.items()}


def _as_headers(raw: Any, name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"源 {name}: headers 必须是字典")
    return {str(k): str(v) for k, v in raw.items()}


def load_sources(
    path: str | Path,
    *,
    log: Callable[[str], None] | None = None,
) -> list[SourceConfig]:
    """加载源配置。

    容错策略（一个坏源不该拖垮整条流水线）：
        * 文件级错误（YAML 语法、根节点/sources 结构）才抛 ConfigError；
        * 单个源不合法（缺 name、重复、已启用却没 url、既没选择器也没开自动适配、
          type 不支持、headers 不是字典）→ 打警告并跳过该源，其它源照常加载；
        * 未启用的源允许没有 url / 没有选择器（通常是模板占位）。
    """
    emit = log or (lambda _msg: None)
    target = Path(path)
    data = _read_yaml(target)
    defaults_raw = data.get("defaults") or {}
    if not isinstance(defaults_raw, Mapping):
        raise ConfigError(f"源配置 defaults 必须是字典: {target}")
    raw_sources = data.get("sources")
    if raw_sources is None:
        raise ConfigError(f"源配置缺少 sources 列表: {target}")
    if not isinstance(raw_sources, list):
        raise ConfigError(f"源配置 sources 必须是列表: {target}")

    sources: list[SourceConfig] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, Mapping):
            emit(f"源配置 sources[{index}] 不是字典，已跳过")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            emit(f"源配置 sources[{index}] 缺少 name，已跳过")
            continue
        if name in seen_names:
            emit(f"源名称重复 {name}，已跳过后面的重复项")
            continue
        seen_names.add(name)

        merged = {**defaults_raw, **raw}
        source_type = str(merged.get("type") or "html_list").strip()
        if source_type not in SUPPORTED_SOURCE_TYPES:
            emit(f"源 {name}: type 只支持 {list(SUPPORTED_SOURCE_TYPES)}，实际 {source_type!r}，已跳过")
            continue

        url = str(merged.get("url") or "").strip()
        enabled = bool(merged.get("enabled", False))
        if enabled and not url.startswith(("http://", "https://")):
            emit(
                f"源 {name}: 已启用但 url 不是可用的 http(s) 地址（实际 {url!r}），"
                "已跳过；请填上列表页地址，或把它改成 enabled: false"
            )
            continue

        max_items = merged.get("max_items", 10)
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            emit(f"源 {name}: max_items 非法（{max_items!r}），已按 10 处理")
            max_items = 10

        auto_selectors = bool(merged.get("auto_selectors", False))
        selector_kind = str(merged.get("selector_type") or "xpath").strip().lower()
        if selector_kind not in ("xpath", "css"):
            emit(f"源 {name}: selector_type 只支持 xpath / css，实际 {selector_kind!r}，按 xpath 处理")
            selector_kind = "xpath"

        json_paths: dict[str, str] = {}
        if source_type == "json_api":
            raw_json = merged.get("json") or {}
            if not isinstance(raw_json, Mapping):
                emit(f"源 {name}: json 必须是字典，已跳过")
                continue
            json_paths = {str(k): str(v) for k, v in raw_json.items() if isinstance(v, str)}
            if enabled and not json_paths.get("items"):
                emit(f"源 {name}: json_api 源缺少 json.items 路径（例如 data.items[*]），已跳过")
                continue
            selectors = {}
        else:
            try:
                selectors = parse_selectors(
                    merged.get("selectors") or {},
                    name,
                    require_list_item=(enabled and auto_selectors is False and source_type == "html_list"),
                )
            except ExtractError as exc:
                if enabled:
                    emit(f"源 {name}: {exc}，已跳过；请手写 selectors 或设置 auto_selectors: true")
                    continue
                emit(f"源 {name}（未启用）选择器不完整：{exc}，按空选择器处理")
                selectors = {}

            if selector_kind == "css" and selectors:
                try:
                    selectors = _css_selectors(selectors)
                except CssError as exc:
                    emit(f"源 {name}: CSS 选择器转换失败：{exc}，已跳过")
                    continue

        if enabled and source_type == "html_list" and not auto_selectors and not selectors.get("list_item"):
            emit(
                f"源 {name}: 已启用但没有 list_item 选择器，也没有设置 auto_selectors: true，"
                "已跳过；只填 URL 的用法需要 auto_selectors: true"
            )
            continue

        try:
            headers = _as_headers(merged.get("headers"), name)
        except ConfigError as exc:
            emit(f"{exc}，已按空 headers 处理")
            headers = {}

        sources.append(
            SourceConfig(
                name=name,
                url=url,
                enabled=enabled,
                type=source_type,
                encoding=str(merged.get("encoding") or "").strip(),
                referer=str(merged.get("referer") or "").strip(),
                use_selenium=bool(merged.get("use_selenium", False)),
                fetch_detail=bool(merged.get("fetch_detail", True)),
                max_items=max_items,
                auto_selectors=auto_selectors,
                selector_kind=selector_kind,
                headers=headers,
                selectors=selectors,
                json_paths=json_paths,
            )
        )
    return sources


def _css_selectors(selectors: Mapping[str, str]) -> dict[str, str]:
    """把 CSS 选择器统一转成 XPath；字段选择器转成相对条目的 `.//` 形式。"""

    def convert(key: str, value: str) -> str:
        if not value:
            return ""
        if value.startswith("css:"):
            value = value[4:]
        if key in ("list_item", "detail_content"):
            return css_to_xpath(value)
        return css_to_relative_xpath(value)

    return {key: convert(key, value) for key, value in selectors.items()}


class SourceCrawler:
    """单个源的抓取器。

    拆成两步，便于「先粗筛再抓详情」：
        fetch_list()      列表页 -> 条目（不含详情正文）
        attach_details()  仅对传进来的条目抓详情页
        crawl()           两步连起来（兼容旧用法）
    """

    def __init__(
        self,
        source: SourceConfig,
        http: HttpClient,
        config: CrawlConfig,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.source = source
        self.http = http
        self.config = config
        self.log = log or (lambda _msg: None)

    # ---------------------------------------------------------------- 列表
    def fetch_list(self) -> list[RawItem]:
        source = self.source
        if not source.enabled:
            self.log(f"源 {source.name} 未启用，跳过")
            return []
        if not source.url:
            self.log(f"源 {source.name} 没有配置 url，跳过")
            return []
        if source.type == "rss":
            return self._fetch_rss()
        if source.type == "json_api":
            return self._fetch_json_api()
        if not source.has_list_selector:
            self.log(
                f"源 {source.name} 没有可用的 list_item 选择器（auto_selectors="
                f"{source.auto_selectors}），跳过；请开启 auto_selectors 或手写 selectors"
            )
            return []

        result = self.http.get(
            source.url,
            referer=source.referer,
            headers=source.headers,
            use_selenium=source.use_selenium,
            encoding=source.encoding,
        )
        if not result.text:
            self.log(f"源 {source.name} 列表页抓取失败: {result.error or '空响应'}")
            return []

        limit = min(source.max_items, self.config.max_items_per_source)
        try:
            items = extract_list(
                result.text,
                source.selectors,
                base_url=result.final_url or source.url,
                source=source.name,
                max_items=limit,
                log=self.log,
            )
        except ExtractError as exc:
            self.log(f"源 {source.name} 抽取失败: {exc}")
            return []

        if not items:
            self.log(
                f"源 {source.name} 未抽到条目（list_item={source.selectors.get('list_item')!r}），"
                "请检查选择器或页面结构是否变化"
            )
            return []
        self.log(f"源 {source.name} 列表页抽到 {len(items)} 条（上限 {limit}）")
        return items

    def _fetch_rss(self) -> list[RawItem]:
        source = self.source
        result = self.http.get(
            source.url,
            referer=source.referer,
            headers=source.headers,
            encoding=source.encoding,
        )
        if not result.text:
            self.log(f"源 {source.name} RSS 抓取失败: {result.error or '空响应'}")
            return []
        try:
            items = parse_feed(
                result.text, source=source.name, base_url=result.final_url or source.url
            )
        except FeedError as exc:
            self.log(f"源 {source.name} RSS 解析失败: {exc}")
            return []
        limit = min(source.max_items, self.config.max_items_per_source)
        items = items[:limit]
        self.log(f"源 {source.name} RSS 解析到 {len(items)} 条")
        return items

    def _fetch_json_api(self) -> list[RawItem]:
        source = self.source
        result = self.http.get(
            source.url,
            referer=source.referer,
            headers=source.headers,
            encoding=source.encoding,
        )
        if not result.text:
            self.log(f"源 {source.name} 接口抓取失败: {result.error or '空响应'}")
            return []
        try:
            payload = parse_json_text(result.text)
            items = parse_json_items(
                payload,
                source.json_paths,
                source=source.name,
                base_url=result.final_url or source.url,
            )
        except FeedError as exc:
            self.log(f"源 {source.name} 接口解析失败: {exc}")
            return []
        limit = min(source.max_items, self.config.max_items_per_source)
        items = items[:limit]
        self.log(f"源 {source.name} 接口解析到 {len(items)} 条")
        return items

    # ---------------------------------------------------------------- 详情
    def attach_details(self, items: list[RawItem]) -> list[RawItem]:
        if not (self.source.fetch_detail and self.config.fetch_detail):
            return items
        for item in items:
            if item.content_raw and not item.meta.get("need_detail"):
                continue  # RSS/JSON 自带正文，除非显式要求补详情
            self._attach_detail(item)
        return items

    def crawl(self) -> list[RawItem]:
        return self.attach_details(self.fetch_list())

    def _attach_detail(self, item: RawItem) -> None:
        source = self.source
        if not item.url:
            return
        result = self.http.get(
            item.url,
            referer=source.referer or source.url,
            headers=source.headers,
            use_selenium=source.use_selenium,
            encoding=source.encoding,
        )
        if not result.text:
            item.meta["detail_error"] = result.error or "空响应"
            self.log(f"详情页抓取失败，仅用列表信息: {item.url} | {result.error}")
            return
        item.html = result.text
        item.meta["detail_fetched"] = True
        item.meta["detail_from_selenium"] = result.from_selenium
        content_xpath = source.selectors.get("detail_content", "")
        if content_xpath:
            text = extract_container_text(result.text, content_xpath)
            if text:
                item.content_raw = text
        else:
            # 抓到了详情页 HTML：正文以详情页为准，清掉列表/接口带来的短摘要
            item.content_raw = ""
