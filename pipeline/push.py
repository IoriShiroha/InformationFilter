# -*- coding: utf-8 -*-
"""推送层：复用原有 send_wcom_msg.send_news，接口与调用方式不变。

* 只推送 is_relevant 的内容；
* 幂等三层：进程内集合 -> 持久化 push_log -> 原有推送模块；
* 新增字段（matched_intent / tags / reason）只拼进 description，作为附加信息，
  不改变 send_news(title, description, url, picurl) 的签名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from models import ArticleItem, iso
from pipeline.metrics import PipelineStats
from pipeline.storage import Storage
from settings import PushConfig


def default_send_news(title: str, description: str, url: str, picurl: str):
    """默认推送实现：直接调用原有模块，不改动其代码与接口。"""
    from send_wcom_msg import send_news

    return send_news(title, description, url, picurl)


@dataclass(slots=True)
class PushOutcome:
    url: str
    pushed: bool
    skipped: bool = False
    reason: str = ""
    response: str = ""


class Pusher:
    def __init__(
        self,
        send_fn: Callable[..., Any] | None,
        storage: Storage,
        config: PushConfig,
        *,
        stats: PipelineStats | None = None,
        log: Callable[[str], None] | None = None,
        channel: str = "wecom_news",
    ) -> None:
        self.storage = storage
        self.config = config
        self.stats = stats
        self.log = log or (lambda _msg: None)
        self.channel = config.channels[0] if config.channels else channel
        self._pushed_in_process: set[str] = set()
        self.send_fn = send_fn or default_send_news
        if send_fn is None:
            # 真实渠道：把 app.yaml 的请求地址/sendkey 注入原有推送模块
            from pipeline.channel import configure_send_channel

            configure_send_channel(config, log=self.log)

    # ---------------------------------------------------------------- 组装
    def compose_title(self, item: ArticleItem) -> str:
        title = (item.title or "").strip() or item.url
        return title if len(title) <= 64 else title[:63] + "…"

    def compose_description(self, item: ArticleItem) -> str:
        matched = (
            (item.matched_intent or "overall") if item.intent_matched else "未命中自然语言意图"
        )
        tags = "、".join(item.tags) if item.tags else "无"
        summary = (item.description or item.content or "").strip()
        summary = summary.replace("\n", " ")
        if len(summary) > 120:
            summary = summary[:120] + "…"
        reason = (item.reason or "").strip()[: self.config.reason_max_chars]

        lines = [
            f"匹配意图：{matched}",
            f"命中标签：{tags}",
            f"筛选理由：{reason}",
            f"摘要：{summary}",
            f"来源：{item.source or '-'} · {iso(item.crawled_at)[:10]}",
            f"链接：{item.url}",
        ]
        picurl = item.validate_picurl()
        if picurl:
            lines.append(f"封面：{picurl}")
        text = "\n".join(lines)
        limit = max(50, self.config.description_max_chars)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    # ---------------------------------------------------------------- 推送
    def push(self, items: list[ArticleItem]) -> list[PushOutcome]:
        outcomes: list[PushOutcome] = []
        sent = 0
        for item in items:
            if not item.is_relevant:
                continue
            item.ensure_url_norm()
            url_norm = item.url_norm

            if self.config.max_per_run >= 0 and sent >= self.config.max_per_run:
                outcomes.append(
                    PushOutcome(item.url, False, True, f"本次运行推送上限 {self.config.max_per_run} 已用完")
                )
                self._skip_log(item, outcomes[-1])
                continue
            if url_norm in self._pushed_in_process:
                outcomes.append(PushOutcome(item.url, False, True, "进程内已推送过，跳过"))
                self._skip_log(item, outcomes[-1])
                continue
            if self.storage.is_pushed(url_norm):
                outcomes.append(PushOutcome(item.url, False, True, "持久化层记录已推送过，跳过"))
                self._skip_log(item, outcomes[-1])
                continue
            if not self.config.enabled:
                outcomes.append(PushOutcome(item.url, False, True, "push.enabled=false，跳过推送"))
                self._skip_log(item, outcomes[-1])
                continue
            if self.config.dry_run:
                outcomes.append(PushOutcome(item.url, False, True, "push.dry_run=true，只记录不发送"))
                self._skip_log(item, outcomes[-1])
                continue

            title = self.compose_title(item)
            description = self.compose_description(item)
            try:
                response = self.send_fn(title, description, item.url, item.validate_picurl())
            except Exception as exc:  # 原有推送模块内部无异常处理，这里兜住
                reason = f"推送异常: {type(exc).__name__}: {exc}"
                outcomes.append(PushOutcome(item.url, False, False, reason))
                if self.stats:
                    self.stats.push_failed += 1
                self.log(f"推送失败 {item.url} | {reason}")
                continue

            status_code = getattr(response, "status_code", None)
            body = getattr(response, "text", "") or ""
            ok = status_code is None or 200 <= int(status_code) < 300
            excerpt = body.strip().replace("\n", " ")[:200]
            if ok:
                sent += 1
                self._pushed_in_process.add(url_norm)
                self.storage.mark_pushed(url_norm, item.url, self.channel, excerpt, status="ok")
                if self.stats:
                    self.stats.push_ok += 1
                self.log(f"推送成功 {item.url} | status={status_code} | {excerpt}")
                outcomes.append(PushOutcome(item.url, True, False, "", excerpt))
            else:
                if self.stats:
                    self.stats.push_failed += 1
                self.log(f"推送失败 {item.url} | status={status_code} | {excerpt}")
                outcomes.append(
                    PushOutcome(item.url, False, False, f"推送接口返回 HTTP {status_code}: {excerpt}")
                )
        return outcomes

    def _skip_log(self, item: ArticleItem, outcome: PushOutcome) -> None:
        if self.stats:
            self.stats.push_skipped += 1
        self.log(f"推送跳过 {item.url} | {outcome.reason}")
