# -*- coding: utf-8 -*-
"""持久化层（sqlite，标准库）：URL 去重、simhash 指纹、筛选结果、丢弃样本、推送幂等。

为什么用 sqlite 而不是 JSON 文件：
    * 需要「近 N 天」时间窗口查询与唯一约束（url_norm 幂等写入）；
    * 进程重启后仍能挡住重复推送；
    * 不引入额外依赖。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from models import ArticleItem, iso, utcnow

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS seen_urls (
        url_norm   TEXT PRIMARY KEY,
        url        TEXT NOT NULL,
        title      TEXT,
        source     TEXT,
        first_seen TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        hit_count  INTEGER NOT NULL DEFAULT 1,
        stage      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_seen_urls_last_seen ON seen_urls(last_seen)",
    """
    CREATE TABLE IF NOT EXISTS content_fingerprints (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        url_norm   TEXT NOT NULL,
        simhash    TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_created ON content_fingerprints(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_url ON content_fingerprints(url_norm)",
    """
    CREATE TABLE IF NOT EXISTS filter_results (
        url_norm       TEXT PRIMARY KEY,
        url            TEXT NOT NULL,
        title          TEXT,
        description    TEXT,
        picurl         TEXT,
        content        TEXT,
        content_hash   TEXT,
        source         TEXT,
        crawled_at     TEXT,
        intent_matched INTEGER,
        matched_intent TEXT,
        tags           TEXT,
        score          REAL,
        is_relevant    INTEGER,
        reason         TEXT,
        engine         TEXT,
        raw_response   TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_filter_results_updated ON filter_results(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS dropped_samples (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        url_norm   TEXT,
        url        TEXT,
        title      TEXT,
        stage      TEXT NOT NULL,
        reason     TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dropped_created ON dropped_samples(created_at)",
    """
    CREATE TABLE IF NOT EXISTS push_log (
        url_norm   TEXT PRIMARY KEY,
        url        TEXT NOT NULL,
        channel    TEXT NOT NULL,
        pushed_at  TEXT NOT NULL,
        response   TEXT,
        status     TEXT
    )
    """,
)


class Storage:
    """sqlite 持久层。所有方法都自带连接，因此可以在不同阶段的函数里随意调用。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        log: Callable[[str], None] | None = None,
        dropped_retention_days: int = 7,
        dropped_max_chars: int = 200,
    ) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log or (lambda _msg: None)
        self.dropped_retention_days = max(0, int(dropped_retention_days))
        self.dropped_max_chars = max(20, int(dropped_max_chars))
        with closing(self._connect()) as conn, conn:
            for statement in _SCHEMA:
                conn.execute(statement)

    # ---------------------------------------------------------------- 基础设施
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:  # pragma: no cover - 只读介质等极端情况
            pass
        return conn

    @staticmethod
    def _since(days: int) -> str:
        return iso(utcnow() - timedelta(days=max(0, days)))

    # ---------------------------------------------------------------- URL 去重
    def url_seen_within(self, url_norm: str, days: int) -> dict[str, Any] | None:
        """近 days 天内是否见过该归一化 URL。days<=0 视为不做 URL 去重。"""
        if not url_norm or days <= 0:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT url, title, first_seen, last_seen, hit_count, stage "
                "FROM seen_urls WHERE url_norm = ? AND last_seen >= ?",
                (url_norm, self._since(days)),
            ).fetchone()
        return dict(row) if row else None

    def remember_url(
        self,
        url_norm: str,
        url: str,
        title: str = "",
        source: str = "",
        stage: str = "",
        at: str | None = None,
    ) -> None:
        now = at or iso(utcnow())
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO seen_urls (url_norm, url, title, source, first_seen, last_seen, hit_count, stage)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(url_norm) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    hit_count = seen_urls.hit_count + 1,
                    title     = COALESCE(excluded.title, seen_urls.title),
                    source    = COALESCE(excluded.source, seen_urls.source),
                    stage     = COALESCE(excluded.stage, seen_urls.stage)
                """,
                (url_norm, url, title, source, now, now, stage),
            )

    # ---------------------------------------------------------------- 内容指纹
    def add_fingerprint(self, url_norm: str, simhash: str, at: str | None = None) -> None:
        if not url_norm or not simhash:
            return
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO content_fingerprints (url_norm, simhash, created_at) VALUES (?, ?, ?)",
                (url_norm, simhash, at or iso(utcnow())),
            )

    def fingerprints_within(self, days: int, limit: int = 5000) -> list[dict[str, Any]]:
        if days <= 0:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT url_norm, simhash, created_at FROM content_fingerprints "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (self._since(days), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def remember(self, item: ArticleItem, stage: str = "") -> None:
        """把一条「已产出结论」的内容落成去重记录（URL + 指纹）。"""
        item.ensure_url_norm()
        item.ensure_content_hash()
        self.remember_url(item.url_norm, item.url, item.title, item.source, stage)
        if item.content_hash:
            self.add_fingerprint(item.url_norm, item.content_hash)

    # ---------------------------------------------------------------- 精筛结果
    def save_filter_result(
        self,
        item: ArticleItem,
        *,
        engine: str = "",
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        """按 url_norm 幂等 upsert（重复写入只更新，不产生新行）。"""
        item.ensure_url_norm()
        now = iso(utcnow())
        record = item.to_record()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO filter_results (
                    url_norm, url, title, description, picurl, content, content_hash, source,
                    crawled_at, intent_matched, matched_intent, tags, score, is_relevant, reason,
                    engine, raw_response, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_norm) DO UPDATE SET
                    url            = excluded.url,
                    title          = excluded.title,
                    description    = excluded.description,
                    picurl         = excluded.picurl,
                    content        = excluded.content,
                    content_hash   = excluded.content_hash,
                    source         = excluded.source,
                    crawled_at     = excluded.crawled_at,
                    intent_matched = excluded.intent_matched,
                    matched_intent = excluded.matched_intent,
                    tags           = excluded.tags,
                    score          = excluded.score,
                    is_relevant    = excluded.is_relevant,
                    reason         = excluded.reason,
                    engine         = excluded.engine,
                    raw_response   = excluded.raw_response,
                    updated_at     = excluded.updated_at
                """,
                (
                    record["url_norm"],
                    record["url"],
                    record["title"],
                    record["description"],
                    record["picurl"],
                    record["content"],
                    record["content_hash"],
                    record["source"],
                    record["crawled_at"],
                    record["intent_matched"],
                    record["matched_intent"],
                    json.dumps(record["tags"], ensure_ascii=False),
                    record["score"],
                    record["is_relevant"],
                    record["reason"],
                    engine,
                    json.dumps(raw_response or {}, ensure_ascii=False)[:4000],
                    now,
                    now,
                ),
            )

    def get_filter_result(self, url_norm: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM filter_results WHERE url_norm = ?", (url_norm,)
            ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 丢弃样本
    def add_dropped_sample(
        self,
        url_norm: str,
        stage: str,
        reason: str,
        url: str = "",
        title: str = "",
    ) -> None:
        """短期保留「被丢弃样本 + reason」，便于调优意图描述与阈值。"""
        if self.dropped_retention_days <= 0:
            return
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO dropped_samples (url_norm, url, title, stage, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    url_norm,
                    url,
                    (title or "")[: self.dropped_max_chars],
                    stage,
                    (reason or "")[: self.dropped_max_chars * 2],
                    iso(utcnow()),
                ),
            )

    def recent_dropped(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT url_norm, url, title, stage, reason, created_at "
                "FROM dropped_samples ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self) -> int:
        """按保留期清理丢弃样本，返回删除行数。"""
        if self.dropped_retention_days <= 0:
            return 0
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM dropped_samples WHERE created_at < ?",
                (self._since(self.dropped_retention_days),),
            )
            return cursor.rowcount or 0

    # ---------------------------------------------------------------- 推送幂等
    def is_pushed(self, url_norm: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM push_log WHERE url_norm = ? LIMIT 1", (url_norm,)
            ).fetchone()
        return row is not None

    def mark_pushed(
        self,
        url_norm: str,
        url: str,
        channel: str,
        response: str = "",
        status: str = "ok",
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO push_log (url_norm, url, channel, pushed_at, response, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_norm) DO UPDATE SET
                    url       = excluded.url,
                    channel   = excluded.channel,
                    pushed_at = excluded.pushed_at,
                    response  = excluded.response,
                    status    = excluded.status
                """,
                (url_norm, url, channel, iso(utcnow()), (response or "")[:2000], status),
            )

    # ---------------------------------------------------------------- 观测
    def stats(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            def count(sql: str, args: Iterable[Any] = ()) -> int:
                row = conn.execute(sql, tuple(args)).fetchone()
                return int(row[0]) if row else 0

            return {
                "seen_urls": count("SELECT COUNT(*) FROM seen_urls"),
                "fingerprints": count("SELECT COUNT(*) FROM content_fingerprints"),
                "filter_results": count("SELECT COUNT(*) FROM filter_results"),
                "relevant": count("SELECT COUNT(*) FROM filter_results WHERE is_relevant = 1"),
                "dropped_samples": count("SELECT COUNT(*) FROM dropped_samples"),
                "pushed": count("SELECT COUNT(*) FROM push_log"),
            }

    def tag_distribution(self, limit: int = 200) -> dict[str, int]:
        """已保留内容的 Tag 分布（按 url_norm 幂等表统计，用于调参）。"""
        buckets: dict[str, int] = {}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT tags FROM filter_results WHERE is_relevant = 1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        for row in rows:
            try:
                tags = json.loads(row["tags"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, str) and tag:
                    buckets[tag] = buckets.get(tag, 0) + 1
        return dict(sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0])))
