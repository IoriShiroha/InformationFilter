# -*- coding: utf-8 -*-
"""新增源类型：RSS / Atom 与 JSON 接口。

两者都不需要 XPath 规则：RSS/Atom 按标准字段解析，JSON 接口用简化的路径表达式
（`data.items[*]`、`data.list[0]`、`result.news`）定位列表与字段。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin

from lxml import etree

from models import RawItem

_ATOM = "http://www.w3.org/2005/Atom"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_MEDIA = "http://search.yahoo.com/mrss/"
_NS = {"atom": _ATOM, "content": _CONTENT, "media": _MEDIA}

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SEGMENT_RE = re.compile(r"^([^\[\]]*)((?:\[[^\]]*\])*)$")


class FeedError(ValueError):
    """RSS / JSON 结构无法解析。"""


def _tidy(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _strip_tags(text: str) -> str:
    return _tidy(_TAG_RE.sub(" ", text or ""))


def _absolutize(base_url: str, value: str) -> str:
    value = (value or "").strip()
    if not value or value.startswith(("javascript:", "data:", "mailto:", "#")):
        return ""
    return urljoin(base_url, value)


# ---------------------------------------------------------------------------
# RSS / Atom
# ---------------------------------------------------------------------------


def _first_text(node: etree._Element, expr: str) -> str:
    try:
        values = node.xpath(expr, namespaces=_NS)
    except etree.XPathError:
        return ""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, etree._Element):
            text = "".join(value.itertext()).strip()
            if text:
                return text
    return ""


def parse_feed(xml_text: str, *, source: str, base_url: str) -> list[RawItem]:
    """解析 RSS 2.0 或 Atom；失败抛 FeedError。"""
    if not xml_text or not xml_text.strip():
        raise FeedError("订阅内容为空")
    parser = etree.XMLParser(recover=True, huge_tree=True)
    try:
        root = etree.fromstring(xml_text.encode("utf-8", "replace"), parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise FeedError(f"XML 解析失败：{exc}") from exc
    if root is None:
        raise FeedError("XML 解析结果为空")

    nodes = root.xpath("//item")
    is_atom = False
    if not nodes:
        nodes = root.xpath("//atom:entry", namespaces=_NS)
        is_atom = bool(nodes)
    if not nodes:
        raise FeedError("没有找到 RSS item 或 Atom entry")

    items: list[RawItem] = []
    for node in nodes:
        title = _strip_tags(_first_text(node, "title"))
        if is_atom:
            link = _first_text(node, "atom:link[@rel='alternate']/@href") or _first_text(
                node, "atom:link/@href"
            )
            description = _strip_tags(
                _first_text(node, "atom:summary") or _first_text(node, "atom:content")
            )
            full_content = _strip_tags(_first_text(node, "atom:content"))
            content = full_content or description
            published = _first_text(node, "atom:updated") or _first_text(node, "atom:published")
        else:
            link = _first_text(node, "link") or _first_text(node, "guid")
            description = _strip_tags(_first_text(node, "description"))
            full_content = _strip_tags(_first_text(node, "content:encoded"))
            content = full_content or description
            published = _first_text(node, "pubDate")
        url = _absolutize(base_url, link)
        if not title or not url:
            continue
        picurl = _absolutize(
            base_url,
            _first_text(node, "enclosure/@url") or _first_text(node, "media:content/@url"),
        )
        items.append(
            RawItem(
                url=url,
                title=title,
                description=description,
                picurl=picurl,
                content_raw=content or description,
                source=source,
                meta={
                    "published_at": published,
                    "via": "rss",
                    # 没有 content:encoded / atom:content 时，正文只是摘要，仍需要抓详情页
                    "need_detail": not bool(full_content),
                },
            )
        )
    return items


# ---------------------------------------------------------------------------
# JSON 接口（简化路径表达式）
# ---------------------------------------------------------------------------


def _split_path(path: str) -> list[str]:
    cleaned = (path or "").strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        return []
    return [segment for segment in cleaned.split(".") if segment]


def _iter_segment(nodes: Iterable[Any], segment: str) -> list[Any]:
    match = _SEGMENT_RE.match(segment)
    if not match:
        return []
    name, selectors = match.group(1), match.group(2)
    selector_list = re.findall(r"\[([^\]]*)\]", selectors)
    current = list(nodes)
    if name:
        current = [
            node.get(name) for node in current if isinstance(node, Mapping) and name in node
        ]
    for selector in selector_list:
        selector = selector.strip()
        if selector in ("*", ""):
            expanded: list[Any] = []
            for node in current:
                if isinstance(node, list):
                    expanded.extend(node)
                elif isinstance(node, Mapping):
                    expanded.append(node)
            current = expanded
        else:
            try:
                index = int(selector)
            except ValueError:
                return []
            indexed: list[Any] = []
            for node in current:
                if isinstance(node, list) and -len(node) <= index < len(node):
                    indexed.append(node[index])
            current = indexed
    return current


def json_path_all(data: Any, path: str) -> list[Any]:
    """`data.items[*]` / `result.list[0]` / `items` 这类简化 JSON 路径。"""
    nodes: list[Any] = [data]
    for segment in _split_path(path):
        nodes = _iter_segment(nodes, segment)
        if not nodes:
            return []
    return nodes


def _json_text(node: Any, path: str, default: str = "") -> str:
    if not path:
        return default
    values = json_path_all(node, path)
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, Mapping):
            for key in ("text", "name", "url", "href"):
                inner = value.get(key)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return default


_JSON_DEFAULT_FIELDS = {
    "title": "title",
    "url": "url",
    "picurl": "picurl",
    "summary": "summary",
    "published_at": "published_at",
    "content": "content",
}


def parse_json_items(
    payload: Any,
    mapping: Mapping[str, str],
    *,
    source: str,
    base_url: str,
) -> list[RawItem]:
    """按配置的字段路径从 JSON 响应里抽条目。"""
    items_path = (mapping.get("items") or "").strip()
    if not items_path:
        raise FeedError("JSON 源缺少 json.items 路径（例如 data.items[*]）")
    nodes = json_path_all(payload, items_path)
    if not nodes:
        raise FeedError(f"JSON 路径 {items_path!r} 没有匹配到列表节点")

    def field(name: str) -> str:
        return (mapping.get(name) or _JSON_DEFAULT_FIELDS[name]).strip()

    items: list[RawItem] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        title = _json_text(node, field("title"))
        url = _absolutize(base_url, _json_text(node, field("url")))
        if not title or not url:
            continue
        description = _json_text(node, field("summary"))
        content = _json_text(node, field("content"))
        items.append(
            RawItem(
                url=url,
                title=title,
                description=description,
                picurl=_absolutize(base_url, _json_text(node, field("picurl"))),
                content_raw=content or description,
                source=source,
                meta={
                    "published_at": _json_text(node, field("published_at")),
                    "via": "json_api",
                    # 接口没给正文时只有摘要，仍需要抓详情页补齐
                    "need_detail": not bool(content),
                },
            )
        )
    return items


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text or "")
    except ValueError as exc:
        raise FeedError(f"接口返回不是合法 JSON：{exc}") from exc
