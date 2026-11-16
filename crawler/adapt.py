# -*- coding: utf-8 -*-
"""只给 URL 的源自动适配：AI 生成解析规则 → 确定性校验 → 落盘缓存复用。

设计取舍（为什么不是「每页都让 AI 解析」）：
    * 成本与延迟：每次运行逐页调 AI，条数一多就贵且慢；
    * 可复现：同一条内容两次运行可能得到不同字段，去重与幂等会失真；
    * 可回滚：规则落盘成 JSON 后，出问题能人工改、能对比、能删除重生成。

所以采用「生成一次 + 缓存复用」：AI 只负责**产出规则**，运行期用确定性 XPath 抽取；
页面结构指纹变化时才重新生成；AI 不可用或多次生成都不合格时，走启发式兜底。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from lxml import etree

from crawler.css import CssError, css_to_relative_xpath, css_to_xpath
from crawler.extract import (
    ExtractError,
    extract_container_text,
    extract_list,
    parse_selectors,
    xpath_first_value,
    xpath_text,
)
from crawler.http import HttpClient
from crawler.sources import SourceConfig
from models import RawItem, iso, simhash_to_hex, utcnow
from pipeline.llm_client import DeepSeekClient, LLMError
from pipeline.preprocess import normalize_url, simhash_text
from settings import AutoSourceConfig

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"
DEFAULT_TEMPLATE = _TEMPLATE_DIR / "source_adapter_prompt.md"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_TEMPLATE_KEYS = (
    "target",
    "page_url",
    "required_keys",
    "min_items",
    "page_html",
    "previous_error",
    "candidate_hints",
    "sample_item_html",
)

LIST_KEYS = ("list_item", "title", "url", "picurl", "summary", "published_at", "detail_content")

#: 启发式兜底的容器候选（按优先级）
CONTAINER_PATTERNS = (
    "//article",
    "//li[.//a[@href]]",
    "//div[contains(@class,'item')][.//a[@href]]",
    "//div[contains(@class,'card')][.//a[@href]]",
    "//div[contains(@class,'post')][.//a[@href]]",
    "//tr[.//a[@href]]",
    "//li",
)

TITLE_CANDIDATES = (
    ".//h1//text()",
    ".//h2//text()",
    ".//h3//text()",
    ".//h4//text()",
    ".//a//text()",
)
URL_CANDIDATES = (
    ".//h1/a/@href",
    ".//h2/a/@href",
    ".//h3/a/@href",
    ".//a[@href]/@href",
)
PIC_CANDIDATES = (
    ".//figure//img/@src",
    ".//img/@src",
    ".//img/@data-src",
)
SUMMARY_CANDIDATES = (
    ".//p//text()",
    ".//*[contains(@class,'summary')]//text()",
    ".//*[contains(@class,'desc')]//text()",
)
DETAIL_PATTERNS = (
    "//article",
    "//*[@itemprop='articleBody']",
    "//*[@id='content']",
    "//main",
    "//div[contains(@class,'article')]",
    "//div[contains(@class,'content')]",
)


class SourceAdaptError(RuntimeError):
    """自动适配失败（无法得到可用的解析规则）。"""


class TemplateError(ValueError):
    """规则生成 Prompt 模板不合法。"""


def load_template(path: str | Path) -> str:
    """读取并校验规则生成模板：占位符必须齐全且不允许未知占位符。"""
    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    if not target.is_file():
        raise TemplateError(f"规则生成 Prompt 模板不存在: {target}")
    text = target.read_text(encoding="utf-8")
    found = set(_PLACEHOLDER_RE.findall(text))
    unknown = sorted(found - set(_TEMPLATE_KEYS))
    if unknown:
        raise TemplateError(f"规则生成模板含未知占位符 {unknown}；可用：{list(_TEMPLATE_KEYS)}")
    missing = [key for key in _TEMPLATE_KEYS if key not in found]
    if missing:
        raise TemplateError(f"规则生成模板缺少必需占位符 {missing}")
    return text


# ---------------------------------------------------------------------------
# 解析规则档案（缓存）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceProfile:
    url: str
    url_norm: str
    selectors: dict[str, str] = field(default_factory=dict)
    referer: str = ""
    encoding: str = ""
    mode: str = "ai"  # ai | heuristic
    attempts: int = 0
    page_fingerprint: str = ""
    notes: str = ""
    generated_at: str = ""
    model: str = ""
    #: 规则自愈用：连续抽到 0 条的次数与最近一次原因
    zero_runs: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "url_norm": self.url_norm,
            "selectors": dict(self.selectors),
            "referer": self.referer,
            "encoding": self.encoding,
            "mode": self.mode,
            "attempts": self.attempts,
            "page_fingerprint": self.page_fingerprint,
            "notes": self.notes,
            "generated_at": self.generated_at,
            "model": self.model,
            "zero_runs": self.zero_runs,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceProfile":
        selectors_raw = data.get("selectors") or {}
        selectors = {
            str(k): str(v) for k, v in selectors_raw.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(selectors_raw, Mapping) else {}
        return cls(
            url=str(data.get("url") or ""),
            url_norm=str(data.get("url_norm") or ""),
            selectors=selectors,
            referer=str(data.get("referer") or ""),
            encoding=str(data.get("encoding") or ""),
            mode=str(data.get("mode") or "ai"),
            attempts=int(data.get("attempts") or 0),
            page_fingerprint=str(data.get("page_fingerprint") or ""),
            notes=str(data.get("notes") or ""),
            generated_at=str(data.get("generated_at") or ""),
            model=str(data.get("model") or ""),
            zero_runs=int(data.get("zero_runs") or 0),
            last_error=str(data.get("last_error") or ""),
        )


class ProfileStore:
    """解析规则缓存（JSON，原子写入；损坏时按空处理并保留原文件备份）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".bak"))
            except OSError:
                pass
            return {}
        profiles = data.get("profiles") if isinstance(data, dict) else None
        return profiles if isinstance(profiles, dict) else {}

    def get(self, url_norm: str) -> SourceProfile | None:
        raw = self._load().get(url_norm)
        if not isinstance(raw, Mapping):
            return None
        profile = SourceProfile.from_dict(raw)
        return profile if profile.selectors else None

    def put(self, profile: SourceProfile) -> None:
        data = self._load()
        data[profile.url_norm] = profile.to_dict()
        payload = json.dumps({"version": 1, "profiles": data}, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def remove(self, url_norm: str) -> bool:
        """删除某条规则缓存（规则自愈用）。"""
        data = self._load()
        if url_norm not in data:
            return False
        data.pop(url_norm, None)
        payload = json.dumps({"version": 1, "profiles": data}, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)
        return True


# ---------------------------------------------------------------------------
# 页面指纹与校验
# ---------------------------------------------------------------------------


def page_fingerprint(html: str) -> str:
    """页面结构指纹：只看标签与 class，忽略正文文案，用于判断「结构是否变了」。"""
    if not html:
        return ""
    try:
        tree = etree.HTML(html)
    except Exception:
        return ""
    if tree is None:
        return ""
    skeleton: list[str] = []
    for index, element in enumerate(tree.iter()):
        if index > 600:
            break
        if not isinstance(element.tag, str):
            continue
        skeleton.append(element.tag)
        classes = (element.get("class") or "").strip()
        if classes:
            skeleton.append(classes.replace(" ", "."))
    return simhash_to_hex(simhash_text(" ".join(skeleton)))


def validate_list_selectors(
    html: str,
    selectors: Mapping[str, str],
    *,
    base_url: str,
    min_items: int,
    source_name: str = "auto",
) -> tuple[bool, str, list]:
    """用真实页面验证规则。

    真实站点里常有少量「噪音节点」（导航 li、javascript:void(0) 链接、纯图片卡），
    因此不要求 100% 有效：过滤掉无效条目后，只要有效条目够多且占比合理即通过。
    """
    try:
        items = extract_list(
            html, selectors, base_url=base_url, source=source_name, max_items=50
        )
    except (ExtractError, etree.XPathError) as exc:
        return False, f"XPath 无法执行：{exc}", []
    except Exception as exc:  # 规则写坏时的兜底
        return False, f"抽取异常：{type(exc).__name__}: {exc}", []

    if not items:
        return False, "list_item 没匹配到任何条目（选择器不对，或页面是 JS 渲染的空壳）", []

    valid = [
        item
        for item in items
        if item.title.strip() and item.url.lower().startswith(("http://", "https://"))
    ]
    invalid = len(items) - len(valid)
    if len(valid) < max(1, min_items):
        return (
            False,
            f"list_item 匹配到 {len(items)} 个节点，但没有一条同时具备标题与绝对链接"
            "（title / url 选择器不对）",
            [],
        )
    ratio = len(valid) / len(items)
    if ratio < 0.5:
        return (
            False,
            f"{invalid}/{len(items)} 条无效（缺标题或缺绝对链接），有效率只有 {ratio:.0%}，"
            "容器选择器可能选错了",
            valid,
        )
    if invalid:
        return True, f"ok（忽略 {invalid} 条噪音节点）", valid
    return True, "ok", valid


# ---------------------------------------------------------------------------
# 启发式兜底
# ---------------------------------------------------------------------------


def _coverage(nodes: list[Any], expr: str, *, first_value: bool = False, sample: int = 6) -> float:
    picked = nodes[:sample]
    if not picked:
        return 0.0
    hits = 0
    for node in picked:
        if not isinstance(node, etree._Element):
            continue
        value = (
            xpath_first_value(node, expr) if first_value else xpath_text(node, expr)
        )
        if value and value.strip():
            hits += 1
    return hits / len(picked)


def _pick_candidate(nodes: list[Any], candidates: tuple[str, ...], *, first_value: bool, minimum: float):
    for expr in candidates:
        if _coverage(nodes, expr, first_value=first_value) >= minimum:
            return expr
    return ""


def heuristic_selectors(html: str, *, min_items: int = 2) -> dict[str, str]:
    """不依赖 AI 的确定性兜底：挑出最像列表的容器，再选最稳的字段 XPath。"""
    if not html:
        return {}
    try:
        tree = etree.HTML(html)
    except Exception:
        return {}
    if tree is None:
        return {}

    best: tuple[tuple[float, int], str, list[Any]] | None = None
    for pattern in CONTAINER_PATTERNS:
        try:
            nodes = tree.xpath(pattern)
        except etree.XPathError:
            continue
        if not isinstance(nodes, list) or len(nodes) < max(2, min_items):
            continue
        nodes = [node for node in nodes if isinstance(node, etree._Element)]
        if len(nodes) < max(2, min_items):
            continue
        url_cov = _coverage(nodes, ".//a[@href]/@href", first_value=True)
        title_cov = _coverage(nodes, ".//a//text()")
        if url_cov < 0.8 or title_cov < 0.6:
            continue
        score = (round(url_cov + title_cov, 3), len(nodes))
        if best is None or score > best[0]:
            best = (score, pattern, nodes)
    if best is None:
        return {}

    _score, container, nodes = best
    selectors = {
        "list_item": container,
        "title": _pick_candidate(nodes, TITLE_CANDIDATES, first_value=False, minimum=0.8),
        "url": _pick_candidate(nodes, URL_CANDIDATES, first_value=True, minimum=0.8),
        "picurl": _pick_candidate(nodes, PIC_CANDIDATES, first_value=True, minimum=0.5),
        "summary": _pick_candidate(nodes, SUMMARY_CANDIDATES, first_value=False, minimum=0.5),
        "published_at": "",
        "detail_content": "",
    }
    if not selectors["title"] or not selectors["url"]:
        return {}
    return selectors


def heuristic_detail_selector(html: str, *, min_chars: int) -> str:
    """详情页正文容器的启发式选择：取文字最多的常见容器。"""
    if not html:
        return ""
    try:
        tree = etree.HTML(html)
    except Exception:
        return ""
    if tree is None:
        return ""
    best_expr = ""
    best_len = 0
    for expr in DETAIL_PATTERNS:
        try:
            nodes = tree.xpath(expr)
        except etree.XPathError:
            continue
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, etree._Element):
                continue
            text = "".join(node.itertext()).strip()
            if len(text) > best_len:
                best_len = len(text)
                best_expr = expr
    return best_expr if best_len >= min_chars else ""


