# -*- coding: utf-8 -*-
"""推送渠道配置注入（统一入口，避免各处重复读取 YAML）。

`send_wcom_msg` 的函数签名不变，只把 API_URL / SENDKEY / TIMEOUT 从配置文件注入。
注入可能被调用多次（main / Pipeline / Pusher / Notifier），因此只在配置变化时打日志。
"""

from __future__ import annotations

from typing import Callable

from settings import PushConfig, WeComPushConfig

#: 上一次注入的 (api_url, sendkey)，用于避免重复日志
_last_signature: tuple[str, str] | None = None


def _mask(sendkey: str) -> str:
    if len(sendkey) > 8:
        return sendkey[:4] + "*" * (len(sendkey) - 8) + sendkey[-4:]
    return "*" * len(sendkey)


def configure_send_channel(
    config: PushConfig,
    *,
    log: Callable[[str], None] | None = None,
) -> WeComPushConfig:
    """把 config/app.yaml 的 push.wecom 注入到原有推送模块（幂等）。"""
    global _last_signature
    import send_wcom_msg

    wecom = config.wecom
    sendkey = wecom.resolve_sendkey()
    send_wcom_msg.configure(
        api_url=wecom.api_url,
        sendkey=sendkey,
        timeout=wecom.timeout_seconds,
    )
    signature = (wecom.api_url, sendkey)
    if log and signature != _last_signature:
        _last_signature = signature
        if wecom.api_url:
            log(f"推送渠道已配置：{wecom.api_url} sendkey={_mask(sendkey) or '(空)'}")
        else:
            log("推送渠道未配置：请填写 config/app.yaml 的 push.wecom.api_url")
    return wecom
