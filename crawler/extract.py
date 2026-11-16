# -*- coding: utf-8 -*-
"""列表页抽取：把 HTML 适配成 RawItem 列表（纯 XPath 驱动，选择器全部来自 YAML）。"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from lxml import etree

from models import RawItem

_WS_RE = re.compile(r"\s+")

SELECTOR_KEYS = (
    "list_item",
    "title",
    "url",
    "picurl",
    "summary",
    "published_at",
    "detail_content",
)

#: 图片懒加载站点的常见属性，配置里没写 picurl 时兜底尝试
_LAZY_IMAGE_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-url", "src")


class ExtractError(ValueError):
    """选择器配置错误。"""


def parse_selectors(
    raw: Mapping[str, Any], source_name: str, *, require_list_item: bool = True
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ExtractError(f"源 {source_name}: selectors 必须是字典")
    selectors: dict[str, str] = {}
    for key in SELECTOR_KEYS:
        value = raw.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ExtractError(f"源 {source_name}: selectors.{key} 必须是字符串")
        selectors[key] = value.strip()
    if require_list_item and not selectors["list_item"]:
        raise ExtractError(f"源 {source_name}: selectors.list_item 不能为空")
    return selectors


def _tidy(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def xpath_text(node: etree._Element, expr: str) -> str:
    """公开版本：取 XPath 结果里的文本（适配器校验与启发式兜底会用到）。"""
    return _xpath_text(node, expr)


def xpath_first_value(node: etree._Element, expr: str) -> str:
    """公开版本：取 XPath 结果里的第一个非空值（适合 href/src）。"""
    return _xpath_first_value(node, expr)


def _xpath_text(node: etree._Element, expr: str) -> str:
    """取 XPath 结果里的文本：字符串直接拼，元素取全部文本节点。"""
    if not expr:
        return ""
    try:
        results = node.xpath(expr)
    except etree.XPathError as exc:
        raise ExtractError(f"XPath 非法: {expr!r} ({exc})") from exc
    if not isinstance(results, list):
        results = [results]
    pieces: list[str] = []
    for result in results:
        if isinstance(result, str):
            pieces.append(result)
        elif isinstance(result, etree._Element):
            pieces.append("".join(result.itertext()))
        elif hasattr(result, "text_content"):
            pieces.append(result.text_content())
    return _tidy(" ".join(piece for piece in pieces if piece))


def _xpath_first_value(node: etree._Element, expr: str) -> str:
    if not expr:
        return ""
    try:
        results = node.xpath(expr)
    except etree.XPathError as exc:
        raise ExtractError(f"XPath 非法: {expr!r} ({exc})") from exc
    if not isinstance(results, list):
        results = [results]
    for result in results:
        if isinstance(result, str):
            value = result.strip()
        elif isinstance(result, etree._Element):
            value = (result.get("href") or result.get("src") or "").strip()
        elif hasattr(result, "get"):
            value = (result.get("href") or result.get("src") or "").strip()
        else:
            value = ""
        if value:
            return value
    return ""


def _absolute(base_url: str, link: str) -> str:
    link = (link or "").strip()
    if not link or link.startswith(("javascript:", "data:", "#", "mailto:")):
        return ""
    if link.startswith(("http://", "https://")):
        return link
    return urljoin(base_url, link)


def _lazy_image(item: etree._Element) -> str:
    for attr in _LAZY_IMAGE_ATTRS:
        values = item.xpath(f".//img/@{attr}")
        for value in values:
            value = (value or "").strip()
            if value and not value.startswith("data:"):
                return value
    return ""


def _is_nav_like(page_url: str, item_url: str) -> bool:
    """判断条目链接是不是「导航/栏目/分页」而不是具体文章。

    依据（都与站点无关）：
        * 指向站点根目录；
        * 与列表页路径相同（分页链接）；
        * 与列表页同父目录的兄弟栏目链接。
    """
    try:
        page = urlsplit(page_url)
        item = urlsplit(item_url)
    except Exception:
        return False
    if not item.netloc or item.netloc.lower() != page.netloc.lower():
        return False
    page_path = (page.path or "/").rstrip("/") or "/"
    item_path = (item.path or "/").rstrip("/") or "/"
    if item_path in ("", "/") or item_path == page_path:
        return True
    page_parent = page_path.rsplit("/", 1)[0] or "/"
    if item_path.rsplit("/", 1)[0] != page_parent:
        return False
    # 同目录下的兄弟链接：若两边都是具体页面文件（带扩展名），那是文章而不是导航
    item_last = item_path.rsplit("/", 1)[-1]
    page_last = page_path.rsplit("/", 1)[-1]
    return not ("." in item_last and "." in page_last)


def extract_list(
    html: str,
    selectors: Mapping[str, str],
    *,
    base_url: str,
    source: str = "",
    max_items: int = 10,
    log: Callable[[str], None] | None = None,
) -> list[RawItem]:
    """按配置的选择器从列表页抽条目。抽不到就返回空列表（调用方记日志）。"""
    log = log or (lambda _msg: None)
    if not html:
        return []
    try:
        tree = etree.HTML(html)
    except Exception as exc:
        log(f"列表页解析失败（source={source}）: {type(exc).__name__}: {exc}")
        return []
    if tree is None:
        log(f"列表页解析为空（source={source}）")
        return []

    try:
        nodes = tree.xpath(selectors["list_item"])
    except etree.XPathError as exc:
        raise ExtractError(f"selectors.list_item 非法: {selectors['list_item']!r} ({exc})") from exc
    if not isinstance(nodes, list):
        nodes = [nodes]

    items: list[RawItem] = []
    skipped_nav = 0
    page_url = base_url
    for node in nodes:
        if not isinstance(node, etree._Element):
            continue
        title = _xpath_text(node, selectors.get("title", ""))
        link = _xpath_first_value(node, selectors.get("url", "")) or _xpath_first_value(
            node, ".//a/@href"
        )
        link = _absolute(base_url, link)
        if not title and not link:
            continue
        if link and _is_nav_like(page_url, link):
            skipped_nav += 1
            continue
        picurl = _absolute(base_url, _xpath_first_value(node, selectors.get("picurl", "")))
        if not picurl:
            picurl = _absolute(base_url, _lazy_image(node))
        items.append(
            RawItem(
                url=link,
                title=title,
                description=_xpath_text(node, selectors.get("summary", "")),
                picurl=picurl,
                source=source,
                meta={
                    "published_at": _xpath_text(node, selectors.get("published_at", "")),
                    "list_url": base_url,
                },
            )
        )
        if len(items) >= max_items:
            break
    if skipped_nav:
        log(f"列表页跳过 {skipped_nav} 条导航/栏目/分页链接（source={source or '-'}）")
    return items


def extract_container_text(html: str, xpath: str) -> str:
    """按详情页正文容器的 XPath 抽纯文本（后续仍会走统一清洗）。"""
    if not html or not xpath:
        return ""
    try:
        tree = etree.HTML(html)
    except Exception:
        return ""
    if tree is None:
        return ""
    try:
        nodes = tree.xpath(xpath)
    except etree.XPathError:
        return ""
    if not isinstance(nodes, list):
        nodes = [nodes]
    for node in nodes:
        if isinstance(node, etree._Element):
            text = _tidy("\n".join(node.itertext()))
            if text:
                return text
        elif isinstance(node, str) and node.strip():
            return _tidy(node)
    return ""