# ---------------------------------------------------------------------------
# 给 AI 的页面上下文（候选容器 + 示例条目），显著提高一次生成命中率
# ---------------------------------------------------------------------------


def _node_has_link_text(node: etree._Element) -> bool:
    try:
        anchors = node.xpath(".//a[@href]")
    except etree.XPathError:
        return False
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        text = "".join(anchor.itertext()).strip()
        if href and not href.startswith(("javascript:", "#", "mailto:")) and len(text) >= 4:
            return True
    return False


def page_candidates(html: str, *, max_candidates: int = 3) -> list[dict[str, Any]]:
    """扫描常见列表容器，返回「容器 XPath + 匹配数 + 示例条目 HTML」。"""
    if not html:
        return []
    try:
        tree = etree.HTML(html)
    except Exception:
        return []
    if tree is None:
        return []
    found: list[dict[str, Any]] = []
    for pattern in CONTAINER_PATTERNS:
        try:
            nodes = [node for node in tree.xpath(pattern) if isinstance(node, etree._Element)]
        except etree.XPathError:
            continue
        if len(nodes) < 2:
            continue
        valid = [node for node in nodes if _node_has_link_text(node)]
        if not valid:
            continue
        sample = etree.tostring(valid[0], encoding="unicode")
        found.append(
            {
                "pattern": pattern,
                "matched": len(nodes),
                "valid": len(valid),
                "sample_html": re.sub(r"\s+", " ", sample)[:1200],
                "nodes": nodes,
            }
        )
    found.sort(key=lambda item: (item["valid"], item["matched"]), reverse=True)
    return found[:max_candidates]


