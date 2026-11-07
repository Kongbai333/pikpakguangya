"""把监控群里的视频转发到自己创建的群组。

为什么用「转发」而不是下载重传：转发是服务端行为，不占本机带宽、不落盘，
几百 MB 的视频也只算一次 API 调用。

三个现实约束，都会在状态里写清楚，不会静默失败：

1. 来源群开了「禁止转发」（protected content）时 Telegram 直接拒，
   这种只能下载重传到目标群，本项目不做。
2. 相册（一条消息带多个视频）要按 `grouped_id` 一次性转，否则会散成好几条。
3. 转发有频率限制；`FLOOD_WAIT` 在阈值内由 Telethon 自动等待重试，
   超过阈值的会抛出来，这里记成失败原因。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import iso, utcnow

log = logging.getLogger("forward")

# 目标群和统计都存在 state 表里，按账号分开
DEFAULT_GROUP_TITLE = "PikPak 视频转发"
# 相册最多按这个窗口找同组消息（相册里消息 id 通常是连号的）
ALBUM_WINDOW = 20

# 已经转过的源消息（«chat_id:msg_id»），落库而不是只放内存：
# 重启后内存集合会空，而补偿回溯会把最近 60 分钟的视频再扫一遍 ——
# 不落库就会整批重复转发。
DONE_KEY_PREFIX = "forward.done:"
DONE_LIMIT = 3000

# bot 中转模式下，bot 把视频发到哪个群（全局一份：bot 只有一个）
RELAY_TARGET_KEY = "forward.bot.chat"
# bot 自己复制消息的统计与错误
RELAY_STATS_KEY = "forward.bot.stats"


def target_key(account_id: int) -> str:
    return f"forward.target:{int(account_id)}"


def stats_key(account_id: int) -> str:
    return f"forward.stats:{int(account_id)}"


def done_key(account_id: int) -> str:
    return f"{DONE_KEY_PREFIX}{int(account_id)}"


def _done_list(store, account_id: int) -> list[str]:
    data = store.get_json(done_key(account_id), []) or []
    return [str(x) for x in data] if isinstance(data, list) else []


def was_forwarded(store, account_id: int, chat_id: int, message_id: int) -> bool:
    """这条源消息是否已经转过了。"""
    mid = int(message_id or 0)
    if not mid:
        return False
    return f"{int(chat_id)}:{mid}" in set(_done_list(store, account_id))


def mark_forwarded(store, account_id: int, chat_id: int, message_ids) -> None:
    """记下已经转过的源消息（相册一次记一整组）。"""
    ids = []
    for value in message_ids or []:
        try:
            mid = int(value or 0)
        except (TypeError, ValueError):
            continue
        if mid > 0:
            ids.append(mid)
    if not ids:
        return
    data = _done_list(store, account_id)
    known = set(data)
    for mid in ids:
        key = f"{int(chat_id)}:{mid}"
        if key not in known:
            data.append(key)
            known.add(key)
    store.set_json(done_key(account_id), data[-DONE_LIMIT:])


def load_target(store, account_id: int) -> dict:
    data = store.get_json(target_key(account_id), {}) or {}
    return data if isinstance(data, dict) else {}


def save_target(store, account_id: int, *, chat_id: int, raw_id: int,
                access_hash: int | None, title: str, kind: str = "megagroup",
                mode: str = "direct", bot_username: str = "",
                bot_user_id: int = 0, link_source: bool = True) -> dict:
    """记下转发目标。

    存 `access_hash` 是因为采集用的是另一条长连接（独立 client），
    只有 id 没有 hash 时 Telethon 解析不出实体，转发会报
    「Could not find the input entity」。

    mode：`direct` = 采集号直接发到群；`bot` = 采集号转发给 bot，由 bot 发到群。
    bot 模式不需要 access_hash，但要存 bot 的 username 才能转发过去。
    """
    data = {
        "chat_id": int(chat_id),
        "raw_id": int(raw_id),
        "access_hash": int(access_hash) if access_hash else 0,
        "title": title or "",
        "kind": kind,
        "mode": "bot" if mode == "bot" else "direct",
        "bot_username": bot_username or "",
        "bot_user_id": int(bot_user_id or 0),
        "link_source": bool(link_source),
        "created_at": iso(utcnow()),
    }
    store.set_json(target_key(account_id), data)
    return data


def clear_target(store, account_id: int) -> None:
    store.set_json(target_key(account_id), {})


def load_stats(store, account_id: int) -> dict:
    data = store.get_json(stats_key(account_id), {}) or {}
    return data if isinstance(data, dict) else {}


def note_result(store, account_id: int, *, ok: bool, error: str = "",
                source: str = "", count: int = 0) -> dict:
    """累加转发统计，顺手记下最后一次结果，页面上能看出「到底在不在转」。"""
    data = load_stats(store, account_id)
    forwarded = int(data.get("forwarded") or 0)
    failed = int(data.get("failed") or 0)
    if ok:
        forwarded += 1
    else:
        failed += 1
    data.update({
        "forwarded": forwarded,
        "failed": failed,
        "messages": int(data.get("messages") or 0) + int(count or 0),
        "last_at": iso(utcnow()),
        "last_source": source or "",
        "last_error": "" if ok else (error or "")[:300],
    })
    store.set_json(stats_key(account_id), data)
    return data


def detect_video(message: Any) -> bool:
    """这条消息里是不是**真视频**（含圆视频、以文件发出的视频）。

    表情包要优先排除：Telegram 的 video 表情（.webm）同时带
    `DocumentAttributeSticker` 和 `DocumentAttributeVideo`，
    Telethon 的 `message.video` 对两者都返回真 —— 先看 video 就会把表情
    当视频转走。GIF（DocumentAttributeAnimated）也不算，它在 Bot API 那边
    是 `animation` 字段，不排除的话两边口径又对不上。

    口径必须和 `telegram_bot._is_video_message`（Bot API 侧）保持一致，
    否则会变成「采集侧转了、bot 丢掉」这种一半成功的状态。
    """
    if getattr(message, "sticker", None) is not None:
        return False
    if getattr(message, "gif", None) is not None:
        return False
    if getattr(message, "video_note", None) is not None:
        return True
    if getattr(message, "video", None) is not None:
        return True

    doc = getattr(message, "document", None)
    if doc is None:
        return False
    if not str(getattr(doc, "mime_type", "") or "").startswith("video/"):
        return False
    # 再防一层：属性里带表情 / 自定义 emoji / GIF 的也不算
    for attr in getattr(doc, "attributes", None) or []:
        if type(attr).__name__ in (
            "DocumentAttributeSticker",
            "DocumentAttributeCustomEmoji",
            "DocumentAttributeAnimated",
        ):
            return False
    return True


def message_link(chat_id: int, username: str, message_id: int) -> str:
    """指向 Telegram 里某条消息的链接。

    超级群/频道用 t.me/c/<内部 id>/<msg>（需要是成员才能打开）；
    公开频道用 t.me/<username>/<msg>。普通群没有可分享链接，返回空。
    """
    mid = int(message_id or 0)
    if not mid:
        return ""
    name = str(username or "").strip().lstrip("@")
    if name:
        return f"https://t.me/{name}/{mid}"
    cid = str(int(chat_id or 0))
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{mid}"
    return ""


def source_text(title: str, link: str) -> str:
    """跟一条在视频下面的「来源」文本。没有链接时只给群名。"""
    name = " ".join(str(title or "").split())[:80]
    link = str(link or "").strip()
    if name and link:
        return f"来源：{name}\n{link}"
    if link:
        return f"来源：{link}"
    if name:
        return f"来源：{name}"
    return ""


def link_source_enabled(target: dict) -> bool:
    """默认开：附带来源是排查和回原帖的主要手段，只有手动关掉才不发。"""
    return bool((target or {}).get("link_source", True))


def is_recent(value: Any, minutes: int) -> bool:
    """消息发布时间是否在最近 minutes 分钟内。

    补偿回溯补转视频时用它卡时间窗：不卡的话重启会把几天历史灌进目标群。
    拿不到时间一律当「旧」，宁可少转也不刷爆目标群。
    """
    if not isinstance(value, datetime):
        return False
    posted = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return utcnow() - posted <= timedelta(minutes=max(1, int(minutes or 0)))


def media_kind(message: Any) -> str:
    """给人看的媒体类型，只用日志和诊断，不参与判断。"""
    if getattr(message, "sticker", None) is not None:
        return "表情"
    if getattr(message, "gif", None) is not None:
        return "GIF"
    if getattr(message, "video", None) is not None:
        return "视频"
    if getattr(message, "video_note", None) is not None:
        return "圆视频"
    if getattr(message, "photo", None) is not None:
        return "图片"
    doc = getattr(message, "document", None)
    if doc is not None:
        return f"文件({getattr(doc, 'mime_type', '') or '未知'})"
    return "无媒体"


def _describe(error: Exception) -> str:
    """把 Telegram 的报错翻成人能看懂的一句话。"""
    text = str(error)
    lowered = text.lower()
    if (
        "protected" in lowered
        or "noforwards" in lowered
        or "can't forward" in lowered
        or "chat_forbidden" in lowered
    ):
        return "来源群禁止转发（protected content），只能下载重传，本项目不做"
    return f"{type(error).__name__}: {text}"[:300]


async def album_ids(client, chat, message) -> list[int]:
    """相册要整组转，否则一条帖子会散成好几条消息。"""
    message_id = int(getattr(message, "id", 0) or 0)
    grouped = getattr(message, "grouped_id", None)
    if not grouped or not message_id:
        return [message_id] if message_id else []
    window = range(max(1, message_id - ALBUM_WINDOW), message_id + ALBUM_WINDOW + 1)
    try:
        siblings = await client.get_messages(chat, ids=list(window))
    except Exception as e:      # 拿不到同组消息就只转这一条，别整个放弃
        log.debug("相册同组消息读取失败：%s", e)
        return [message_id]
    picked = [
        int(m.id) for m in (siblings or [])
        if m is not None and getattr(m, "grouped_id", None) == grouped
    ]
    return sorted(picked) or [message_id]


def bot_chat_id(peer: dict) -> int:
    """把 Telethon 那边的 peer 换成 Bot API 用的 chat_id。

    超级群/频道的 `-100…` 两边一致；**普通群** Telethon 给正数（如 123456789），
    Bot API 那边是它的负数（-123456789）。
    这个换算绑定时会用 `getChat` 再验一次，转错会当场报错，不会发到错的群。
    """
    chat_id = int(peer.get("chat_id") or 0)
    raw_id = int(peer.get("raw_id") or 0)
    if str(peer.get("kind") or "") == "chat" and raw_id:
        return -raw_id
    return chat_id


def _chat_title(chat: Any) -> str:
    for attr in ("title", "username", "first_name"):
        value = getattr(chat, attr, None)
        if value:
            return str(value)
    return ""


def _target_entity(target: dict):
    """用 raw_id + access_hash 拼一个 InputPeer。

    不用带 -100 的 chat_id 直接查：采集那条连接没见过这个群，
    没有 access_hash 的话 Telethon 解析不出实体。
    """
    from telethon.tl.types import InputPeerChannel, InputPeerChat

    kind = str(target.get("kind") or "megagroup")
    raw_id = int(target.get("raw_id") or 0)
    if kind == "chat":
        # 基本群（没升级成超级群）只需要 chat_id
        return InputPeerChat(chat_id=raw_id) if raw_id else None
    access_hash = int(target.get("access_hash") or 0)
    if not raw_id or not access_hash:
        return int(target.get("chat_id") or 0) or None
    return InputPeerChannel(channel_id=raw_id, access_hash=access_hash)


async def forward_message(client, chat, message, target: dict, *,
                          forwarded_albums: set[int] | None = None) -> dict:
    """采集号直接发到目标群（相册整组）。返回 {ok, count, error, skipped}。"""
    if not target.get("chat_id"):
        return {"ok": False, "skipped": "no-target", "error": "还没有设置转发目标群"}

    grouped = getattr(message, "grouped_id", None)
    if grouped and forwarded_albums is not None:
        if grouped in forwarded_albums:
            # 相册里的第 2..n 条消息会各自触发一次事件，别重复转
            return {"ok": True, "skipped": "album", "count": 0}

    ids = await album_ids(client, chat, message)
    if not ids:
        return {"ok": False, "skipped": "empty", "error": "消息没有 id"}

    entity = _target_entity(target)
    if not entity:
        return {"ok": False, "skipped": "no-target", "error": "转发目标群记录不完整"}

    try:
        # 注意参数名：client 层是 messages=（Request 层才是 message_ids），
        # 相册多传一个 as_album，转过去仍然是一条相册
        sent = await client.forward_messages(
            entity=entity,
            messages=ids,
            from_peer=chat,
            as_album=len(ids) > 1,
        )
    except Exception as e:
        return {"ok": False, "error": _describe(e), "count": 0}

    # 转发不包含「原消息在哪」的信息（私群尤其看不出），补一条可点的来源回复
    if link_source_enabled(target):
        posted = [int(getattr(m, "id", 0) or 0) for m in (sent or [])]
        head = posted[0] if posted else ids[0]
        note = source_text(
            _chat_title(chat),
            message_link(
                int(getattr(chat, "id", 0) or 0),
                getattr(chat, "username", "") or "",
                ids[0],
            ),
        )
        if note and head:
            try:
                await client.send_message(
                    entity=entity, message=note, reply_to=head,
                )
            except Exception as e:
                # 来源发不出去不算转发失败，媒体已经在群里了
                log.warning("附带来源消息发送失败：%s", str(e)[:160])

    if grouped and forwarded_albums is not None:
        forwarded_albums.add(grouped)
        if len(forwarded_albums) > 1000:      # 别无限长
            forwarded_albums.clear()
    return {"ok": True, "count": len(ids), "ids": ids}


def _describe_bot(error: Exception) -> str:
    """转发给 bot 这一步的常见报错。"""
    text = str(error).lower()
    if (
        "peer_id_invalid" in text
        or "peeridinvalid" in text
        or "could not find the input entity" in text
        or "can't write" in text
    ):
        return "采集号还没给 bot 发过 /start（先私聊 bot 发一条 /start）"
    if "protected" in text or "noforwards" in text or "can't forward" in text:
        return "来源群禁止转发（protected content），只能下载重传，本项目不做"
    if "flood" in text:
        return f"Telegram 限流：{str(error)[:140]}"
    if "blocked" in text:
        return "采集号把 bot 拉黑了，或者 bot 被停用"
    return _describe(error)


async def forward_to_bot(client, chat, message, target: dict, *,
                         forwarded_albums: set[int] | None = None) -> dict:
    """把视频转发给 bot 私聊，剩下的交给 bot（它再 copyMessage 到群）。

    为什么不直接发到群：采集号大量往群里扔大文件更容易被风控，
    而「给 bot 发消息」不构成向陌生人发广告，风险低得多。
    """
    peer = str(target.get("bot_username") or "").strip()
    bot_user_id = int(target.get("bot_user_id") or 0)
    if not peer and bot_user_id:
        peer = bot_user_id
    if not peer:
        return {"ok": False, "skipped": "no-target",
                "error": "还没绑定 bot（先在设置里填 Bot Token 并绑定）"}

    grouped = getattr(message, "grouped_id", None)
    if grouped and forwarded_albums is not None and grouped in forwarded_albums:
        return {"ok": True, "skipped": "album", "count": 0}

    ids = await album_ids(client, chat, message)
    if not ids:
        return {"ok": False, "skipped": "empty", "error": "消息没有 id"}

    try:
        await client.forward_messages(
            entity=peer,
            messages=ids,
            from_peer=chat,
            as_album=len(ids) > 1,
        )
    except Exception as e:
        return {"ok": False, "error": _describe_bot(e), "count": 0}

    if grouped and forwarded_albums is not None:
        forwarded_albums.add(grouped)
        if len(forwarded_albums) > 1000:
            forwarded_albums.clear()
    return {"ok": True, "count": len(ids), "ids": ids}


def load_relay_target(store) -> dict:
    data = store.get_json(RELAY_TARGET_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def save_relay_target(store, *, chat_id: int, title: str = "",
                      link_source: bool = True) -> dict:
    data = {
        "chat_id": int(chat_id),
        "title": title or "",
        "link_source": bool(link_source),
    }
    store.set_json(RELAY_TARGET_KEY, data)
    return data


def clear_relay_target(store) -> None:
    store.set_json(RELAY_TARGET_KEY, {})


def note_seen(store, account_id: int) -> dict:
    """记下「看到了一条该转的视频」。

    和 forwarded / failed 对账用的：seen 比 forwarded + failed 大，
    就说明有东西没转出去 —— 至少能看出来，而不是靠拍脑袋。
    """
    data = load_stats(store, account_id)
    data["seen"] = int(data.get("seen") or 0) + 1
    store.set_json(stats_key(account_id), data)
    return data


def load_relay_stats(store) -> dict:
    data = store.get_json(RELAY_STATS_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def note_relay_result(store, *, ok: bool, error: str = "", count: int = 0) -> dict:
    """bot 侧复制消息的统计（和采集号转发那一步分开算）。"""
    data = load_relay_stats(store)
    # 两个计数器始终存在：页面直接读，少一个会让前端显示成 undefined
    data["copied"] = int(data.get("copied") or 0)
    data["failed"] = int(data.get("failed") or 0)
    if ok:
        data["copied"] = int(data.get("copied") or 0) + max(1, int(count or 1))
        data["last_error"] = ""
    else:
        data["failed"] = int(data.get("failed") or 0) + 1
        data["last_error"] = (error or "")[:300]
    data["last_at"] = iso(utcnow())
    store.set_json(RELAY_STATS_KEY, data)
    return data
