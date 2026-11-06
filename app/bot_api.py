"""Bot API 的小工具：给「转发群组」和设置页用，不依赖长轮询器。

`telegram_bot.py` 里那套是长轮询专用的（自己管 client 和 offset），
这里只要「发一次请求、把错误翻成人话」，两者互不影响。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.proxy import ProxyConfig

TELEGRAM_API_BASE = "https://api.telegram.org"

_RETRY_RE = re.compile(r"retry after (\d+)", re.I)

# Bot API 的报错是英文短句，直接甩给用户看不懂；这里翻成「该去做什么」
_HINTS = (
    ("chat not found", "找不到这个群：确认 bot 已经加入，且用的是 -100 开头的那个 id"),
    ("not enough rights", "bot 没有发消息权限：把它设成管理员，或放开群里的发言限制"),
    ("have no rights", "bot 没有发消息权限：把它设成管理员"),
    ("not a member", "bot 不在这个群里：先把 bot 拉进去"),
    ("message to copy", "要复制的消息找不到了（私聊里可能已被删除或撤回）"),
    ("group chat was upgraded", "这个群升级成超级群了，id 变了，重新绑一次目标"),
    ("too many requests", "Bot API 限流（约 20 条/分钟/群），等一会儿再试"),
    ("chat_write_forbidden", "bot 不能在这个群发言：检查群权限或管理员设置"),
    ("bot was blocked", "你把 bot 拉黑或停用了，先在私聊里解除"),
)


class BotApiError(RuntimeError):
    """Bot API 调用失败，message 已经是人话。"""


class BotApiRateLimit(BotApiError):
    """被限流（HTTP 429），retry_after 是 Telegram 要求的等待秒数。"""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after or 0))


def _retry_after(description: str, payload: dict | None = None) -> float:
    """从 “Too Many Requests: retry after 12” 或 parameters 里取秒数。"""
    params = (payload or {}).get("parameters") or {}
    try:
        seconds = float(params.get("retry_after") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        return seconds
    match = _RETRY_RE.search(description or "")
    return float(match.group(1)) if match else 5.0


def _friendly(description: str) -> str:
    lowered = (description or "").lower()
    for key, hint in _HINTS:
        if key in lowered:
            return f"{hint}（Telegram：{description[:140]}）"
    return description or "Bot API 调用失败"


def _proxy_url(proxy: ProxyConfig | None) -> str:
    if proxy is not None and getattr(proxy, "enabled", False):
        return proxy.to_url()
    return ""


async def call(token: str, method: str, payload: dict | None = None, *,
               proxy: ProxyConfig | None = None, timeout: float = 25.0) -> Any:
    """调一个 Bot API 方法，返回 result。失败抛 BotApiError。"""
    if not token:
        raise BotApiError("还没有配置 Bot Token（设置 → Telegram Bot）")
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    kwargs: dict[str, Any] = {"trust_env": False, "timeout": timeout}
    proxy_url = _proxy_url(proxy)
    if proxy_url:
        kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.post(url, json=payload or {})
    try:
        data = response.json()
    except ValueError:
        raise BotApiError(f"Bot API 返回的不是 JSON（HTTP {response.status_code}）") from None
    if not data.get("ok"):
        description = str(data.get("description") or "")
        # 429 要单独拿出来：调用方得等 retry_after 再重试，而不是当成普通失败
        if data.get("error_code") == 429 or "too many requests" in description.lower():
            raise BotApiRateLimit(
                f"Bot API 限流：{description[:120]}",
                _retry_after(description, data),
            )
        raise BotApiError(_friendly(description))
    return data.get("result")


async def get_me(token: str, proxy: ProxyConfig | None = None) -> dict:
    return await call(token, "getMe", proxy=proxy) or {}


async def get_chat(token: str, chat_id: int, proxy: ProxyConfig | None = None) -> dict:
    return await call(token, "getChat", {"chat_id": int(chat_id)}, proxy=proxy) or {}


async def send_text(token: str, chat_id: int, text: str,
                    proxy: ProxyConfig | None = None) -> dict:
    return await call(
        token, "sendMessage",
        {"chat_id": int(chat_id), "text": text, "disable_web_page_preview": True},
        proxy=proxy,
    ) or {}


async def copy_messages(token: str, to_chat: int, from_chat: int,
                        message_ids: list[int],
                        proxy: ProxyConfig | None = None) -> list:
    """把私聊里的消息复制到目标群。

    相册用 copyMessages 一次发完（否则会散成好几条）；单条退回 copyMessage，
    早期 Bot API 版本没有批量接口也不至于报错。
    """
    ids = [int(mid) for mid in message_ids]
    if not ids:
        return []
    if len(ids) > 1:
        result = await call(
            token, "copyMessages",
            {"chat_id": int(to_chat), "from_chat_id": int(from_chat),
             "message_ids": ids},
            proxy=proxy,
        )
        # copyMessages 返回 MessageId 列表；数量对得上就当成成功
        return result or []
    result = await call(
        token, "copyMessage",
        {"chat_id": int(to_chat), "from_chat_id": int(from_chat),
         "message_id": ids[0]},
        proxy=proxy,
    )
    return [result] if result else []