def _region_html(nodes: list[etree._Element], max_chars: int, *, min_chars: int = 300) -> str:
    """取候选节点所在容器（必要时逐级上溯）的 HTML，避免送整页样板。"""
    if not nodes:
        return ""
    region = nodes[0].getparent()
    for _ in range(3):
        if region is None:
            return ""
        html = etree.tostring(region, encoding="unicode")
        if len(html) >= min_chars or region.getparent() is None:
            cleaned = re.sub(r"[ \t]+", " ", html)
            cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
            return cleaned[:max_chars]
        region = region.getparent()
    return ""


def _densest_subtree(tree: etree._Element, max_chars: int) -> str:
    """详情页：取文字最多的内容容器子树（比整页 HTML 小得多）。"""
    best = None
    best_len = 0
    try:
        nodes = tree.xpath("//article | //main | //*[@role='main'] | //div")
    except etree.XPathError:
        nodes = []
    for node in nodes:
        if not isinstance(node, etree._Element):
            continue
        text = "".join(node.itertext()).strip()
        if len(text) > best_len:
            best, best_len = node, len(text)
    if best is None or best_len < 200:
        return ""
    html = etree.tostring(best, encoding="unicode")
    cleaned = re.sub(r"[ \t]+", " ", html)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned[:max_chars]


