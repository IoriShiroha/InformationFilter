# -*- coding: utf-8 -*-
"""流水线编排：爬虫 -> 预处理 -> 双层去重 -> 硬规则 -> 粗召回 -> AI 精筛 -> 落地 -> 幂等推送。

入口：
    python main.py                     # 正式跑（与 python run_pipeline.py 等价）
    python main.py --dry-run           # 只跑到粗筛，不调用 AI、不推送
    python main.py --setup-source URL  # 只给 URL，让 AI 生成并缓存解析规则
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from crawler.adapt import (
    AutoSourceResolver,
    ProfileStore,
    SourceAdaptError,
    TemplateError,
    fetch_page_item,
    load_template,
)
from crawler.ai_items import AiItemExtractor, ItemExtractError, ItemTemplateError, load_item_template
from crawler.health import SourceHealthStore
from crawler.http import HttpClient
from crawler.extract import extract_container_text
from crawler.sources import SourceConfig, SourceCrawler, load_sources
from models import ArticleItem, ModelError, RawItem, TagSignals, iso, simhash_to_hex, utcnow
from pipeline.channel import configure_send_channel
from pipeline.dedup import Deduplicator
from pipeline.embedder import HashingTfEmbedder
from pipeline.llm_client import DeepSeekClient, LLMError
from pipeline.llm_filter import (
    FilterBatchOutcome,
    LLMFilter,
    LegacyTagFilter,
    PromptError,
    PromptRenderer,
)
from pipeline.metrics import PipelineStats
from pipeline.notify import ErrorNotifier
from pipeline.prefilter import CoarseRecaller, HardRuleFilter
from pipeline.preprocess import clean_content, normalize_url, simhash_text
from pipeline.push import Pusher
from pipeline.storage import Storage
from settings import AppConfig, ConfigError, FilterIntentConfig, load_app_config, load_filter_intent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WS_RE = re.compile(r"\s+")


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def tidy(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _default_log(message: str) -> None:
    """默认走原有日志模块（不修改它，只复用它的入口）。"""
    from log_recording import Logger

    Logger().log(message)


def _console_log(message: str) -> None:
    """同时写日志文件与终端，方便人工盯流水线。"""
    _default_log(message)
    print(message, flush=True)


def _cli_log(verbose: bool) -> Callable[[str], None]:
    """默认保持旧行为的「只写日志文件」；交互式终端或 --verbose 时同时打印。"""
    if verbose or sys.stdout.isatty():
        return _console_log
    return _default_log


@dataclass(slots=True)
class _Prepared:
    item: ArticleItem | None
    error: str = ""


class Pipeline:
    """一次运行的完整流水线。依赖注入式构造，便于测试替换爬虫/LLM/推送。"""

    def __init__(
        self,
        app: AppConfig,
        intent: FilterIntentConfig,
        *,
        log: Callable[[str], None] | None = None,
        send_fn: Callable[..., Any] | None = None,
        notify_send_fn: Callable[[str], Any] | None = None,
        http: HttpClient | None = None,
        llm_client: DeepSeekClient | None = None,
        storage: Storage | None = None,
    ) -> None:
        self.app = app
        self.intent = intent
        self.log = log or _default_log
        self.observability = app.observability
        self.stats = PipelineStats(
            self.log,
            prefix=self.observability.event_prefix,
            enabled=self.observability.log_stage_counts or self.observability.log_summary,
        )
        self.mode = "legacy_tag" if intent.is_legacy_tag_mode else "llm"

        # 推送渠道（请求地址 / sendkey）从配置注入（幂等；Pusher 真实发送时还会再注入一次）
        configure_send_channel(app.push, log=self.log)
        for warning in (*app.warnings, *intent.warnings):
            self.log(f"配置警告：{warning}")

        self.storage = storage or Storage(
            resolve_path(app.storage.db_path),
            log=self.log,
            dropped_retention_days=app.storage.dropped_sample_retention_days,
            dropped_max_chars=app.storage.dropped_sample_max_chars,
        )
        self.http = http or HttpClient(app.crawl, log=self.log)
        self.dedup = Deduplicator(self.storage, app.dedup)
        self.embedder = HashingTfEmbedder(intent.prefilter.embedding_model)
        self.recaller = CoarseRecaller(intent, self.embedder, log=self.log)
        self.hard_rule = HardRuleFilter(app.preprocess, log=self.log)
        self.pusher = Pusher(send_fn, self.storage, app.push, stats=self.stats, log=self.log)
        self.notifier = ErrorNotifier(
            app.notify,
            send_fn=notify_send_fn,
            log=self.log,
            stats=self.stats,
            dry_run=app.push.dry_run,
            channel_ready=True if notify_send_fn is not None else app.push.wecom.configured,
        )
        self.llm_client = llm_client or DeepSeekClient(app.llm, log=self.log, stats=self.stats)
        self.source_store = ProfileStore(resolve_path(app.crawl.auto.profile_cache))
        self.source_resolver = AutoSourceResolver(
            self.llm_client, app.crawl.auto, store=self.source_store, log=self.log
        )
        self.source_health = SourceHealthStore(resolve_path(app.crawl.health_store))
        self.ai_items = AiItemExtractor(self.llm_client, app.crawl.auto, log=self.log)
        self.stats.token_budget = app.llm.max_tokens_per_run

        if self.mode == "llm":
            renderer = PromptRenderer(intent, app.llm, log=self.log)
            # 精筛正文按「意图关键词 + Tag」择段，控制 token
            renderer.excerpt_keywords = list(
                dict.fromkeys(
                    [
                        *self.recaller.intent_keywords(),
                        *intent.tags.must_include,
                        *intent.tags.boost,
                    ]
                )
            )
            self.filter: Any = LLMFilter(
                self.llm_client, renderer, intent, app.llm, stats=self.stats, log=self.log
            )
            self.filter.prompt_preview_chars = self.observability.prompt_preview_chars
        else:
            self.filter = LegacyTagFilter(intent, stats=self.stats, log=self.log)

    # ---------------------------------------------------------------- 主流程
    def run(
        self,
        *,
        limit: int | None = None,
        source_names: Sequence[str] | None = None,
        dry_run: bool = False,
        push: bool = True,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "mode": self.mode,
            "started_at": iso(utcnow()),
            "dry_run": dry_run,
        }
        self.log("=" * 24 + " 流水线启动 " + "=" * 24)
        # dry-run 时默认不打扰通知渠道（可在 notify.dry_run_silent 关闭该行为）
        self.notifier.dry_run = self.app.push.dry_run or dry_run
        self.log(
            f"筛选模式：{self.intent.describe()}；意图阈值={self.intent.score_threshold}；"
            f"冲突策略={self.intent.conflict_policy}"
        )
        if self.mode == "legacy_tag":
            self.log(
                "未配置 intent.description：进入 legacy Tag 模式（只按 Tag 判定、不调用 LLM），"
                "行为与旧版兼容"
            )
        if self.mode == "llm" and not dry_run and not self.filter.available:
            message = (
                "DeepSeek API Key 缺失（config/app.yaml 的 llm.api_key 或环境变量 "
                f"{self.app.llm.api_key_env}），流水线终止：不调用 AI、不推送，避免伪造结果"
            )
            self.log(message)
            summary["aborted"] = message
            self.notifier.notify("run_aborted", message)
            return summary

        raw_items = self._crawl(limit=limit, source_names=source_names)
        summary["crawled"] = len(raw_items)
        summary["list_items"] = getattr(self, "_last_list_items", len(raw_items))
        if limit:
            raw_items = raw_items[:limit]

        candidates = self._select_candidates(raw_items)
        summary["candidates"] = len(candidates)

        if dry_run:
            self.log(
                f"dry-run：爬虫 {len(raw_items)} 条，粗筛后候选 {len(candidates)} 条；"
                "跳过 AI 精筛与推送"
            )
            for item, signals in candidates:
                self.log(f"[dry-run] 候选 {item.url_norm} | {signals.as_text()}")
            summary["candidates_detail"] = [
                {
                    "url": item.url,
                    "url_norm": item.url_norm,
                    "title": item.title,
                    "signals": signals.as_text(),
                }
                for item, signals in candidates
            ]
            self._report(summary, dry_run=True)
            return summary

        decided = self._filter_all(candidates)
        relevant = [item for item in decided if item.is_relevant]
        summary["filtered"] = len(decided)
        summary["relevant"] = len(relevant)
        self.log(f"精筛完成：判定 {len(decided)} 条，保留 {len(relevant)} 条")

        if not push:
            self.log("--no-push 生效：跳过推送")
            summary["push"] = {"ok": 0, "failed": 0, "skipped": 0}
        else:
            outcomes = self.pusher.push(relevant)
            for outcome in outcomes:
                self.stats.record(
                    "push", kept=outcome.pushed, reason=outcome.reason if not outcome.pushed else ""
                )
            summary["push"] = {
                "ok": sum(1 for o in outcomes if o.pushed),
                "failed": sum(1 for o in outcomes if not o.pushed and not o.skipped),
                "skipped": sum(1 for o in outcomes if o.skipped),
                "items": [{"url": o.url, "reason": o.reason} for o in outcomes],
            }
            failed_outcomes = [o for o in outcomes if not o.pushed and not o.skipped]
            if failed_outcomes:
                sample = "；".join(f"{o.url}（{o.reason[:80]}）" for o in failed_outcomes[:3])
                self.notifier.notify(
                    "push_failure",
                    f"本次有 {len(failed_outcomes)} 条内容推送失败",
                    detail=f"示例：{sample}",
                )

        self._report(summary)
        return summary

    # ---------------------------------------------------------------- 抓取
    def _crawl(self, *, limit: int | None, source_names: Sequence[str] | None) -> list[RawItem]:
        try:
            sources = load_sources(resolve_path(self.app.sources_path), log=self.log)
        except ConfigError as exc:
            message = f"源配置文件本身不可用: {exc}"
            self.log(message)
            self.notifier.notify("config_error", message)
            return []
        enabled = [source for source in sources if source.enabled]
        if source_names:
            wanted = set(source_names)
            enabled = [source for source in enabled if source.name in wanted]
        if not enabled:
            message = (
                f"没有可用的爬虫源（共 {len(sources)} 个源，0 个能跑）；"
                "请检查 config/sources.yaml：只填 URL 的源需要 url + auto_selectors: true，"
                "手写源需要 selectors.list_item；上面若有「已跳过」警告，按提示修正即可"
            )
            self.log(message)
            self.notifier.notify("crawl_failure", message)
            return []

        items: list[RawItem] = []
        raw_total = 0
        for source in enabled:
            url_norm = normalize_url(source.url)
            cooling = self.source_health.cooling(url_norm)
            if cooling is not None:
                self.log(
                    f"源 {source.name} 连续失败 {cooling.failures} 次，熔断冷却至 "
                    f"{cooling.cooldown_until}，本次跳过（crawl.source_failure_threshold 可调）"
                )
                self.stats.extra.setdefault("skipped_sources", []).append(source.name)
                continue

            crawled, problem, extracted, raw_count = self._crawl_one(source)
            raw_total += raw_count
            if extracted:
                self.source_health.record_success(url_norm)
                if source.needs_rules:
                    self.source_resolver.note_success(source)
            else:
                health = self.source_health.record_failure(
                    url_norm,
                    problem or "未抽到条目",
                    threshold=self.app.crawl.source_failure_threshold,
                    cooldown_minutes=self.app.crawl.source_cooldown_minutes,
                )
                if source.needs_rules:
                    self.source_resolver.note_empty(source)
                if health.cooldown_until:
                    message = (
                        f"源 {source.name} 连续失败 {health.failures} 次，已熔断至 "
                        f"{health.cooldown_until}：{health.last_error}"
                    )
                    self.log(message)
                    self.notifier.notify("crawl_failure", message)

            self.log(f"源 {source.name} 抓取到 {len(crawled)} 条")
            items.extend(crawled)
            if limit and len(items) >= limit:
                break
        self._last_list_items = raw_total
        self.log(f"抓取阶段完成：列表页共 {raw_total} 条，抓取详情后 {len(items)} 条原始条目")
        return items

    def _crawl_one(self, source: SourceConfig) -> tuple[list[RawItem], str, bool, int]:
        """抓一个源：列表 -> 粗筛 -> 只给通过的抓详情。

        返回 (条目, 问题描述, 是否成功抽到列表条目, 列表条目数)。
        """
        prepared, problem = self._prepare_source(source)
        if prepared is None:
            self.log(f"源 {source.name} 规则不可用，进入兜底链路（{problem}）")
            items = self._fallback_items(source)
            if not items and problem:
                self.notifier.notify(
                    "source_adapt_failure" if source.auto_selectors else "crawl_failure", problem
                )
            return items, problem, False, 0

        crawler = SourceCrawler(prepared, self.http, self.app.crawl, log=self.log)
        try:
            raw_items = crawler.fetch_list()
        except Exception as exc:  # 单个源失败不影响其它源
            problem = f"源 {source.name} 抓取异常: {type(exc).__name__}: {exc}"
            self.log(problem)
            self.notifier.notify("crawl_failure", problem)
            return [], problem, False, 0

        if not raw_items:
            items = self._fallback_items(prepared)
            return items, problem or "未抽到条目", False, 0

        survivors = self._list_screen(prepared, raw_items)
        if not survivors:
            self.log(f"源 {source.name} 列表页 {len(raw_items)} 条均未通过粗筛，跳过详情抓取")
            return [], problem, True, len(raw_items)
        if len(survivors) != len(raw_items):
            self.log(
                f"源 {source.name} 列表页粗筛：{len(raw_items)} -> {len(survivors)} 条进入详情抓取"
            )
        items = crawler.attach_details(survivors)
        return items, problem, True, len(raw_items)

    def _list_screen(self, source: SourceConfig, items: Sequence[RawItem]) -> list[RawItem]:
        """列表页阶段先筛：URL 去重 + 标题/摘要黑名单 + 宽松粗召回。

        通过者才去抓详情页；这里只做「明显不值得抓」的淘汰，避免误杀。
        """
        if not self.app.crawl.list_prefilter:
            return list(items)

        survivors: list[RawItem] = []
        seen_in_run: set[str] = set()
        for raw in items:
            if raw.meta.get("via") == "page_fallback":
                survivors.append(raw)
                continue
            stage = "list_prefilter"
            reason = ""
            item = ArticleItem(
                url=(raw.url or "").strip(),
                title=tidy(raw.title),
                description=tidy(raw.description),
                source=raw.source,
                crawled_at=raw.crawled_at,
            )
            try:
                item.validate()
            except ModelError as exc:
                stage, reason = "list_prefilter", f"模型校验失败: {exc}"
            if not reason:
                item.url_norm = normalize_url(item.url, self.app.preprocess.strip_query_params)
                if item.url_norm in seen_in_run:
                    stage, reason = "list_prefilter", "同一次运行内出现重复 URL（归一化后相同）"
                seen_in_run.add(item.url_norm)
                if not reason:
                    url_check = self.dedup.check_url(item.url_norm)
                    if url_check.duplicate:
                        stage, reason = "list_prefilter_url", url_check.reason
                if not reason:
                    hard = self.hard_rule.check_list_fields(item)
                    if not hard.keep:
                        stage, reason = "list_prefilter_hard", hard.reason
                if not reason:
                    decision = self.recaller.evaluate_list_level(item)
                    if not decision.keep:
                        stage, reason = decision.stage, decision.reason

            self.stats.record(stage, kept=not reason, reason=reason)
            if reason:
                self._drop(stage, reason, item.url, item.title, item.url_norm)
                continue
            raw.meta["list_passed"] = True
            survivors.append(raw)
        return survivors

    def _fallback_items(self, source: SourceConfig) -> list[RawItem]:
        """选择器抽不到条目时的两层兜底：AI 直接抽条目 -> 整页当候选。"""
        auto = self.app.crawl.auto
        if auto.item_fallback:
            try:
                items = self.ai_items.extract(source, self.http)
                if items:
                    self.log(
                        f"源 {source.name} 选择器未抽到条目，已改用 AI 直接抽取：{len(items)} 条"
                    )
                    self.stats.extra.setdefault("fallbacks", {}).setdefault(
                        source.name, []
                    ).append("ai_items")
                    return self._fetch_details(source, items)
            except (ItemExtractError, LLMError) as exc:
                self.log(f"源 {source.name} AI 条目抽取失败：{exc}")

        if auto.page_fallback:
            item = fetch_page_item(source, self.http)
            if item is not None:
                self.log(
                    f"源 {source.name} 降级为整页候选：只对整页文本做筛选，"
                    "推送的是列表页 URL（可在 crawl.auto.page_fallback 关闭）"
                )
                self.stats.extra.setdefault("fallbacks", {}).setdefault(
                    source.name, []
                ).append("page")
                return [item]
        return []

    def _fetch_details(self, source: SourceConfig, items: list[RawItem]) -> list[RawItem]:
        """AI 只给了条目元信息，正文仍需按详情页抓一次（与手写源链路一致）。"""
        if not self.app.crawl.fetch_detail:
            return items
        for item in items:
            if not item.url:
                continue
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
                continue
            item.html = result.text
            item.meta["detail_fetched"] = True
            detail_selector = (source.selectors or {}).get("detail_content", "")
            if detail_selector:
                text = extract_container_text(result.text, detail_selector)
                if text:
                    item.content_raw = text
        return items

    def _prepare_source(self, source: SourceConfig) -> tuple[SourceConfig | None, str]:
        """有选择器就直接用；只有 URL 的 html 源先做自动适配（AI 生成规则 + 缓存）。

        返回 (可抓取的源, 问题描述)。返回 (None, 原因) 时由调用方决定走兜底还是放弃，
        这样「规则生成失败」不会直接掐掉该源——还有 AI 直接抽条目 / 整页兜底。
        """
        if not source.needs_rules:
            return source, ""  # RSS / JSON 接口不需要解析规则
        if source.has_list_selector:
            return source, ""
        if not source.auto_selectors:
            message = (
                f"源 {source.name} 没有 list_item 选择器，也没有开启 auto_selectors"
            )
            self.log(message)
            return None, message
        try:
            resolved = self.source_resolver.resolve(source, self.http)
        except SourceAdaptError as exc:
            message = f"源 {source.name} 自动适配失败: {exc}"
            self.log(message)
            return None, message
        except TemplateError as exc:
            message = f"源 {source.name} 自动适配模板不可用: {exc}"
            self.log(message)
            return None, message
        except Exception as exc:  # 适配过程的任何意外都不能拖垮整条流水线
            message = f"源 {source.name} 自动适配异常: {type(exc).__name__}: {exc}"
            self.log(message)
            return None, message
        mode = self.source_resolver.last_mode or "auto"
        self.stats.extra.setdefault("auto_sources", {})[source.name] = mode
        self.log(
            f"源 {source.name} 解析规则就绪（mode={mode}）："
            f"list_item={resolved.selectors.get('list_item')}"
        )
        return resolved, ""
    # ---------------------------------------------------------------- 预处理 + 去重 + 粗筛
    def _select_candidates(self, raw_items: Sequence[RawItem]) -> list[tuple[ArticleItem, TagSignals]]:
        candidates: list[tuple[ArticleItem, TagSignals]] = []
        seen_in_run: set[str] = set()

        for raw in raw_items:
            self.stats.record("crawl", kept=True)
            prepared = self._prepare(raw)
            if prepared.item is None:
                self.stats.record("preprocess", kept=False, reason=prepared.error)
                self._drop("preprocess", prepared.error, raw.url, raw.title)
                continue
            item = prepared.item
            self.stats.record("preprocess", kept=True)

            if item.url_norm in seen_in_run:
                reason = "同一次运行内出现重复 URL（归一化后相同）"
                self.stats.record("dedup_url", kept=False, reason=reason)
                self._drop("dedup_url", reason, item.url, item.title, item.url_norm)
                continue
            seen_in_run.add(item.url_norm)

            url_check = self.dedup.check_url(item.url_norm)
            if url_check.duplicate:
                self.stats.record("dedup_url", kept=False, reason=url_check.reason)
                self._drop("dedup_url", url_check.reason, item.url, item.title, item.url_norm)
                continue
            self.stats.record("dedup_url", kept=True)

            content_check = self.dedup.check_content(item.url_norm, item.content_hash)
            if content_check.duplicate:
                self.stats.record("dedup_content", kept=False, reason=content_check.reason)
                self._drop("dedup_content", content_check.reason, item.url, item.title, item.url_norm)
                continue
            self.stats.record("dedup_content", kept=True)

            # 整页兜底候选：整页里必然混着多条内容，用黑名单/粗召回判断「整页」没有意义，
            # 直接交给 AI 判断这一页值不值得推（推送的是列表页 URL）。
            if item.meta.get("via") == "page_fallback":
                self.log(f"整页兜底候选，跳过硬规则与粗召回，直接交给 AI 判断: {item.url}")
                self.stats.record("prefilter", kept=True, reason="整页兜底跳过")
                candidates.append((item, TagSignals(recalled=True, reason="page_fallback")))
                continue

            hard = self.hard_rule.check(item)
            if not hard.keep:
                self.stats.record("hard_rule", kept=False, reason=hard.reason)
                self._drop("hard_rule", hard.reason, item.url, item.title, item.url_norm)
                continue
            self.stats.record("hard_rule", kept=True)

            recall, signals = self.recaller.evaluate(item)
            if not recall.keep:
                self.stats.record("prefilter", kept=False, reason=recall.reason)
                self._drop(recall.stage or "prefilter", recall.reason, item.url, item.title, item.url_norm)
                continue
            self.stats.record("prefilter", kept=True)
            self.log(f"粗筛通过 {item.url} | {signals.as_text()}")
            candidates.append((item, signals))

        self.log(f"粗筛阶段完成：候选 {len(candidates)} 条进入 AI 精筛")
        return candidates

    def _prepare(self, raw: RawItem) -> _Prepared:
        source_text = raw.content_raw or raw.html
        cleaned = clean_content(source_text, max_chars=self.app.preprocess.max_content_chars)
        item = ArticleItem(
            url=(raw.url or "").strip(),
            title=tidy(raw.title),
            description=tidy(raw.description),
            picurl=(raw.picurl or "").strip(),
            content=cleaned,
            source=raw.source,
            crawled_at=raw.crawled_at,
            meta=dict(raw.meta),
        )
        try:
            item.validate()
        except ModelError as exc:
            return _Prepared(None, f"模型校验失败: {exc}")
        item.url_norm = normalize_url(item.url, self.app.preprocess.strip_query_params)
        item.content_hash = simhash_to_hex(simhash_text(item.content)) if item.content else ""
        return _Prepared(item)

    # ---------------------------------------------------------------- 精筛
    def _filter_all(
        self, candidates: Sequence[tuple[ArticleItem, TagSignals]]
    ) -> list[ArticleItem]:
        decided: list[ArticleItem] = []
        if not candidates:
            return decided
        batch_size = self.filter.batch_size if self.mode == "llm" else max(1, len(candidates))
        batches = [
            list(candidates[start : start + batch_size])
            for start in range(0, len(candidates), batch_size)
        ]
        self.log(f"精筛阶段：{len(candidates)} 条候选，分 {len(batches)} 批（每批 {batch_size}）")

        for index, batch in enumerate(batches, 1):
            by_url = {item.url: item for item, _signals in batch}
            try:
                outcome: FilterBatchOutcome = self.filter.filter(batch)
            except LLMError as exc:
                budget_hit = self.stats.budget_exhausted
                reason = f"LLM 调用失败，本批降级丢弃（不记为已处理，下次重试）: {exc}"
                self.log(f"第 {index}/{len(batches)} 批{reason}")
                self.notifier.notify(
                    "llm_failure",
                    (
                        f"token 预算用尽，跳过剩余 {len(batches) - index + 1} 批候选"
                        if budget_hit
                        else f"第 {index}/{len(batches)} 批 LLM 调用失败，{len(batch)} 条内容降级丢弃（下次重试）"
                    ),
                    detail=f"原因：{str(exc)[:500]}",
                )
                for item, _signals in batch:
                    self.stats.record("llm", kept=False, reason="LLM 调用失败（整批降级）")
                    self._drop("llm_error", str(exc)[:180], item.url, item.title, item.url_norm)
                if budget_hit:
                    # 成本护栏：预算用尽就不再继续烧钱，剩余候选不记为已处理，下次运行重试
                    self.stats.record("llm_budget", kept=False, reason="token 预算用尽，跳过剩余批次")
                    self.log("token 预算已用尽：跳过剩余批次（这些条目未记为已处理）")
                    break
                continue
            self._apply_batch(index, len(batches), outcome, by_url, decided)
        return decided

    def _apply_batch(
        self,
        index: int,
        total: int,
        outcome: FilterBatchOutcome,
        by_url: dict[str, ArticleItem],
        decided: list[ArticleItem],
    ) -> None:
        for url, reason in outcome.failures:
            item = by_url.get(url)
            self.stats.record("llm", kept=False, reason=reason)
            self._drop(
                "llm",
                reason,
                item.url if item else url,
                item.title if item else "",
                item.url_norm if item else self._safe_norm(url),
            )
        if outcome.failures:
            sample = outcome.failures[0][1]
            self.notifier.notify(
                "parse_failure",
                f"第 {index}/{total} 批有 {len(outcome.failures)} 条 LLM 返回解析/校验失败，已按降级丢弃",
                detail=f"首条原因：{sample[:400]}",
            )

        kept = 0
        for result in outcome.results:
            item = by_url.get(result.url)
            if item is None:  # 理论上不会发生（filter 已校验 url 一致性）
                continue
            item.apply_filter_result(result)
            self.stats.record(
                "llm", kept=result.is_relevant, reason="" if result.is_relevant else result.reason
            )
            self.storage.save_filter_result(item, engine=self.mode, raw_response=result.raw)
            # 已产出结论的内容才写去重记录，避免 LLM 临时失败后再也不重试
            self.dedup.remember(item, stage="relevant" if result.is_relevant else "irrelevant")
            decided.append(item)
            if result.is_relevant:
                kept += 1
            self.log(
                f"筛选[{'保留' if result.is_relevant else '丢弃'}] {item.url} | "
                f"score={result.score:.2f} | tags={result.tags or '-'} | {result.reason[:160]}"
            )
        self.log(
            f"第 {index}/{total} 批完成：返回 {len(outcome.results)} 条，保留 {kept} 条，"
            f"解析/校验失败 {len(outcome.failures)} 条"
        )

    # ---------------------------------------------------------------- 日志辅助
    def _drop(
        self,
        stage: str,
        reason: str,
        url: str = "",
        title: str = "",
        url_norm: str = "",
    ) -> None:
        self.log(f"丢弃[{stage}] {title or url} | {reason}")
        if not self.observability.log_dropped_samples:
            return
        self.storage.add_dropped_sample(
            url_norm or self._safe_norm(url), stage, reason, url=url, title=title
        )

    @staticmethod
    def _safe_norm(url: str) -> str:
        """非 http(s) 的占位串（如 items[0]）不做归一化，避免写进脏主键。"""
        target = (url or "").strip()
        if not target.lower().startswith(("http://", "https://")):
            return ""
        try:
            return normalize_url(target)
        except Exception:
            return ""

    def _report(self, summary: dict[str, Any], *, dry_run: bool = False) -> None:
        if self.mode == "llm":
            self.stats.log_llm()
        self.stats.log_tags()
        self.stats.log_stages()
        pruned = self.storage.prune()
        if pruned:
            self.log(f"清理过期丢弃样本 {pruned} 条")
        stats_summary = self.stats.summary()
        summary["stats"] = stats_summary
        summary["storage"] = self.storage.stats()
        summary["notify"] = {
            "enabled": self.app.notify.enabled,
            "sent": self.notifier.sent,
            "skipped": self.notifier.skipped,
            "failed": self.notifier.failed,
        }
        if self.observability.log_summary:
            self.stats.log_summary(self.mode + ("/dry-run" if dry_run else ""))
            self.log(
                f"存储：URL {summary['storage']['seen_urls']}，指纹 "
                f"{summary['storage']['fingerprints']}，精筛结果 "
                f"{summary['storage']['filter_results']}，已推送 {summary['storage']['pushed']}"
            )
            self.log(self.notifier.describe())

        failures = (
            self.stats.llm_failures
            + self.stats.llm_parse_failures
            + (summary.get("push") or {}).get("failed", 0)
        )
        if self.app.notify.include_summary and failures and not dry_run:
            push_info = summary.get("push") or {}
            self.notifier.notify(
                "summary",
                f"本次运行共 {failures} 处失败：LLM 失败 {self.stats.llm_failures}，"
                f"解析失败 {self.stats.llm_parse_failures}，推送失败 {push_info.get('failed', 0)}",
                detail=(
                    f"爬虫 {summary.get('crawled', 0)} 条 / "
                    f"候选 {summary.get('candidates', 0)} 条 / "
                    f"保留 {summary.get('relevant', 0)} 条"
                ),
            )
        self.log("=" * 24 + " 流水线结束 " + "=" * 24)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def check_config(
    app_path: str | None = None,
    intent_path: str | None = None,
    *,
    sources_path: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """加载并校验全部配置（含两个 Prompt 模板、源配置、推送渠道、通知），供 --check-config 使用。"""
    log = log or _console_log
    app = load_app_config(app_path)
    if sources_path:
        app.sources_path = sources_path
    intent = load_filter_intent(intent_path)
    sources = load_sources(resolve_path(app.sources_path), log=log)
    renderer = PromptRenderer(intent, app.llm, log=log)
    load_template(app.crawl.auto.prompt_template)  # 规则生成模板也做一次占位符校验
    load_item_template(app.crawl.auto.item_prompt_template)  # 条目抽取模板同理
    enabled = [source.name for source in sources if source.enabled]
    auto_enabled = [source.name for source in sources if source.enabled and source.auto_selectors]

    log("配置校验通过：")
    for warning in (*app.warnings, *intent.warnings):
        log(f"  ⚠ 配置警告：{warning}")
    log(f"  筛选模式：{intent.describe()}")
    log(
        f"  阈值={intent.score_threshold} 冲突策略={intent.conflict_policy} "
        f"粗召回阈值={intent.prefilter.similarity_threshold} 模型={intent.prefilter.embedding_model}"
    )
    log(
        f"  Tag：must_include={intent.tags.must_include} boost={intent.tags.boost} "
        f"exclude={intent.tags.exclude}"
    )
    log(
        f"  DeepSeek：{app.llm.endpoint} model={app.llm.model} "
        f"key={'已配置' if app.llm.resolve_api_key() else '未配置'}"
    )
    log(f"  筛选模板：{renderer.template_path}（占位符校验通过）")
    log(
        f"  规则生成模板：{resolve_path(app.crawl.auto.prompt_template)}（占位符校验通过，"
        f"max_attempts={app.crawl.auto.max_attempts}）"
    )
    log(
        "  抽取兜底："
        f"AI 直接抽条目={'开' if app.crawl.auto.item_fallback else '关'}，"
        f"整页兜底={'开' if app.crawl.auto.page_fallback else '关'}"
    )
    log(f"  爬虫源：共 {len(sources)} 个，启用 {enabled or '无'}，其中只填 URL 的源 {auto_enabled or '无'}")
    log(
        f"  推送渠道：{app.push.wecom.api_url or '未配置'} "
        f"sendkey={'已配置' if app.push.wecom.resolve_sendkey() else '未配置'}"
    )
    log(
        f"  报错通知：{'开启' if app.notify.enabled else '关闭'} "
        f"events={app.notify.events}"
    )
    log(f"  规则缓存：{resolve_path(app.crawl.auto.profile_cache)}")
    log(f"  数据库：{resolve_path(app.storage.db_path)}")
    log(
        "  成本/稳定性护栏："
        f"精筛正文={app.llm.content_max_chars} 字，"
        f"单次运行 token 预算={app.llm.max_tokens_per_run or '不限'}，"
        f"列表页先粗筛={'开' if app.crawl.list_prefilter else '关'}，"
        f"页面缓存={'开' if app.crawl.page_cache else '关'}，"
        f"源熔断=连续 {app.crawl.source_failure_threshold} 次失败冷却 "
        f"{app.crawl.source_cooldown_minutes} 分钟，"
        f"规则自愈=连续 {app.crawl.auto.self_heal_after or '关闭'} 次 0 条"
    )
    log(
        f"  源类型：html_list / json_api / rss（JSON 源 {sum(1 for s in sources if s.type == 'json_api')} 个，"
        f"RSS 源 {sum(1 for s in sources if s.type == 'rss')} 个）"
    )
    return {
        "mode": intent.describe(),
        "sources": [source.name for source in sources],
        "enabled_sources": enabled,
        "auto_sources": auto_enabled,
        "api_key_configured": bool(app.llm.resolve_api_key()),
        "push_configured": app.push.wecom.configured,
        "notify_enabled": app.notify.enabled,
    }


def setup_source(
    url: str,
    *,
    app: AppConfig,
    name: str = "",
    log: Callable[[str], None] | None = None,
    http: HttpClient | None = None,
    client: DeepSeekClient | None = None,
) -> dict[str, Any]:
    """只给一个列表页 URL：生成并缓存解析规则，打印可直接粘贴的源配置片段。"""
    import yaml

    log = log or _console_log
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"--setup-source 需要 http(s) 列表页地址，实际 {url!r}")
    if not app.crawl.auto.enabled:
        raise ConfigError("crawl.auto.enabled=false，无法自动生成解析规则；请先开启")
    load_template(app.crawl.auto.prompt_template)  # 提前校验模板

    client = client or DeepSeekClient(app.llm, log=log)
    if not client.available:
        log("未配置 DeepSeek 密钥：将只用启发式规则（效果不如 AI，建议配置密钥）")
    store = ProfileStore(resolve_path(app.crawl.auto.profile_cache))
    resolver = AutoSourceResolver(client, app.crawl.auto, store=store, log=log)
    http = http or HttpClient(app.crawl, log=log)

    source_name = name or "auto-source"
    source = SourceConfig(
        name=source_name,
        url=url,
        enabled=True,
        auto_selectors=True,
        max_items=app.crawl.max_items_per_source,
    )
    resolved = resolver.resolve(source, http)
    mode = resolver.last_mode or "auto"

    snippet = {
        "sources": [
            {
                "name": source_name,
                "enabled": True,
                "type": "html_list",
                "url": url,
                "auto_selectors": True,
                "max_items": app.crawl.max_items_per_source,
            }
        ]
    }
    log(f"解析规则已生成（mode={mode}）并写入缓存 {store.path.name}，可把下面片段贴进 config/sources.yaml：")
    log(yaml.safe_dump(snippet, allow_unicode=True, sort_keys=False).rstrip())
    log("当前规则（想冻结到 YAML 里就把 selectors 一起粘进去）：")
    log(yaml.safe_dump({"selectors": resolved.selectors}, allow_unicode=True, sort_keys=False).rstrip())
    return {"mode": mode, "selectors": resolved.selectors, "snippet": snippet}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="爬虫 -> 预处理去重 -> AI 意图筛选 -> 幂等推送 流水线",
    )
    parser.add_argument("--config", default=None, help="运行时配置，默认 config/app.yaml")
    parser.add_argument("--intent", default=None, help="筛选意图配置，默认 config/filter_intent.yaml")
    parser.add_argument("--sources", default=None, help="覆盖 app.yaml 里的 sources_path")
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少条原始条目")
    parser.add_argument("--source", action="append", default=None, help="只跑指定源（可重复）")
    parser.add_argument("--dry-run", action="store_true", help="只跑到粗筛，不调用 AI、不推送")
    parser.add_argument("--no-push", action="store_true", help="精筛后不推送")
    parser.add_argument("--check-config", action="store_true", help="只校验配置后退出")
    parser.add_argument(
        "--setup-source",
        default=None,
        metavar="URL",
        help="只给列表页 URL：让 AI 生成并缓存解析规则后退出",
    )
    parser.add_argument("--name", default=None, help="配合 --setup-source 指定源名")
    parser.add_argument("--verbose", action="store_true", help="日志同时打印到终端")
    args = parser.parse_args(list(argv) if argv is not None else None)

    cli_log = _cli_log(args.verbose)
    notifier = None
    try:
        app = load_app_config(args.config)
        if args.sources:
            app.sources_path = args.sources
        # 先注入推送渠道：这样「配置错误」这类早期失败也能真的发出文字通知
        configure_send_channel(app.push, log=cli_log)
        for warning in app.warnings:
            cli_log(f"配置警告：{warning}")
        notifier = ErrorNotifier(
            app.notify, log=cli_log, channel_ready=app.push.wecom.configured
        )
        if args.setup_source:
            try:
                setup_source(args.setup_source, app=app, name=args.name or "", log=cli_log)
            except SourceAdaptError as exc:
                message = f"自动适配失败：{exc}"
                _default_log(message)
                notifier.notify("source_adapt_failure", message)
                print(message, file=sys.stderr)
                return 1
            return 0
        intent = load_filter_intent(args.intent)
        for warning in intent.warnings:
            cli_log(f"配置警告：{warning}")
        if args.check_config:
            check_config(args.config, args.intent, sources_path=args.sources)
            return 0
        pipeline = Pipeline(app, intent, log=cli_log)
        summary = pipeline.run(
            limit=args.limit, source_names=args.source, dry_run=args.dry_run, push=not args.no_push
        )
    except (ConfigError, PromptError, TemplateError, ItemTemplateError) as exc:
        message = f"配置错误，流水线未启动：{exc}"
        _default_log(message)
        if notifier is not None:
            notifier.notify("config_error", message)
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if summary.get("aborted"):
        return 1
    push_info = summary.get("push") or {}
    if push_info.get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
