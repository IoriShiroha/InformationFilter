# -*- coding: utf-8 -*-
"""统一数据模型（新增模块，不改动原有爬虫/日志/推送的能力）

数据流：
    RawItem      爬虫产出：只负责「拿全」标题/摘要/封面/正文 HTML，不做任何筛选判断
    ArticleItem  预处理后的统一条目：补全 url_norm、content_hash(64bit simhash)、清洗正文
                 并在筛选阶段回填 intent_matched / matched_intent / tags / score /
                 is_relevant / reason
    FilterResult 精筛（LLM 或 legacy Tag 模式）对单条内容的判定结果
    TagSignals   粗召回阶段收集到的 Tag / 意图关键词 / embedding 相似度信号
    Decision     某一阶段「保留或丢弃」的裁决，丢弃原因会进日志与丢弃样本库

设计约束：
    * 这里只放数据结构与轻量校验，不放任何业务阈值、意图描述或 Tag 字面量。
    * 所有「会被调参」的东西走 config/*.yaml。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

SIMHASH_BITS = 64
_MASK = (1 << SIMHASH_BITS) - 1

_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class ModelError(ValueError):
    """模型字段不合法时抛出。"""


def utcnow() -> datetime:
    """统一使用带时区的 UTC 时间，避免跨时区比较出错。"""
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str:
    """datetime -> 可入库/入日志的 ISO 字符串。"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """ISO 字符串 -> datetime，非法值返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def simhash_to_hex(value: int) -> str:
    return f"{value & _MASK:016x}"