def build_page_context(
    html: str,
    *,
    max_chars: int,
    max_candidates: int = 3,
    prefer_list_region: bool = True,
) -> dict[str, str]:
    """组装给 AI 的页面上下文：候选容器提示 + 示例条目 + **重复区块所在子树**。

    prefer_list_region=True（列表页）：只送最佳候选容器所在子树，省掉页头/页脚/脚本等样板；
    False（详情页）：送正文最密集的容器子树。
    """
    hints = "（没有发现明显的重复列表结构，请直接读 HTML 判断）"
    sample = "（无）"
    excerpt = (html or "")[:max_chars]

    try:
        tree = etree.HTML(html)
    except Exception:
        tree = None

    if tree is not None:
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

        candidates = page_candidates(html, max_candidates=max_candidates)
        region = ""
        if candidates:
            hints = "\n".join(
                f"- 容器 XPath `{c['pattern']}`：匹配 {c['matched']} 个节点，"
                f"其中 {c['valid']} 个含有效链接（标题 + href）"
                for c in candidates
            )
            sample = candidates[0]["sample_html"]
            if prefer_list_region:
                region = _region_html(candidates[0]["nodes"], max_chars)

        if not region:
            try:
                cleaned = etree.tostring(tree, encoding="unicode")
            except Exception:
                cleaned = html
            cleaned = re.sub(r"[ \t]+", " ", cleaned)
            cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
            region = _densest_subtree(tree, max_chars) if not prefer_list_region else ""
            if not region:
                region = cleaned
        excerpt = region[:max_chars]

    return {"candidate_hints": hints, "sample_item_html": sample, "excerpt": excerpt}


