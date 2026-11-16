# -*- coding: utf-8 -*-
"""企业微信 send-post 推送封装。

请求地址与 sendkey 已外置到 config/app.yaml 的 push.wecom，由 pipeline 启动时调用
configure() 注入；模块级 API_URL / SENDKEY 仅作为兜底默认值，保持向后兼容。

# ================== 使用示例 =====================
# from send_wcom_msg import send_text, send_news
# resp = send_text("Hello, this is a test message.")
# resp = send_news("标题", "描述", "https://example.com", "https://example.com/pic.jpg")
# print("响应:", resp.text)
"""

import requests

# ============== 兜底配置（正常由 config/app.yaml 注入）==============
API_URL = ''
SENDKEY = ''
TIMEOUT = 15
# =================================================================


def configure(api_url: str | None = None, sendkey: str | None = None, timeout: float | None = None) -> dict:
    """注入推送渠道配置（幂等，可重复调用）。

    只改模块级配置，不改变任何 send_* 函数的签名，保持既有调用方式不变。
    """
    global API_URL, SENDKEY, TIMEOUT
    if api_url is not None:
        API_URL = str(api_url).strip()
    if sendkey is not None:
        SENDKEY = str(sendkey).strip()
    if timeout is not None:
        TIMEOUT = float(timeout)
    return current_config()


def current_config() -> dict:
    """返回当前生效的渠道配置（用于自检/日志，sendkey 需调用方自行脱敏）。"""
    return {"api_url": API_URL, "sendkey": SENDKEY, "timeout": TIMEOUT}


def _post(data: dict, files: dict | None = None):
    if not API_URL:
        raise RuntimeError(
            "推送地址未配置：请填写 config/app.yaml 的 push.wecom.api_url（sendkey 同理）"
        )
    return requests.post(API_URL, data=data, files=files, timeout=TIMEOUT)


def send_text(text: str):
    """发送纯文本消息"""
    data = {
        'sendkey': SENDKEY,
        'msgtype': 'text',
        'text': text
    }
    return _post(data)


def send_textcard(title: str, description: str, url: str, btntxt: str = "点击查看"):
    """发送文字卡片消息"""
    data = {
        'sendkey': SENDKEY,
        'msgtype': 'textcard',
        'title': title,
        'description': description,
        'url': url,
        'btntxt': btntxt
    }
    return _post(data)


def send_news(title: str, description: str, url: str, picurl: str):
    """发送图文卡片消息（带图片）"""
    data = {
        'sendkey': SENDKEY,
        'msgtype': 'news',
        'title': title,
        'description': description,
        'url': url,
        'picurl': picurl
    }
    return _post(data)


def send_image(image_path: str):
    """发送本地图片消息"""
    data = {
        'sendkey': SENDKEY,
        'msgtype': 'image'
    }
    with open(image_path, 'rb') as f:
        files = {'media': f}
        return _post(data, files=files)


def send_file(file_path: str):
    """发送本地文件消息"""
    data = {
        'sendkey': SENDKEY,
        'msgtype': 'file'
    }
    with open(file_path, 'rb') as f:
        files = {'media': f}
        return _post(data, files=files)