def simhash_from_hex(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value, 16) & _MASK
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class RawItem:
    """爬虫层产出的原始条目。

    html / content_raw 二选一即可：列表页直出正文时用 content_raw，
    需要进详情页时把详情页 HTML 放在 html 里交给预处理层清洗。
    """

    url: str
    title: str = ""
    description: str = ""
    picurl: str = ""
    html: str = ""
    content_raw: str = ""
    source: str = ""
    crawled_at: datetime = field(default_factory=utcnow)
    meta: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """最小可用性判断：有 http(s) 链接且标题非空。"""
        return bool(self.url and self.title and _URL_SCHEME_RE.match(self.url.strip()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "picurl": self.picurl,
            "source": self.source,
            "crawled_at": iso(self.crawled_at),
            "meta": dict(self.meta),
        }


@dataclass(slots=True)
class ArticleItem:
    """全链路统一条目。

    content_hash 语义：清洗后正文的 64 位 simhash 十六进制串（空串表示正文过短、不参与比对）。
    url_norm 语义：去掉锚点、跟踪参数并排序后的规范化 URL，作为幂等主键。
    """

    url: str
    title: str = ""
    description: str = ""
    picurl: str = ""
    content: str = ""
    url_norm: str = ""
    content_hash: str = ""
    source: str = ""
    crawled_at: datetime = field(default_factory=utcnow)
    # ---- 筛选阶段回填（默认未筛选）----
    intent_matched: bool | None = None
    matched_intent: str = ""
    tags: list[str] = field(default_factory=list)
    score: float | None = None
    is_relevant: bool | None = None
    reason: str = ""
    # ---- 仅内部流转，不参与推送 ----
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- 派生字段
    def ensure_url_norm(self, strip_params: list[str] | None = None) -> str:
        """未显式设置时用本地默认规则补一个 url_norm（权威归一化在 pipeline.preprocess）。"""
        if not self.url_norm:
            from pipeline.preprocess import normalize_url  # 延迟导入，避免循环依赖

            self.url_norm = normalize_url(self.url, strip_params)
        return self.url_norm

    def ensure_content_hash(self) -> str:
        if not self.content_hash and self.content:
            from pipeline.preprocess import simhash_text  # 延迟导入

            self.content_hash = simhash_to_hex(simhash_text(self.content))
        return self.content_hash

    @property
    def is_filtered(self) -> bool:
        return self.is_relevant is not None

    # ---------------------------------------------------------------- 校验
    def validate(self) -> None:
        """进入筛选前的强校验，抛出 ModelError 由 runner 记日志丢弃。"""
        if not self.url or not self.url.strip():
            raise ModelError("url 为空")
        if not _URL_SCHEME_RE.match(self.url.strip()):
            raise ModelError(f"url 不是 http(s) 链接: {self.url!r}")
        if not _URL_SCHEME_RE.match(urlsplit(self.url.strip()).geturl() or ""):
            raise ModelError(f"url 结构非法: {self.url!r}")
        if not self.title.strip():
            raise ModelError("title 为空")

    def validate_picurl(self) -> str:
        """封面图只接受 http(s)，其它（相对路径、data:）一律清空，避免推送脏数据。"""
        pic = (self.picurl or "").strip()
        return pic if _URL_SCHEME_RE.match(pic) else ""

    # ---------------------------------------------------------------- 回填与序列化
    def apply_filter_result(self, result: "FilterResult") -> None:
        self.intent_matched = result.intent_matched
        self.matched_intent = result.matched_intent
        self.tags = list(result.tags)
        self.score = result.score
        self.is_relevant = result.is_relevant
        self.reason = result.reason

    def to_record(self) -> dict[str, Any]:
        """落库用扁平字典。"""
        return {
            "url": self.url,
            "url_norm": self.url_norm,
            "title": self.title,
            "description": self.description,
            "picurl": self.picurl,
            "content": self.content,
            "content_hash": self.content_hash,
            "source": self.source,
            "crawled_at": iso(self.crawled_at),
            "intent_matched": None if self.intent_matched is None else int(self.intent_matched),
            "matched_intent": self.matched_intent,
            "tags": list(self.tags),
            "score": self.score,
            "is_relevant": None if self.is_relevant is None else int(self.is_relevant),
            "reason": self.reason,
        }

    def to_push_fields(self) -> dict[str, str]:
        """推送模块需要的字段（字段名与原有 send_news 参数保持一致）。"""
        return {
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "picurl": self.validate_picurl(),
        }


@dataclass(slots=True)
class FilterResult:
    """单条内容的筛选结果（LLM 输出或 legacy Tag 判定）。"""

    url: str
    intent_matched: bool
    is_relevant: bool
    matched_intent: str = ""
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    title: str = ""
    description: str = ""
    # 原样保留 LLM 给的单条 JSON，便于排查与回放
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "intent_matched": self.intent_matched,
            "matched_intent": self.matched_intent,
            "tags": list(self.tags),
            "score": round(self.score, 4),
            "is_relevant": self.is_relevant,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TagSignals:
    """粗召回阶段收集到的全部信号，供精筛后做冲突裁决与日志解释。"""

    must_include_hits: list[str] = field(default_factory=list)
    boost_hits: list[str] = field(default_factory=list)
    exclude_hits: list[str] = field(default_factory=list)
    intent_keyword_hits: list[str] = field(default_factory=list)
    embedding_similarity: float = 0.0
    recalled: bool = False
    reason: str = ""

    @property
    def tag_hits(self) -> list[str]:
        """命中的 Tag（must_include + boost，保序去重）。"""
        seen: list[str] = []
        for tag in [*self.must_include_hits, *self.boost_hits]:
            if tag not in seen:
                seen.append(tag)
        return seen

    def as_text(self) -> str:
        return (
            f"must_include={self.must_include_hits or '-'} "
            f"boost={self.boost_hits or '-'} exclude={self.exclude_hits or '-'} "
            f"intent_kw={self.intent_keyword_hits or '-'} "
            f"sim={self.embedding_similarity:.3f}"
        )


@dataclass(slots=True)
class Decision:
    """某阶段的裁决结果。keep=False 时 reason 会进日志与丢弃样本库。"""

    keep: bool
    stage: str
    reason: str = ""
    conflict: str = ""

    def merged_reason(self) -> str:
        if self.conflict:
            return f"{self.reason}｜冲突：{self.conflict}"
        return self.reason