def fetch_page_item(source: SourceConfig, http: HttpClient) -> RawItem | None:
    """整页兜底：抓一页，把「整页」当成一个候选条目交给下游筛选。"""
    result = http.get(
        source.url,
        referer=source.referer,
        headers=source.headers,
        use_selenium=source.use_selenium,
        encoding=source.encoding,
    )
    if not result.text:
        return None
    base = result.final_url or source.url
    title = ""
    description = ""
    picurl = ""
    try:
        tree = etree.HTML(result.text)
    except Exception:
        tree = None
    if tree is not None:
        h1 = ["".join(node.itertext()).strip() for node in tree.xpath("//h1")]
        page_titles = [text.strip() for text in tree.xpath("//title/text()")]
        title = next((text for text in h1 if text), "") or next(
            (text for text in page_titles if text), ""
        )
        descriptions = tree.xpath("//meta[@name='description']/@content")
        description = (descriptions[0].strip() if descriptions else "")
        images = tree.xpath("//meta[@property='og:image']/@content")
        picurl = (images[0].strip() if images else "")
    return RawItem(
        url=base,
        title=title or base,
        description=description,
        picurl=picurl,
        html=result.text,
        source=source.name,
        meta={"via": "page_fallback"},
    )


# ---------------------------------------------------------------------------
# AI 规则生成
# ---------------------------------------------------------------------------


