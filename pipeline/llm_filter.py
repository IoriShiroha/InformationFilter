# -*- coding: utf-8 -*-
"""AI 精筛层

组成：
    PromptRenderer  读取 config/prompts/filter_prompt.md，只做占位拼接；
                    意图、Tag、阈值、冲突规则全部来自 YAML，模板里没有任何主题内容。
    LLMFilter       组装候选 -> 调 DeepSeek -> 严格 JSON 解析 -> 字段校验 ->
                    按 conflict_policy 裁决 is_relevant -> 产出 FilterResult。
    LegacyTagFilter 只配 Tag、不写意图时的向后兼容路径（不调用 LLM）。

降级原则：解析失败 / 字段非法 / url 对不上，逐条丢弃并打日志，绝不阻断整条流水线。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

try:  # 缺依赖时给出可读提示
    _ = yaml.safe_load
except AttributeError:  # pragma: no cover
    raise ModuleNotFoundError(
        "PyYAML 版本不可用：请执行 python -m pip install -r requirements.txt"
    ) from None

from models import ArticleItem, FilterResult, TagSignals
from pipeline.llm_client import DeepSeekClient, LLMError
from pipeline.metrics import PipelineStats
from pipeline.preprocess import select_excerpt
from settings import CONFLICT_POLICIES, FilterIntentConfig, LLMConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = PROJECT_ROOT / "config" / "prompts" / "filter_prompt.md"
DEFAULT_POLICY_FILE = PROJECT_ROOT / "config" / "prompts" / "conflict_policy.yaml"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

#: 模板必须包含的占位符（少了任何一个都说明模板被改坏，意图或 Tag 会静默丢失）
REQUIRED_PLACEHOLDERS = (
    "intent_description",
    "tags_must_include",
    "tags_boost",
    "tags_exclude",
    "conflict_policy_instruction",
    "candidates",
    "candidate_count",
    "score_threshold",
)

_CONTENT_PLACEHOLDERS = REQUIRED_PLACEHOLDERS + (
    "intent_examples_include",
    "intent_examples_exclude",
    "intent_preferences",
    "conflict_policy",
    "content_max_chars",
)

_TAGS_MUST = "must_include"
_TAGS_BOOST = "boost"


class PromptError(ValueError):
    """Prompt 模板 / 冲突策略文件不合法。"""


class ParseError(ValueError):
    """LLM 返回内容无法按约定结构解析。"""


@dataclass(slots=True)
class FilterBatchOutcome:
    results: list[FilterResult] = field(default_factory=list)
    #: (url, 丢弃原因)，由 runner 打日志 + 写丢弃样本
    failures: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt 渲染
# ---------------------------------------------------------------------------


class PromptRenderer:
    def __init__(
        self,
        intent_config: FilterIntentConfig,
        llm_config: LLMConfig,
        *,
        template_path: str | Path | None = None,
        policy_path: str | Path | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.intent_config = intent_config
        self.llm_config = llm_config
        self.log = log or (lambda _msg: None)
        self.template_path = _resolve_path(template_path or llm_config.prompt_template or DEFAULT_TEMPLATE)
        self.policy_path = _resolve_path(policy_path or DEFAULT_POLICY_FILE)
        self.template = self._load_template(self.template_path)
        self.policies = self._load_policies(self.policy_path)
        #: 由 runner 注入（意图关键词 + 命中 Tag），用于按关键词择段正文
        self.excerpt_keywords: list[str] = []

    # ---------------------------------------------------------------- 加载校验
    @staticmethod
    def _load_template(path: Path) -> str:
        if not path.is_file():
            raise PromptError(f"Prompt 模板不存在: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise PromptError(f"Prompt 模板为空: {path}")
        found = set(_PLACEHOLDER_RE.findall(text))
        unknown = sorted(found - set(_CONTENT_PLACEHOLDERS))
        if unknown:
            raise PromptError(
                f"Prompt 模板 {path.name} 含未知占位符 {unknown}；"
                f"可用占位符：{sorted(_CONTENT_PLACEHOLDERS)}"
            )
        missing = [name for name in REQUIRED_PLACEHOLDERS if name not in found]
        if missing:
            raise PromptError(
                f"Prompt 模板 {path.name} 缺少必需占位符 {missing}；"
                "意图/Tag 必须通过占位符注入，不能被写死或漏掉"
            )
        return text

    @staticmethod
    def _load_policies(path: Path) -> dict[str, str]:
        if not path.is_file():
            raise PromptError(f"冲突策略文案文件不存在: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise PromptError(f"冲突策略文案 YAML 解析失败: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PromptError("冲突策略文案必须是「策略名 -> 文案」的字典")
        missing = [policy for policy in CONFLICT_POLICIES if not str(data.get(policy) or "").strip()]
        if missing:
            raise PromptError(f"冲突策略文案缺少 {missing}，可选值应为 {list(CONFLICT_POLICIES)}")
        return {policy: str(data[policy]).strip() for policy in CONFLICT_POLICIES}

    # ---------------------------------------------------------------- 组装
    @property
    def conflict_instruction(self) -> str:
        return self.policies[self.intent_config.conflict_policy]

    @staticmethod
    def _format_terms(values: Sequence[str], empty: str) -> str:
        cleaned = [v for v in values if v]
        return "、".join(f"`{v}`" for v in cleaned) if cleaned else empty

    @staticmethod
    def _format_examples(values: Sequence[str], empty: str) -> str:
        cleaned = [v for v in values if v]
        if not cleaned:
            return empty
        return "\n".join(f"- {value}" for value in cleaned)

    def _render_candidates(self, items: Sequence[ArticleItem]) -> str:
        limit = max(50, self.llm_config.content_max_chars)
        blocks: list[str] = []
        for index, item in enumerate(items, 1):
            content = (item.content or "").strip()
            if len(content) > limit:
                # 按意图关键词择段，而不是简单截头：命中段落 + 开头导语，控制 token
                content = select_excerpt(content, self.excerpt_keywords, limit)
                content += "\n…（正文已截断，仅保留开头与命中意图关键词的段落）"
            description = (item.description or "").strip()
            if len(description) > 500:
                description = description[:500] + "…"
            blocks.append(
                "\n".join(
                    [
                        f"[候选 {index}]",
                        f"url: {item.url}",
                        f"title: {item.title}",
                        f"description: {description or '（无摘要）'}",
                        "content:",
                        content or "（无正文）",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _context(self, items: Sequence[ArticleItem]) -> dict[str, str]:
        intent = self.intent_config.intent
        tags = self.intent_config.tags
        return {
            "intent_description": intent.description or "（本次未配置自然语言意图，请以参考标签为准）",
            "intent_examples_include": self._format_examples(
                intent.examples_include, "（未提供正例）"
            ),
            "intent_examples_exclude": self._format_examples(
                intent.examples_exclude, "（未提供反例）"
            ),
            "intent_preferences": intent.preferences or "（未提供软性偏好）",
            "tags_must_include": self._format_terms(tags.must_include, "（未配置）"),
            "tags_boost": self._format_terms(tags.boost, "（未配置）"),
            "tags_exclude": self._format_terms(tags.exclude, "（未配置）"),
            "conflict_policy": self.intent_config.conflict_policy,
            "conflict_policy_instruction": self.conflict_instruction,
            "score_threshold": f"{self.intent_config.score_threshold:.2f}",
            "candidate_count": str(len(items)),
            "content_max_chars": str(self.llm_config.content_max_chars),
            "candidates": self._render_candidates(items),
        }

    def render(self, items: Sequence[ArticleItem]) -> str:
        if not items:
            raise PromptError("没有候选内容，无法渲染 Prompt")
        context = self._context(items)
        return _PLACEHOLDER_RE.sub(lambda match: context[match.group(1)], self.template)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


# ---------------------------------------------------------------------------
# 精筛
# ---------------------------------------------------------------------------


class LLMFilter:
    """把候选批次交给 DeepSeek 精筛。"""

    def __init__(
        self,
        client: DeepSeekClient,
        renderer: PromptRenderer,
        intent_config: FilterIntentConfig,
        llm_config: LLMConfig,
        *,
        stats: PipelineStats | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.renderer = renderer
        self.intent_config = intent_config
        self.llm_config = llm_config
        self.stats = stats
        self.log = log or (lambda _msg: None)
        #: 由 runner 按 observability.prompt_preview_chars 注入；0 表示不打印 Prompt
        self.prompt_preview_chars = 0

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def batch_size(self) -> int:
        return max(1, self.llm_config.batch_size)

    # ---------------------------------------------------------------- 入口
    def filter(self, batch: Sequence[tuple[ArticleItem, TagSignals]]) -> FilterBatchOutcome:
        """返回本批的筛选结果；调用层错误（网络/鉴权）向上抛 LLMError，由 runner 降级。"""
        if not batch:
            return FilterBatchOutcome()
        items = [item for item, _signals in batch]
        signals_by_url = {item.url: signals for item, signals in batch}
        prompt = self.renderer.render(items)
        if self.prompt_preview_chars:
            self.log(f"Prompt 预览（前 {self.prompt_preview_chars} 字）:\n{prompt[: self.prompt_preview_chars]}")

        response = self.client.chat([{"role": "user", "content": prompt}], label=f"{len(items)} 条候选")

        try:
            raw_items = self._parse_json(response.content)
        except ParseError as exc:
            if self.stats:
                self.stats.llm_parse_failure(response.content)
            self.log(f"LLM 返回不符合约定（整批降级丢弃）: {exc} | 原始返回={response.content[:300]!r}")
            return FilterBatchOutcome(
                failures=[(item.url, f"LLM 返回解析失败，整批降级丢弃：{exc}") for item in items]
            )

        expected = {item.url: item for item in items}
        accepted: list[tuple[ArticleItem, TagSignals, dict[str, Any]]] = []
        failures: list[tuple[str, str]] = []
        seen: set[str] = set()

        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                failures.append((f"items[{index}]", "LLM 返回的 item 不是对象"))
                continue
            url = str(raw.get("url") or "").strip()
            if url not in expected:
                failures.append(
                    (url or f"items[{index}]", "LLM 返回的 url 不在本批输入中（疑似串号），整条丢弃")
                )
                continue
            if url in seen:
                failures.append((url, "LLM 对同一 url 返回了多次，重复项丢弃"))
                continue
            seen.add(url)
            fields, error = self._coerce_fields(raw)
            if error:
                failures.append((url, error))
                continue
            accepted.append((expected[url], signals_by_url[url], fields))

        missing = [url for url in expected if url not in seen]
        for url in missing:
            failures.append((url, "LLM 未返回该条候选的结果，按解析失败丢弃"))

        results = [
            self._decide(item, signals, fields) for item, signals, fields in accepted
        ]
        if self.stats:
            for result in results:
                self.stats.tag_hit(result.tags)
        return FilterBatchOutcome(results=results, failures=failures)

    # ---------------------------------------------------------------- 解析与校验
    @staticmethod
    def _parse_json(content: str) -> list[Any]:
        """严格 JSON：只接受纯 JSON 文本，不做代码块剥离或截断修补。"""
        text = (content or "").strip()
        if not text:
            raise ParseError("LLM 返回为空")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"严格 JSON 解析失败: {exc.msg} (line {exc.lineno} col {exc.colno})") from exc
        if isinstance(data, list):
            return data
        if not isinstance(data, Mapping):
            raise ParseError(f"顶层应为对象或数组，实际 {type(data).__name__}")
        items = data.get("items")
        if not isinstance(items, list):
            raise ParseError("缺少 items 数组")
        return items

    def _coerce_fields(self, raw: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        """逐字段校验 + 归一化；返回 ({字段}, "") 或 (None, 错误原因)。"""
        intent_matched = raw.get("intent_matched")
        if isinstance(intent_matched, str):
            lowered = intent_matched.strip().lower()
            if lowered in ("true", "false"):
                intent_matched = lowered == "true"
            else:
                return {}, f"intent_matched 不是布尔值: {intent_matched!r}"
        if not isinstance(intent_matched, bool):
            return {}, f"intent_matched 不是布尔值: {intent_matched!r}"

        score_raw = raw.get("score")
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float)):
            return {}, f"score 不是数字: {score_raw!r}"
        score = float(score_raw)
        clamped = min(1.0, max(0.0, score))
        if clamped != score:
            self.log(f"LLM 返回的 score={score} 越界，已裁剪到 {clamped}")

        tags_raw = raw.get("tags")
        if tags_raw is None:
            tags_raw = []
        if not isinstance(tags_raw, list):
            return {}, f"tags 不是数组: {tags_raw!r}"
        allowed = set(self.intent_config.tags.must_include) | set(
            self.intent_config.tags.boost
        )
        tags: list[str] = []
        unknown: list[str] = []
        for tag in tags_raw:
            if not isinstance(tag, str) or not tag.strip():
                continue
            tag = tag.strip()
            # tags 只能来自配置里的 must_include / boost，避免模型自造标签污染下游统计
            if tag not in allowed:
                unknown.append(tag)
                continue
            if tag not in tags:
                tags.append(tag)
        if unknown:
            self.log(f"LLM 返回了配置外的 Tag（未配置 Tag 时全部忽略），已忽略: {unknown}")

        reason = str(raw.get("reason") or "").strip()
        if not reason:
            reason = "（模型未给出理由）"

        matched_intent = str(raw.get("matched_intent") or "").strip()
        if not intent_matched:
            matched_intent = ""

        return (
            {
                "intent_matched": intent_matched,
                "score": clamped,
                "tags": tags,
                "reason": reason,
                "matched_intent": matched_intent,
                "title": str(raw.get("title") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
            },
            "",
        )

    # ---------------------------------------------------------------- 冲突裁决
    def _decide(
        self, item: ArticleItem, signals: TagSignals, fields: dict[str, Any]
    ) -> FilterResult:
        config = self.intent_config
        intent_matched = fields["intent_matched"]
        score = fields["score"]
        threshold = config.score_threshold
        intent_keep = intent_matched and score >= threshold

        tag_positive = bool(signals.must_include_hits or signals.boost_hits or fields["tags"])
        tag_negative = bool(signals.exclude_hits)
        tag_keep = tag_positive and not tag_negative

        conflict = ""
        if not config.tags.is_blank:
            if tag_positive and not intent_matched:
                conflict = (
                    f"命中 Tag {signals.tag_hits or fields['tags']}，但按自然语言意图判定不相关"
                )
            elif tag_negative and intent_matched:
                conflict = f"命中 exclude Tag {signals.exclude_hits}，但按自然语言意图判定相关"

        policy = config.conflict_policy
        if policy == "tags" and config.tags.is_blank:
            # 没配任何 Tag 时 tags 策略无法成立，退化为以意图为准
            policy = "natural_language"
            self.log("conflict_policy=tags 但未配置任何 Tag，本批退化为按自然语言意图判定")

        if policy == "natural_language":
            keep = intent_keep
        elif policy == "tags":
            keep = tag_keep
        else:  # drop_on_conflict
            keep = False if conflict else intent_keep

        reason = fields["reason"]
        if conflict:
            reason += f"｜冲突：{conflict}"
            if self.stats:
                self.stats.conflict(f"{item.url} {conflict}")
        if conflict or policy != "natural_language":
            reason += (
                f"｜按 conflict_policy={config.conflict_policy} 裁决："
                f"{'保留' if keep else '丢弃'}"
            )
        reason += f"（意图符合={intent_matched}, score={score:.2f}, 阈值={threshold:.2f}）"

        return FilterResult(
            url=item.url,
            intent_matched=intent_matched,
            is_relevant=keep,
            matched_intent=fields["matched_intent"] or ("overall" if intent_matched else ""),
            tags=fields["tags"],
            score=score,
            reason=reason,
            title=item.title,
            description=item.description,
            raw=dict(fields),
        )


class LegacyTagFilter:
    """只配 Tag、不写意图时的向后兼容路径：不调用 LLM，纯 Tag 判定。"""

    def __init__(
        self,
        intent_config: FilterIntentConfig,
        *,
        stats: PipelineStats | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.intent_config = intent_config
        self.stats = stats
        self.log = log or (lambda _msg: None)

    def filter(self, batch: Sequence[tuple[ArticleItem, TagSignals]]) -> FilterBatchOutcome:
        outcome = FilterBatchOutcome()
        # 只配了 exclude 时不做正向召回，只做排除（避免「全 drop」的意外行为）
        has_positive_tags = bool(
            self.intent_config.tags.must_include or self.intent_config.tags.boost
        )
        for item, signals in batch:
            exclude_hit = bool(signals.exclude_hits)
            keep = (
                (bool(signals.must_include_hits or signals.boost_hits) or not has_positive_tags)
                and not exclude_hit
            )
            score = 0.0
            if keep:
                score = min(
                    1.0,
                    0.5
                    + 0.25 * len(signals.must_include_hits)
                    + 0.1 * len(signals.boost_hits),
                )
            reason = (
                "legacy Tag 模式（config/filter_intent.yaml 未写 intent.description，"
                "不调用 LLM）："
                f"must_include={signals.must_include_hits or '-'}，"
                f"boost={signals.boost_hits or '-'}，"
                f"exclude={signals.exclude_hits or '-'}"
            )
            if exclude_hit:
                reason += "｜命中 exclude，按 Tag 规则丢弃"
            outcome.results.append(
                FilterResult(
                    url=item.url,
                    intent_matched=keep,
                    is_relevant=keep,
                    matched_intent="",
                    tags=signals.tag_hits,
                    score=round(score, 2),
                    reason=reason,
                    title=item.title,
                    description=item.description,
                    raw={"engine": "legacy_tag"},
                )
            )
            if self.stats:
                self.stats.tag_hit(signals.tag_hits)
        return outcome


__all__ = [
    "FilterBatchOutcome",
    "LLMFilter",
    "LLMError",
    "LegacyTagFilter",
    "ParseError",
    "PromptError",
    "PromptRenderer",
    "REQUIRED_PLACEHOLDERS",
]
