# -*- coding: utf-8 -*-
"""DeepSeek 客户端（直接用 requests 调 HTTP，不引入 openai SDK）。

要点：
    * 只用一条 OpenAI 兼容的 /chat/completions 路径，便于以后换同协议服务；
    * 只对网络错误 / 429 / 5xx 重试，4xx 直接失败（参数或密钥问题重试无意义）；
    * 每次调用都记录耗时、token、成功/失败，交给 PipelineStats 汇总；
    * 密钥缺失时明确报错，绝不伪造筛选结果。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from pipeline.metrics import PipelineStats
from settings import LLMConfig

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """LLM 调用失败（含不可重试的错误）。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_seconds: float = 0.0
    attempts: int = 1
    status: int = 200


class DeepSeekClient:
    def __init__(
        self,
        config: LLMConfig,
        *,
        log: Callable[[str], None] | None = None,
        stats: PipelineStats | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.log = log or (lambda _msg: None)
        self.stats = stats
        self.session = session or requests.Session()
        self._api_key = config.resolve_api_key()

    # ---------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def require_key(self) -> None:
        if not self.available:
            raise LLMError(
                "DeepSeek API Key 未配置：请填写 config/app.yaml 的 llm.api_key，"
                f"或设置环境变量 {self.config.api_key_env}",
                retryable=False,
            )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.config.extra_headers)
        return headers

    # ---------------------------------------------------------------- 调用
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        label: str = "",
    ) -> LLMResponse:
        self.require_key()
        if self.stats is not None and self.stats.budget_exhausted:
            raise LLMError(
                f"单次运行 token 预算已用尽（已用 {self.stats.tokens_used} / 预算 "
                f"{self.stats.token_budget}），停止调用 AI；下次运行会继续处理",
                retryable=False,
            )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if self.config.json_response_format:
            payload["response_format"] = {"type": "json_object"}

        attempts = 0
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            attempts += 1
            started = time.monotonic()
            try:
                response = self.session.post(
                    self.config.endpoint,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException as exc:
                latency = time.monotonic() - started
                if self.stats:
                    self.stats.llm_call(latency, None, ok=False)
                last_error = LLMError(
                    f"请求 DeepSeek 失败: {type(exc).__name__}: {exc}", retryable=True
                )
                if attempt < self.config.max_retries:
                    self._sleep(attempt, label, reason=type(exc).__name__)
                    continue
                raise last_error from exc

            latency = time.monotonic() - started
            if response.status_code != 200:
                body = (response.text or "")[:400]
                retryable = response.status_code in _RETRYABLE_STATUS
                if self.stats:
                    self.stats.llm_call(latency, None, ok=False)
                last_error = LLMError(
                    f"DeepSeek 返回 HTTP {response.status_code}: {body}",
                    status=response.status_code,
                    retryable=retryable,
                )
                if retryable and attempt < self.config.max_retries:
                    self._sleep(attempt, label, reason=f"HTTP {response.status_code}")
                    continue
                raise last_error

            try:
                data = response.json()
            except ValueError as exc:
                if self.stats:
                    self.stats.llm_call(latency, None, ok=False)
                raise LLMError(f"DeepSeek 响应不是合法 JSON: {response.text[:200]!r}") from exc

            content = self._extract_content(data)
            usage = data.get("usage") or {}
            if self.stats:
                self.stats.llm_call(latency, usage, ok=True)
            self.log(
                f"LLM 调用成功{label and f'（{label}）'}：model={data.get('model') or self.config.model} "
                f"耗时={latency:.2f}s tokens={usage.get('total_tokens', '-')} 尝试={attempts}"
            )
            return LLMResponse(
                content=content,
                model=str(data.get("model") or self.config.model),
                usage={str(k): int(v) for k, v in usage.items() if isinstance(v, (int, float))},
                latency_seconds=latency,
                attempts=attempts,
                status=response.status_code,
            )

        raise last_error or LLMError("DeepSeek 调用失败（未知原因）")

    # ---------------------------------------------------------------- 内部
    def _sleep(self, attempt: int, label: str, *, reason: str) -> None:
        base = max(0.0, self.config.retry_backoff_seconds)
        delay = base * (2**attempt) + random.uniform(0, max(0.2, base * 0.5))
        self.log(f"LLM 重试等待 {delay:.1f}s（第 {attempt + 1} 次，原因 {reason}）{label and f' {label}'}")
        time.sleep(delay)

    @staticmethod
    def _extract_content(data: Any) -> str:
        if not isinstance(data, dict):
            raise LLMError("DeepSeek 响应结构异常（不是对象）")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"DeepSeek 响应缺少 choices: {str(data)[:200]}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LLMError("DeepSeek 响应缺少 message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("DeepSeek 返回内容为空")
        return content
