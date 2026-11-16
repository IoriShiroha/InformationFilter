# -*- coding: utf-8 -*-
"""双层去重：URL 归一化查重 + 正文 simhash 近重复检测。

* URL 层：去 utm/锚点后按 url_norm 查近 N 天记录（config: dedup.url_window_days）；
* 内容层：64 位 simhash 与近 N 天指纹比汉明距离（dedup.content_window_days /
  dedup.hamming_threshold）。

命中任一层即视为重复，由 runner 记日志并写「丢弃样本」，不再进入 AI 精筛。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import ArticleItem, simhash_from_hex
from pipeline.preprocess import hamming_distance
from pipeline.storage import Storage
from settings import DedupConfig


@dataclass(slots=True)
class DedupOutcome:
    duplicate: bool
    stage: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class Deduplicator:
    def __init__(self, storage: Storage, config: DedupConfig) -> None:
        self.storage = storage
        self.config = config

    # ---------------------------------------------------------------- URL
    def check_url(self, url_norm: str) -> DedupOutcome:
        if self.config.url_window_days <= 0:
            return DedupOutcome(False)
        seen = self.storage.url_seen_within(url_norm, self.config.url_window_days)
        if not seen:
            return DedupOutcome(False)
        return DedupOutcome(
            True,
            stage="dedup_url",
            reason=(
                f"URL 在近 {self.config.url_window_days} 天内已处理过"
                f"（首次 {seen.get('first_seen', '')}，最近 {seen.get('last_seen', '')}，"
                f"累计 {seen.get('hit_count', 1)} 次）"
            ),
            detail=seen,
        )

    # ---------------------------------------------------------------- 内容
    def check_content(self, url_norm: str, content_hash: str) -> DedupOutcome:
        if self.config.content_window_days <= 0:
            return DedupOutcome(False)
        fingerprint = simhash_from_hex(content_hash)
        if not fingerprint:
            return DedupOutcome(False)

        rows = self.storage.fingerprints_within(
            self.config.content_window_days, self.config.fingerprint_max_scan
        )
        threshold = self.config.hamming_threshold
        best_distance = 65
        best_url = ""
        for row in rows:
            if row.get("url_norm") == url_norm:
                continue  # 同一条内容自己的指纹不算
            other = simhash_from_hex(row.get("simhash") or "")
            if other is None:
                continue
            distance = hamming_distance(fingerprint, other)
            if distance < best_distance:
                best_distance = distance
                best_url = row.get("url_norm") or ""
                if distance == 0:
                    break
        if best_distance <= threshold:
            return DedupOutcome(
                True,
                stage="dedup_content",
                reason=(
                    f"正文与近 {self.config.content_window_days} 天内记录近重复"
                    f"（汉明距离 {best_distance} <= 阈值 {threshold}，命中的是 {best_url}）"
                ),
                detail={"distance": best_distance, "matched_url_norm": best_url, "scanned": len(rows)},
            )
        return DedupOutcome(False, detail={"distance": best_distance, "scanned": len(rows)})

    # ---------------------------------------------------------------- 记忆
    def remember(self, item: ArticleItem, stage: str = "decided") -> None:
        """内容产出了确定结论后才落去重记录，避免 LLM 临时失败导致再也不重试。"""
        self.storage.remember(item, stage)
