# -*- coding: utf-8 -*-
"""重要报错通知：复用原有 send_wcom_msg.send_text 发文字消息。

约束：
    * 只通知配置里白名单的事件，避免刷屏；
    * 同事件同内容在 min_interval_seconds 内只发一次；
    * 单次运行最多 max_per_run 条；
    * 通知失败只记日志，绝不影响主流程（也不递归通知）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from models import iso, utcnow
from pipeline.metrics import PipelineStats
from settings import NotifyConfig

#: 事件名 -> 中文标签（事件名本身受 settings.NOTIFY_EVENTS 约束）
EVENT_LABELS = {
    "run_aborted": "运行终止",
    "config_error": "配置错误",
    "crawl_failure": "抓取失败",
    "source_adapt_failure": "源适配失败",
    "llm_failure": "LLM 调用失败",
    "parse_failure": "返回解析失败",
    "push_failure": "推送失败",
    "summary": "运行汇总",
}


def default_send_text(text: str):
    """默认通知实现：直接调用原有模块，接口不变。"""
    from send_wcom_msg import send_text

    return send_text(text)


class ErrorNotifier:
    def __init__(
        self,
        config: NotifyConfig,
        *,
        send_fn: Callable[[str], Any] | None = None,
        log: Callable[[str], None] | None = None,
        stats: PipelineStats | None = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
        channel_ready: bool | None = None,
    ) -> None:
        self.config = config
        self._using_default_channel = send_fn is None
        self._channel_ready_override = channel_ready
        self.send_fn = send_fn or default_send_text
        self.log = log or (lambda _msg: None)
        self.stats = stats
        self.dry_run = dry_run
        self.clock = clock
        self.sent = 0
        self.skipped = 0
        self.failed = 0
        self._last_sent: dict[str, float] = {}
        self._warned_unconfigured = False

    def _channel_ready(self) -> bool:
        """默认渠道尚未配置请求地址时，跳过通知（只警告一次），避免反复失败刷日志。"""
        if self._channel_ready_override is not None:
            return self._channel_ready_override
        if not self._using_default_channel:
            return True
        try:
            import send_wcom_msg

            return bool(send_wcom_msg.current_config().get("api_url"))
        except Exception:  # pragma: no cover - 导入失败时按就绪处理，由发送层报错
            return True

    # ---------------------------------------------------------------- 入口
    def notify(self, event: str, message: str, *, detail: str = "") -> bool:
        """发一条文字通知；返回是否真的发出去了。"""
        if not self.config.enabled:
            return False
        if event not in self.config.events:
            self.skipped += 1
            return False
        if self.dry_run and self.config.dry_run_silent:
            self.skipped += 1
            self.log(f"dry-run 静默：跳过通知 [{event}] {message[:120]}")
            return False
        if self.config.max_per_run >= 0 and self.sent >= self.config.max_per_run:
            self.skipped += 1
            self.log(f"通知条数达上限 {self.config.max_per_run}，跳过 [{event}] {message[:120]}")
            return False

        if not self._channel_ready():
            self.skipped += 1
            if not self._warned_unconfigured:
                self._warned_unconfigured = True
                self.log(
                    "通知渠道未配置：请确认 config/app.yaml 里 api_url 与 sendkey 写在 "
                    "push.wecom 下（注意缩进，不是 push 直下、也不是 llm 下）；"
                    f"已跳过 {event} 等文字通知，配置后即可收到"
                )
            return False

        key = f"{event}:{(message or '').strip()[:160]}"
        now = self.clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.config.min_interval_seconds:
            self.skipped += 1
            self.log(f"通知去重（{self.config.min_interval_seconds}s 内同内容），跳过 [{event}]")
            return False

        text = self._compose(event, message, detail)
        try:
            response = self.send_fn(text)
        except Exception as exc:  # 通知渠道自身故障，只记日志
            self.failed += 1
            self.log(f"发送文字通知失败 [{event}]: {type(exc).__name__}: {exc}")
            return False

        status_code = getattr(response, "status_code", None)
        if status_code is not None and not 200 <= int(status_code) < 300:
            self.failed += 1
            self.log(f"发送文字通知失败 [{event}]: HTTP {status_code}")
            return False

        self.sent += 1
        self._last_sent[key] = now
        self.log(f"已发送文字通知 [{event}] {message[:160]}")
        return True

    # ---------------------------------------------------------------- 组装
    def _compose(self, event: str, message: str, detail: str) -> str:
        label = EVENT_LABELS.get(event, event)
        stamp = iso(utcnow())[:19].replace("T", " ")
        lines = [f"【信息筛选】【{label}】{stamp}", message.strip()]
        if detail.strip():
            lines.append(detail.strip())
        text = "\n".join(line for line in lines if line)
        limit = max(50, self.config.text_max_chars)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    # ---------------------------------------------------------------- 汇总
    def describe(self) -> str:
        return f"通知 sent={self.sent} skipped={self.skipped} failed={self.failed}"
