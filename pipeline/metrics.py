# -*- coding: utf-8 -*-
"""可观测性：各阶段条数、丢弃原因、LLM 调用次数/耗时、解析失败数、Tag 分布。

所有输出统一走原有的 log_recording.Logger()（通过注入的 log 回调），
不改动日志模块本身的行为。
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

_REASON_MAX = 90


@dataclass(slots=True)
class StageStat:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


class PipelineStats:
    def __init__(self, log: Callable[[str], None], *, prefix: str = "[pipeline]", enabled: bool = True) -> None:
        self.log = log
        self.prefix = prefix
        self.enabled = enabled
        self.stages: dict[str, StageStat] = {}
        self.llm_calls = 0
        self.llm_failures = 0
        self.llm_parse_failures = 0
        self.llm_latency_total = 0.0
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        #: 单次运行 token 预算（prompt+completion 累计；0 = 不限）
        self.token_budget = 0
        self.tag_hits: Counter[str] = Counter()
        self.conflicts = 0
        self.push_ok = 0
        self.push_failed = 0
        self.push_skipped = 0
        self.started_at = time.monotonic()
        self.extra: dict[str, Any] = {}

    # ---------------------------------------------------------------- 基础设施
    def _emit(self, message: str) -> None:
        self.log(f"{self.prefix} {message}")

    def stage(self, name: str) -> StageStat:
        return self.stages.setdefault(name, StageStat())

    def record(self, stage: str, *, kept: bool, reason: str = "") -> None:
        stat = self.stage(stage)
        stat.total += 1
        if kept:
            stat.kept += 1
        else:
            stat.dropped += 1
            if reason:
                stat.reasons[reason[:_REASON_MAX]] += 1

    # ---------------------------------------------------------------- 预算
    @property
    def tokens_used(self) -> int:
        return self.llm_prompt_tokens + self.llm_completion_tokens

    @property
    def budget_exhausted(self) -> bool:
        return self.token_budget > 0 and self.tokens_used >= self.token_budget

    # ---------------------------------------------------------------- LLM
    def llm_call(self, latency: float, usage: dict[str, int] | None = None, *, ok: bool = True) -> None:
        self.llm_calls += 1
        self.llm_latency_total += max(0.0, latency)
        if not ok:
            self.llm_failures += 1
        for key in ("prompt_tokens", "completion_tokens"):
            if usage and isinstance(usage.get(key), int):
                setattr(self, f"llm_{key}", getattr(self, f"llm_{key}") + usage[key])

    def llm_parse_failure(self, sample: str = "") -> None:
        self.llm_parse_failures += 1
        self._emit(f"LLM 返回解析失败（累计 {self.llm_parse_failures} 次）：{sample[:200]!r}")

    def tag_hit(self, tags: list[str]) -> None:
        self.tag_hits.update(t for t in tags if t)

    def conflict(self, note: str) -> None:
        self.conflicts += 1
        self._emit(f"意图/Tag 冲突：{note[:220]}")

    # ---------------------------------------------------------------- 报告
    @property
    def llm_avg_latency(self) -> float:
        return self.llm_latency_total / self.llm_calls if self.llm_calls else 0.0

    def stage_line(self, name: str) -> str:
        stat = self.stage(name)
        line = f"阶段[{name}] 共 {stat.total} 条，保留 {stat.kept}，丢弃 {stat.dropped}"
        if stat.reasons:
            top = "；".join(f"{reason} ×{count}" for reason, count in stat.reasons.most_common(5))
            line += f" | 丢弃原因：{top}"
        return line

    def log_stages(self) -> None:
        if not self.enabled:
            return
        for name in self.stages:
            self._emit(self.stage_line(name))

    def log_llm(self) -> None:
        if not self.enabled:
            return
        self._emit(
            f"LLM 调用 {self.llm_calls} 次（失败 {self.llm_failures}，解析失败 "
            f"{self.llm_parse_failures}），平均耗时 {self.llm_avg_latency:.2f}s，"
            f"tokens prompt={self.llm_prompt_tokens} completion={self.llm_completion_tokens}"
        )

    def log_tags(self) -> None:
        if not self.enabled:
            return
        if not self.tag_hits:
            self._emit("Tag 命中分布：无")
            return
        top = "，".join(f"{tag}×{count}" for tag, count in self.tag_hits.most_common(10))
        self._emit(f"Tag 命中分布：{top}")

    def summary(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(time.monotonic() - self.started_at, 2),
            "stages": {
                name: {
                    "total": stat.total,
                    "kept": stat.kept,
                    "dropped": stat.dropped,
                    "reasons": dict(stat.reasons),
                }
                for name, stat in self.stages.items()
            },
            "llm_calls": self.llm_calls,
            "llm_failures": self.llm_failures,
            "llm_parse_failures": self.llm_parse_failures,
            "llm_avg_latency": round(self.llm_avg_latency, 2),
            "llm_tokens": {
                "prompt": self.llm_prompt_tokens,
                "completion": self.llm_completion_tokens,
                "total": self.tokens_used,
                "budget": self.token_budget,
                "budget_exhausted": self.budget_exhausted,
            },
            "tag_distribution": dict(self.tag_hits),
            "conflicts": self.conflicts,
            "push": {
                "ok": self.push_ok,
                "failed": self.push_failed,
                "skipped": self.push_skipped,
            },
            **self.extra,
        }

    def log_summary(self, mode: str = "") -> None:
        if not self.enabled:
            return
        summary = self.summary()
        push = summary["push"]
        self._emit(
            f"汇总 模式={mode or '-'} 用时={summary['elapsed_seconds']}s "
            f"LLM 调用={summary['llm_calls']} 解析失败={summary['llm_parse_failures']} "
            f"冲突={summary['conflicts']} 推送 ok={push['ok']} 失败={push['failed']} 跳过={push['skipped']}"
        )
