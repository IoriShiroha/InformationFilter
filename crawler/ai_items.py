# -*- coding: utf-8 -*-
"""第二层兜底：选择器都抽不到条目时，让 AI 直接读页面、输出条目列表（JSON）。

适用场景：
    * 页面结构千变万化，规则生成多轮都失败；
    * 页面是半 JS 渲染，静态 HTML 里的结构线索不稳。

代价：这一层是「每次运行一条 LLM 调用」，所以只在选择器失败时才走；
拿到条目后，后续去重/筛选/推送与正常链路完全一致。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from crawler.adapt import build_page_context
from crawler.http import HttpClient
from crawler.sources import SourceConfig
from models import RawItem
from pipeline.llm_client import DeepSeekClient, LLMError
from settings import AutoSourceConfig

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"
DEFAULT_ITEM_TEMPLATE = _TEMPLATE_DIR / "item_extraction_prompt.md"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_TEMPLATE_KEYS = ("page_url", "page_text", "page_html", "max_items", "min_items", "previous_error")


class ItemExtractError(RuntimeError):
    """AI 条目抽取失败（不可用或结果不可用）。"""


class ItemTemplateError(ValueError):
    """条目抽取 Prompt 模板不合法。"""


def load_item_template(path: str | Path) -> str:
    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    if not target.is_file():
        raise ItemTemplateError(f"条目抽取 Prompt 模板不存在: {target}")
    text = target.read_text(encoding="utf-8")
    found = set(_PLACEHOLDER_RE.findall(text))
    unknown = sorted(found - set(_TEMPLATE_KEYS))
    if unknown:
        raise ItemTemplateError(
            f"条目抽取模板含未知占位符 {unknown}；可用：{list(_TEMPLATE_KEYS)}"
        )
    missing = [key for key in _TEMPLATE_KEYS if key not in found]
    if missing:
        raise ItemTemplateError(f"条目抽取模板缺少必需占位符 {missing}")
    return text


class AiItemExtractor:
    """页面 -> 条目列表（AI 直接抽取，不落规则缓存）。"""

    def __init__(
        self,
        client: DeepSeekClient,
        config: AutoSourceConfig,
        *,
        prompt_template: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.log = log or (lambda _msg: None)
        self.template = load_item_template(
            prompt_template or getattr(config, "item_prompt_template", str(DEFAULT_ITEM_TEMPLATE))
        )

    # ---------------------------------------------------------------- 渲染
    def _render(
        self,
        *,
        page_url: str,
        page_text: str,
        page_html: str,
        max_items: int,
        min_items: int,
        previous_error: str = "",
    ) -> str:
        context = {
            "page_url": page_url,
            "page_text": page_text,
            "page_html": page_html,
            "max_items": str(max_items),
            "min_items": str(min_items),
            "previous_error": previous_error.strip() or "（无，这是第一次尝试）",
        }
        return _PLACEHOLDER_RE.sub(lambda match: context[match.group(1)], self.template)

    # ---------------------------------------------------------------- 抽取
    def extract(self, source: SourceConfig, http: HttpClient) -> list[RawItem]:
        if not self.client.available:
            raise ItemExtractError("未配置 DeepSeek 密钥，无法使用 AI 条目抽取兜底")

        result = http.get(
            source.url,
            referer=source.referer,
            headers=source.headers,
            use_selenium=source.use_selenium,
            encoding=source.encoding,
        )
        if not result.text:
            raise ItemExtractError(
                f"列表页抓取失败，无法做 AI 条目抽取：{result.error or '空响应'}"
            )

        base_url = result.final_url or source.url
        context = build_page_context(result.text, max_chars=self.config.html_max_chars)
        max_items = min(max(1, source.max_items), max(1, self.config.item_max_items))
        page_text = _visible_text(result.text)[: self.config.item_text_max_chars]

        prompt = self._render(
            page_url=base_url,
            page_text=page_text,
            page_html=context["excerpt"],
            max_items=max_items,
            min_items=max(1, self.config.min_items),
        )
        try:
            response = self.client.chat(
                [{"role": "user", "content": prompt}],
                label=f"AI 条目抽取 {source.name}",
            )
        except LLMError as exc:
            raise ItemExtractError(f"AI 条目抽取调用失败：{exc}") from exc

        items = self._parse_items(response.content, base_url=base_url, source=source)
        if not items:
            raise ItemExtractError(
                f"AI 条目抽取没有返回可用条目（原始返回：{response.content[:160]!r}）"
            )
        self.log(f"源 {source.name} AI 条目抽取成功：{len(items)} 条")
        return items

    # ---------------------------------------------------------------- 解析
    def _parse_items(self, content: str, *, base_url: str, source: SourceConfig) -> list[RawItem]:
        text = (content or "").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ItemExtractError(
                f"AI 条目抽取返回不是严格 JSON：{exc.msg}（位置 {exc.lineno}:{exc.colno}）"
            ) from exc

        raw_items: Any
        if isinstance(data, Mapping):
            raw_items = data.get("items")
        elif isinstance(data, list):
            raw_items = data
        else:
            raw_items = None
        if not isinstance(raw_items, list):
            raise ItemExtractError("AI 条目抽取返回缺少 items 数组")

        seen: set[str] = set()
        items: list[RawItem] = []
        invalid = 0
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                invalid += 1
                continue
            title = str(raw.get("title") or "").strip()
            url = _absolutize(base_url, str(raw.get("url") or ""))
            if len(title) < 2 or not url or url in seen:
                invalid += 1
                continue
            seen.add(url)
            picurl = _absolutize(base_url, str(raw.get("picurl") or ""))
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    description=str(raw.get("description") or "").strip(),
                    picurl=picurl,
                    source=source.name,
                    meta={"via": "ai_items"},
                )
            )
            if len(items) >= self.config.item_max_items:
                break
        if invalid:
            self.log(f"源 {source.name} AI 条目抽取忽略 {invalid} 条无效结果（缺标题/链接重复）")
        return items


def _absolutize(base_url: str, value: str) -> str:
    value = (value or "").strip()
    if not value or value.startswith(("javascript:", "data:", "mailto:", "#")):
        return ""
    return urljoin(base_url, value)


def _visible_text(html: str) -> str:
    from lxml import etree

    try:
        tree = etree.HTML(html)
    except Exception:
        return ""
    if tree is None:
        return ""
    for element in list(tree.iter()):
        if isinstance(element.tag, str) and element.tag.lower() in (
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
        ):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    text = "".join(tree.itertext())
    return re.sub(r"\s+", " ", text).strip()