class AutoSourceResolver:
    def __init__(
        self,
        client: DeepSeekClient,
        config: AutoSourceConfig,
        *,
        store: ProfileStore,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.log = log or (lambda _msg: None)
        self.template = self._load_template()
        self.last_mode = ""

    # ---------------------------------------------------------------- 模板
    def _load_template(self) -> str:
        return load_template(self.config.prompt_template)

    def _render(
        self,
        *,
        target: str,
        page_url: str,
        required_keys: str,
        min_items: int,
        page_html: str,
        previous_error: str = "",
        candidate_hints: str = "",
        sample_item_html: str = "",
    ) -> str:
        context = {
            "target": target,
            "page_url": page_url,
            "required_keys": required_keys,
            "min_items": str(min_items),
            "page_html": page_html[: self.config.html_max_chars],
            "previous_error": previous_error.strip() or "（无，这是第一次尝试）",
            "candidate_hints": candidate_hints.strip() or "（没有可用提示，请直接读 HTML）",
            "sample_item_html": sample_item_html.strip() or "（无）",
        }
        return _PLACEHOLDER_RE.sub(lambda match: context[match.group(1)], self.template)

    # ---------------------------------------------------------------- 自愈
    def note_success(self, source: SourceConfig) -> None:
        """抓取成功：清零连续 0 条计数。"""
        profile = self.store.get(normalize_url(source.url))
        if profile is None or (profile.zero_runs == 0 and not profile.last_error):
            return
        profile.zero_runs = 0
        profile.last_error = ""
        self.store.put(profile)

    def note_empty(self, source: SourceConfig) -> bool:
        """一次运行抽到 0 条：累计失败次数，达到阈值就删除缓存规则（下次重新生成）。

        返回是否触发了自愈（删除缓存）。
        """
        threshold = int(self.config.self_heal_after)
        if threshold <= 0:
            return False
        profile = self.store.get(normalize_url(source.url))
        if profile is None:
            return False
        profile.zero_runs += 1
        profile.last_error = "本次运行抽到 0 条"
        if profile.zero_runs >= threshold:
            self.store.remove(profile.url_norm)
            self.log(
                f"规则自愈：源 {source.name} 的缓存规则连续 {profile.zero_runs} 次抽到 0 条，"
                "已删除缓存，下次运行会重新生成"
            )
            return True
        self.store.put(profile)
        self.log(
            f"源 {source.name} 本次抽到 0 条（连续 {profile.zero_runs}/{threshold} 次），"
            "达到阈值后将自动重建规则"
        )
        return False

    # ---------------------------------------------------------------- 主入口
    def resolve(self, source: SourceConfig, http: HttpClient) -> SourceConfig:
        """把「只有 URL 的源」补全成可抓取的源；失败抛 SourceAdaptError。"""
        if source.has_list_selector:
            return source
        if not self.config.enabled:
            raise SourceAdaptError(
                "源没有 list_item 选择器，且 crawl.auto.enabled=false；请手写 selectors 或开启自动适配"
            )

        list_url = source.url
        result = http.get(
            list_url,
            referer=source.referer,
            headers=source.headers,
            use_selenium=source.use_selenium,
            encoding=source.encoding,
        )
        if not result.text:
            raise SourceAdaptError(
                f"列表页抓取失败，无法生成解析规则：{result.error or '空响应'}（{list_url}）"
            )

        html = result.text
        base_url = result.final_url or list_url
        url_norm = normalize_url(list_url)
        fingerprint = page_fingerprint(html)

        cached = self.store.get(url_norm)
        if cached and cached.page_fingerprint == fingerprint:
            ok, message, items = validate_list_selectors(
                html,
                cached.selectors,
                base_url=base_url,
                min_items=self.config.min_items,
                source_name=source.name,
            )
            if ok:
                self.last_mode = "cache"
                self.log(
                    f"源 {source.name} 命中解析规则缓存（生成于 {cached.generated_at or '未知'}，"
                    f"mode={cached.mode}），跳过 AI：list_item={cached.selectors.get('list_item')}"
                )
                return source.with_selectors(cached.selectors)
            self.log(
                f"源 {source.name} 的缓存规则已失效（{message}），将重新生成：{list_url}"
            )

        profile: SourceProfile | None = None
        if self.client.available:
            profile = self._adapt_with_ai(source, http, html, base_url, url_norm, fingerprint)
        else:
            self.log(
                f"源 {source.name} 未配置 DeepSeek 密钥，跳过 AI 规则生成，改用启发式兜底"
            )

        if profile is None and self.config.heuristic_fallback:
            profile = self._adapt_with_heuristic(source, html, base_url, url_norm, fingerprint)

        if profile is None:
            raise SourceAdaptError(
                f"源 {source.name} 的解析规则生成失败（AI 与启发式均未通过校验）：{list_url}"
            )

        self.store.put(profile)
        self.last_mode = profile.mode
        self.log(
            f"源 {source.name} 自动适配完成：mode={profile.mode} "
            f"selectors={profile.selectors} 已缓存到 {self.store.path}"
        )
        resolved = source.with_selectors(profile.selectors)
        # AI 顺带给出的编码/来源页只用于补空，不覆盖用户在 YAML 里写死的值
        if profile.encoding and not resolved.encoding:
            resolved.encoding = profile.encoding
        if profile.referer and not resolved.referer:
            resolved.referer = profile.referer
        return resolved


    # ---------------------------------------------------------------- AI 生成
    def _adapt_with_ai(
        self,
        source: SourceConfig,
        http: HttpClient,
        html: str,
        base_url: str,
        url_norm: str,
        fingerprint: str,
    ) -> SourceProfile | None:
        context = build_page_context(html, max_chars=self.config.html_max_chars)
        previous_error = ""
        for attempt in range(1, self.config.max_attempts + 1):
            prompt = self._render(
                target="列表页条目",
                page_url=source.url,
                required_keys="list_item, title, url, picurl, summary, published_at",
                min_items=self.config.min_items,
                page_html=context["excerpt"],
                previous_error=previous_error,
                candidate_hints=context["candidate_hints"],
                sample_item_html=context["sample_item_html"],
            )
            try:
                response = self.client.chat(
                    [{"role": "user", "content": prompt}],
                    label=f"源适配 {source.name} 第 {attempt} 次",
                )
            except LLMError as exc:
                self.log(f"源 {source.name} 生成解析规则时 LLM 调用失败：{exc}")
                return None

            parsed, message = self._parse_selectors(response.content, mode="list")
            if parsed is None:
                previous_error = message
                self.log(f"源 {source.name} 第 {attempt} 次生成结果不可用：{message}")
                continue
            selectors, extra = parsed

            ok, message, _items = validate_list_selectors(
                html,
                selectors,
                base_url=base_url,
                min_items=self.config.min_items,
                source_name=source.name,
            )
            if not ok:
                previous_error = (
                    f"上一次给出的规则没通过真实页面校验：{message}。请修正后重新给出完整规则。"
                )
                self.log(f"源 {source.name} 第 {attempt} 次规则校验失败：{message}")
                continue

            self.log(f"源 {source.name} 第 {attempt} 次生成的规则通过校验")
            profile = SourceProfile(
                url=source.url,
                url_norm=url_norm,
                selectors=selectors,
                encoding=str(extra.get("encoding") or ""),
                referer=str(extra.get("referer") or ""),
                mode="ai",
                attempts=attempt,
                page_fingerprint=fingerprint,
                notes=str(extra.get("notes") or ""),
                generated_at=iso(utcnow()),
                model=response.model,
            )
            if self.config.detail_adapt:
                self._adapt_detail(source, http, html, base_url, profile)
            return profile
        return None

    def _parse_selectors(
        self, content: str, *, mode: str
    ) -> tuple[tuple[dict[str, str], dict[str, Any]] | None, str]:
        text = (content or "").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"返回不是严格 JSON：{exc.msg}（位置 {exc.lineno}:{exc.colno}）"
        if not isinstance(data, Mapping):
            return None, "返回的顶层不是 JSON 对象"
        raw = data.get("selectors")
        if not isinstance(raw, Mapping):
            return None, "返回里缺少 selectors 对象"
        try:
            selectors = parse_selectors(raw, "auto", require_list_item=(mode == "list"))
        except ExtractError as exc:
            return None, str(exc)
        if mode == "list" and not selectors.get("list_item"):
            return None, "selectors.list_item 为空"
        if mode == "detail" and not selectors.get("detail_content"):
            return None, "selectors.detail_content 为空"
        # 形态检查：允许 XPath 或可转换的 CSS；转换后统一按 XPath 使用
        converted = False
        if mode == "list":
            list_item = selectors.get("list_item", "")
            if not list_item.startswith(("//", ".//")):
                try:
                    selectors["list_item"] = css_to_xpath(list_item)
                    converted = True
                except CssError as exc:
                    return None, (
                        "selectors.list_item 既不是 XPath（// 或 .// 开头），也无法按 CSS 解析"
                        f"（{exc}）；收到：{list_item[:80]!r}"
                    )
            for key in ("title", "url", "picurl", "summary", "published_at"):
                expr = selectors.get(key) or ""
                if not expr or expr.startswith(("./", ".//")):
                    continue
                try:
                    selectors[key] = css_to_relative_xpath(expr)
                    converted = True
                except CssError as exc:
                    return None, (
                        f"selectors.{key} 既不是相对 XPath（./ 或 .// 开头），也无法按 CSS 解析"
                        f"（{exc}）；收到：{expr[:80]!r}"
                    )
        else:
            detail_expr = selectors.get("detail_content", "")
            if not detail_expr.startswith(("//", ".//", "/")):
                try:
                    selectors["detail_content"] = css_to_xpath(detail_expr)
                    converted = True
                except CssError as exc:
                    return None, (
                        "selectors.detail_content 既不是 XPath，也无法按 CSS 解析"
                        f"（{exc}）；收到：{detail_expr[:80]!r}"
                    )
        if converted:
            self.log(f"AI 返回的选择器含 CSS，已自动转换为 XPath：{selectors}")
        extra = {str(k): v for k, v in data.items() if k != "selectors"}
        return (selectors, extra), "ok"

    # ---------------------------------------------------------------- 详情页
    def _adapt_detail(
        self,
        source: SourceConfig,
        http: HttpClient,
        list_html: str,
        base_url: str,
        profile: SourceProfile,
    ) -> None:
        """顺带生成详情页正文选择器；失败只降级成自动正文抽取，不影响列表规则。"""
        detail = self._fetch_first_detail(source, http, list_html, base_url, profile.selectors)
        if not detail:
            self.log(f"源 {source.name} 没有可用的详情页样本，跳过详情页规则生成")
            return
        detail_html, detail_url = detail

        detail_context = build_page_context(
            detail_html, max_chars=self.config.html_max_chars, prefer_list_region=False
        )
        previous_error = ""
        for attempt in range(1, self.config.max_attempts + 1):
            prompt = self._render(
                target="详情页正文",
                page_url=detail_url,
                required_keys="detail_content",
                min_items=1,
                page_html=detail_context["excerpt"],
                previous_error=previous_error,
                candidate_hints="（详情页只需要正文容器，参考下面的页面结构）",
                sample_item_html=detail_context["excerpt"][:1200],
            )
            try:
                response = self.client.chat(
                    [{"role": "user", "content": prompt}],
                    label=f"源适配 {source.name} 详情页第 {attempt} 次",
                )
            except LLMError as exc:
                self.log(f"源 {source.name} 详情页规则生成时 LLM 调用失败：{exc}")
                return
            parsed, message = self._parse_selectors(response.content, mode="detail")
            if parsed is None:
                previous_error = message
                continue
            selectors, _extra = parsed
            detail_selector = selectors.get("detail_content", "")
            text = extract_container_text(detail_html, detail_selector)
            if len(text) < self.config.min_detail_chars:
                previous_error = (
                    f"上一次的 detail_content 只抽出 {len(text)} 字（至少需要 "
                    f"{self.config.min_detail_chars} 字），请换一个更像正文的容器。"
                )
                self.log(f"源 {source.name} 第 {attempt} 次正文规则只抽到 {len(text)} 字")
                continue
            profile.selectors["detail_content"] = detail_selector
            self.log(
                f"源 {source.name} 详情页正文规则已生成：{detail_selector}（试抽 {len(text)} 字）"
            )
            return
        self.log(f"源 {source.name} 详情页规则未生成，回退自动正文抽取")

    def _fetch_first_detail(
        self,
        source: SourceConfig,
        http: HttpClient,
        list_html: str,
        base_url: str,
        selectors: Mapping[str, str],
    ) -> tuple[str, str] | None:
        try:
            items = extract_list(
                list_html,
                selectors,
                base_url=base_url,
                source=source.name,
                max_items=5,
            )
        except Exception:
            return None
        for item in items:
            if not item.url:
                continue
            result = http.get(
                item.url,
                referer=source.referer or source.url,
                headers=source.headers,
                use_selenium=source.use_selenium,
            )
            if result.text:
                return result.text, result.final_url or item.url
        return None

    # ---------------------------------------------------------------- 启发式
    def _adapt_with_heuristic(
        self,
        source: SourceConfig,
        html: str,
        base_url: str,
        url_norm: str,
        fingerprint: str,
    ) -> SourceProfile | None:
        selectors = heuristic_selectors(html, min_items=self.config.min_items + 1)
        if not selectors:
            self.log(f"源 {source.name} 启发式兜底也没找到可用的列表结构")
            return None
        ok, message, _items = validate_list_selectors(
            html,
            selectors,
            base_url=base_url,
            min_items=self.config.min_items,
            source_name=source.name,
        )
        if not ok:
            self.log(f"源 {source.name} 启发式兜底规则未通过校验：{message}")
            return None
        self.log(f"源 {source.name} 启发式兜底规则通过校验：{selectors}")
        return SourceProfile(
            url=source.url,
            url_norm=url_norm,
            selectors=selectors,
            mode="heuristic",
            attempts=0,
            page_fingerprint=fingerprint,
            notes="AI 不可用，使用启发式规则",
            generated_at=iso(utcnow()),
        )
