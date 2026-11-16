# -*- coding: utf-8 -*-
"""粗筛：硬规则过滤 + 粗召回

硬规则（hard rules）：
    * 关键词黑名单（config/app.yaml: preprocess.blacklist_keywords）；
    * 正文过短 / 标题为空（明显不是文章）。

粗召回（coarse recall）：
    * Tag：must_include 命中数、boost 命中、exclude 命中；
    * 意图关键词：从 intent.description 抽取的关键词；
    * embedding：意图文本 vs 候选正文的余弦相似度。
    三者「或」关系，全部不达标才丢弃 —— 这样「只写意图、不配 Tag」也能正常召回。
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from models import ArticleItem, Decision, TagSignals
from pipeline.embedder import HashingTfEmbedder
from pipeline.preprocess import extract_keywords, split_intent_text
from settings import FilterIntentConfig, PreprocessConfig


def _is_ascii_word(tag: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _+\-.#/]*", tag))


class TermMatcher:
    """Tag / 关键词匹配器。

    * 纯 ASCII 词用词边界匹配，避免 "RAG" 命中 "storage"、"eval" 命中 "medieval"；
    * 中文词用子串匹配（中文没有空格分词边界）。
    """

    def __init__(self, terms: Iterable[str]) -> None:
        self.terms: list[str] = [t for t in terms if t and t.strip()]
        self._patterns: dict[str, re.Pattern[str]] = {}
        for term in self.terms:
            if _is_ascii_word(term):
                self._patterns[term] = re.compile(
                    r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE
                )

    def hits(self, haystack: str) -> list[str]:
        if not haystack:
            return []
        lowered = haystack.lower()
        found: list[str] = []
        for term in self.terms:
            pattern = self._patterns.get(term)
            if pattern is not None:
                matched = pattern.search(haystack) is not None
            else:
                matched = term.lower() in lowered
            if matched:
                found.append(term)
        return found


def candidate_text(item: ArticleItem, *, max_chars: int = 2000) -> str:
    """粗召回与 Tag 命中的判定文本：标题 + 摘要 + 正文前 N 字。"""
    parts = [item.title or "", item.description or "", (item.content or "")[:max_chars]]
    return "\n".join(part for part in parts if part)


class HardRuleFilter:
    """硬规则：命中黑名单 / 正文过短的直接丢弃，省下后续所有开销。"""

    def __init__(self, config: PreprocessConfig, *, log=None) -> None:
        self.config = config
        self.matcher = TermMatcher(config.blacklist_keywords)
        self.log = log or (lambda _m: None)

    def check(self, item: ArticleItem) -> Decision:
        content_len = len((item.content or "").strip())
        if content_len < self.config.min_content_chars:
            return Decision(
                keep=False,
                stage="hard_rule",
                reason=(
                    f"清洗后正文过短（{content_len} 字 < min_content_chars="
                    f"{self.config.min_content_chars}），疑似非文章页"
                ),
            )
        if self.config.drop_short_title and not (item.title or "").strip():
            return Decision(keep=False, stage="hard_rule", reason="标题为空")
        hits = self.matcher.hits(candidate_text(item))
        if hits:
            return Decision(
                keep=False,
                stage="hard_rule",
                reason=f"命中黑名单关键词 {hits}（preprocess.blacklist_keywords）",
            )
        return Decision(keep=True, stage="hard_rule", reason="通过硬规则")

    def check_list_fields(self, item: ArticleItem) -> Decision:
        """列表页阶段（还没有正文）能做的硬规则：标题长度 + 标题/摘要黑名单。"""
        title = (item.title or "").strip()
        if not title or len(title) < 3:
            return Decision(keep=False, stage="hard_rule", reason=f"标题过短或为空（{title!r}）")
        hits = self.matcher.hits(f"{title}\n{item.description or ''}")
        if hits:
            return Decision(
                keep=False,
                stage="hard_rule",
                reason=f"标题/摘要命中黑名单关键词 {hits}",
            )
        return Decision(keep=True, stage="hard_rule", reason="列表字段通过硬规则")


class CoarseRecaller:
    """粗召回：Tag 命中 / 意图关键词 / embedding 相似度，三者取或。"""

    def __init__(
        self,
        intent_config: FilterIntentConfig,
        embedder: HashingTfEmbedder,
        *,
        log=None,
        keyword_cache: dict[str, list[str]] | None = None,
    ) -> None:
        self.config = intent_config
        self.prefilter = intent_config.prefilter
        self.embedder = embedder
        self.log = log or (lambda _m: None)
        self._keyword_cache = keyword_cache if keyword_cache is not None else {}

        self.must_matcher = TermMatcher(intent_config.tags.must_include)
        self.boost_matcher = TermMatcher(intent_config.tags.boost)
        self.exclude_matcher = TermMatcher(intent_config.tags.exclude)
        self._positive_text, self._negative_text = split_intent_text(
            intent_config.intent.description
        )
        self.keyword_matcher = TermMatcher(self.intent_keywords())
        self._intent_vector = None
        if self.config.has_intent:
            # 意图向量只用正面描述 + 正例，避免「不要：融资新闻」反而拉高融资文章的相似度
            intent_text = HashingTfEmbedder.compose_intent_text(
                self._positive_text, self.config.intent.examples_include, []
            )
            self._intent_vector = self.embedder.vector(intent_text)

    def intent_keywords(self) -> list[str]:
        """从意图描述+正例抽关键词（受 prefilter.keyword_from_intent 控制）。

        「不要」段落、反例、exclude Tag 里的词会被排除，不当成正向召回信号。
        """
        if not self.prefilter.keyword_from_intent or not self.config.has_intent:
            return []
        cache_key = "intent_keywords"
        if cache_key in self._keyword_cache:
            return self._keyword_cache[cache_key]
        avoid = "\n".join(
            [
                self._negative_text,
                *self.config.tags.exclude,
                *self.config.intent.examples_exclude,
            ]
        )
        keywords = extract_keywords(
            self._positive_text, top_k=self.prefilter.keyword_top_k, avoid_text=avoid
        )
        self._keyword_cache[cache_key] = keywords
        return keywords

    # ---------------------------------------------------------------- 列表页宽松粗筛
    def evaluate_list_level(self, item: ArticleItem) -> Decision:
        """列表页阶段只有标题+摘要时的粗筛（宽松），用来决定「值不值得抓详情页」。

        宽松策略：embedding 阈值打 7 折、任一 Tag / 意图关键词命中即通过；
        这样不会因为列表摘要信息少而误杀文章，真正的严格判断留给拿到正文之后。
        """
        text = "\n".join(part for part in (item.title, item.description) if part)
        if not text.strip():
            return Decision(keep=False, stage="list_prefilter", reason="标题与摘要均为空")

        must_hits = self.must_matcher.hits(text)
        boost_hits = self.boost_matcher.hits(text)
        exclude_hits = self.exclude_matcher.hits(text)
        keyword_hits = self.keyword_matcher.hits(text)

        if exclude_hits:
            return Decision(
                keep=False,
                stage="list_prefilter_exclude",
                reason=f"标题/摘要命中 exclude Tag {exclude_hits}",
            )

        if self.config.is_legacy_tag_mode:
            if not (self.config.tags.must_include or self.config.tags.boost):
                return Decision(keep=True, stage="list_prefilter", reason="legacy 模式仅配置 exclude")
            if must_hits or boost_hits:
                return Decision(keep=True, stage="list_prefilter", reason=f"Tag 命中 {must_hits + boost_hits}")
            return Decision(
                keep=False,
                stage="list_prefilter",
                reason="legacy Tag 模式下标题/摘要未命中 must_include / boost",
            )

        similarity = 0.0
        if self._intent_vector is not None:
            similarity = self.embedder.similarity(self._intent_vector, self.embedder.vector(text))
        relaxed = self.prefilter.similarity_threshold * 0.7
        if must_hits or keyword_hits or boost_hits or similarity >= relaxed:
            reasons = []
            if must_hits:
                reasons.append(f"must_include {must_hits}")
            if keyword_hits:
                reasons.append(f"意图关键词 {keyword_hits[:4]}")
            if boost_hits:
                reasons.append(f"boost {boost_hits}")
            if similarity >= relaxed:
                reasons.append(f"相似度 {similarity:.3f} >= {relaxed:.3f}")
            return Decision(
                keep=True, stage="list_prefilter", reason="列表粗筛通过：" + "；".join(reasons)
            )
        return Decision(
            keep=False,
            stage="list_prefilter",
            reason=(
                "标题/摘要未体现主题："
                f"must_include 0，意图关键词 0，相似度 {similarity:.3f} < 宽松阈值 {relaxed:.3f}"
            ),
        )

    # ---------------------------------------------------------------- 评估
    def evaluate(self, item: ArticleItem) -> tuple[Decision, TagSignals]:
        text = candidate_text(item)
        signals = TagSignals(
            must_include_hits=self.must_matcher.hits(text),
            boost_hits=self.boost_matcher.hits(text),
            exclude_hits=self.exclude_matcher.hits(text),
            intent_keyword_hits=self.keyword_matcher.hits(text),
        )
        if self._intent_vector is not None:
            signals.embedding_similarity = round(
                self.embedder.similarity(self._intent_vector, self.embedder.vector(text)), 4
            )

        # exclude 是硬否：Tag 语义里就是「命中即丢弃」
        if signals.exclude_hits:
            signals.reason = f"命中 exclude Tag {signals.exclude_hits}"
            return (
                Decision(keep=False, stage="prefilter_exclude", reason=signals.reason),
                signals,
            )

        # 只配了 Tag（legacy 模式）：命中 must_include 或 boost 才算召回
        if self.config.is_legacy_tag_mode:
            if not (self.config.tags.must_include or self.config.tags.boost):
                # 只配了 exclude：仅做排除，不拦召回
                signals.recalled = True
                signals.reason = "legacy 模式仅配置 exclude，只做排除"
                return (
                    Decision(keep=True, stage="prefilter_recall", reason=signals.reason),
                    signals,
                )
            if signals.must_include_hits or signals.boost_hits:
                signals.recalled = True
                signals.reason = f"Tag 召回命中 {signals.tag_hits}"
                return Decision(keep=True, stage="prefilter_recall", reason=signals.reason), signals
            signals.reason = "legacy Tag 模式下 must_include / boost 均未命中"
            return (
                Decision(keep=False, stage="prefilter_recall", reason=signals.reason),
                signals,
            )

        has_must = bool(self.config.tags.must_include)
        must_ok = len(signals.must_include_hits) >= self.prefilter.must_include_min_hits
        kw_ok = (
            bool(self.keyword_matcher.terms)
            and len(signals.intent_keyword_hits) >= self.prefilter.min_keyword_hits
        )
        sim_ok = signals.embedding_similarity >= self.prefilter.similarity_threshold

        if (has_must and must_ok) or kw_ok or sim_ok:
            reasons = []
            if has_must and must_ok:
                reasons.append(f"must_include 命中 {signals.must_include_hits}")
            if kw_ok:
                reasons.append(f"意图关键词命中 {signals.intent_keyword_hits[:5]}")
            if sim_ok:
                reasons.append(
                    f"embedding 相似度 {signals.embedding_similarity:.3f} >= "
                    f"{self.prefilter.similarity_threshold}"
                )
            signals.recalled = True
            signals.reason = "粗召回通过：" + "；".join(reasons)
            return Decision(keep=True, stage="prefilter_recall", reason=signals.reason), signals

        signals.reason = (
            "粗召回未通过："
            f"must_include 命中 {len(signals.must_include_hits)}/{self.prefilter.must_include_min_hits}，"
            f"意图关键词命中 {len(signals.intent_keyword_hits)}/{self.prefilter.min_keyword_hits}，"
            f"embedding 相似度 {signals.embedding_similarity:.3f} < "
            f"{self.prefilter.similarity_threshold}"
        )
        return (
            Decision(keep=False, stage="prefilter_recall", reason=signals.reason),
            signals,
        )
