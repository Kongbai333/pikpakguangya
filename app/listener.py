"""Telethon 用户账号监听群组，抽取 mypikpak 分享链接入库。

为什么要用户账号：Bot API 下机器人读不到群里的普通消息（除非关掉 privacy mode
或设为管理员），也读不到入群前的历史。所以这里用 MTProto 用户客户端。

process_message() 是与 Telethon 解耦的纯逻辑，便于单测；
_ingest() 负责把 Telethon 的 Message 转成它需要的参数。

多账号：每个账号一套心跳 / 状态 / 回溯游标，键里都带 account_id；
实际运行由 app/collector.py 的 CollectorManager 按账号拉起。
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app import media as media_module
from app.parser import CodeMerger, keep_message
from app.proxy import ProxyConfig, proxy_from_account
from app.store import Store, iso, utcnow
from app.tg import (
    TelegramSession,
    _make_session,
    missing_credentials,
    resolve_credentials,
    session_file,
)

log = logging.getLogger("listener")

# 管理页通过这些键看到「某个账号在不在跑」
def heartbeat_key(account_id: int) -> str:
    return f"listener.heartbeat:{int(account_id)}"


def status_key(account_id: int) -> str:
    return f"listener.status:{int(account_id)}"


def last_event_key(account_id: int) -> str:
    return f"listener.last_event:{int(account_id)}"


def cursor_key(account_id: int, chat_id: int) -> str:
    return f"last_message_id:{int(account_id)}:{int(chat_id)}"

# 心跳间隔
HEARTBEAT_SECONDS = 30
# 超过这个时间没心跳就当采集没在跑（3 倍留抖动余量）。
# 管理页的「在运行」判断，以及「采集占着 session 时后台别去连 Telegram」都用它。
HEARTBEAT_STALE_SECONDS = HEARTBEAT_SECONDS * 3


def _beat(store: Store, account_id: int) -> None:
    store.set_json(
        heartbeat_key(account_id),
        {"at": iso(utcnow()), "pid": os.getpid(), "account_id": int(account_id)},
    )


# ---------- 纯逻辑：可单测 ----------
def process_message(
    store: Store,
    merger: CodeMerger,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    posted_at: str,
    chat_title: str = "",
    sender_id: int | None = None,
    sender_name: str = "",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    ignore_senders: list[str] | None = None,
) -> dict:
    """过滤 → 合并提取码 → 落库。"""
    if ignore_senders and (
        str(sender_id) in ignore_senders or (sender_name and sender_name in ignore_senders)
    ):
        return {"skipped": "sender"}

    if not keep_message(text, include, exclude):
        return {"skipped": "keyword"}

    now = _parse_iso(posted_at)
    shares = merger.observe(chat_id, sender_id, text, now)
    if not shares:
        return {"skipped": "no-link"}

    saved = []
    for s in shares:
        if s.update_only:
            # 这条只是补提取码，不算一次新命中
            store.update_pass_code(s.link_id, s.pass_code)
            res = store.add_message(
                chat_id=chat_id, chat_title=chat_title, message_id=message_id,
                sender_id=sender_id, sender_name=sender_name, posted_at=posted_at,
                raw_text=text, link_id=s.link_id, url=s.url, pass_code=s.pass_code,
                remark="",
            )
        else:
            res = store.add_message(
                chat_id=chat_id, chat_title=chat_title, message_id=message_id,
                sender_id=sender_id, sender_name=sender_name, posted_at=posted_at,
                raw_text=text, link_id=s.link_id, url=s.url, pass_code=s.pass_code,
                remark=s.remark,
            )
        saved.append({
            "link_id": s.link_id,
            "share_id": res.get("share_id"),
            "pass_code": s.pass_code,
            "remark": s.remark,
            "share_created": res.get("share_created", False),
            "update_only": s.update_only,
        })
    return {"saved": saved}


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return utcnow()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- Telethon 胶水层 ----------
def _chat_title(chat: Any) -> str:
    for attr in ("title", "username", "first_name"):
        v = getattr(chat, attr, None)
        if v:
            return str(v)
    return str(getattr(chat, "id", ""))


async def _sender_info(msg: Any) -> tuple[int | None, str]:
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        name = getattr(fwd, "from_name", None)
        if name:
            fid = getattr(fwd, "from_id", None)
            uid = getattr(fid, "user_id", None) or getattr(fid, "channel_id", None)
            return uid, str(name)
    sender = None
    getter = getattr(msg, "get_sender", None)
    if callable(getter):
        try:
            sender = await getter()
        except Exception:  # 已注销账号、隐私设置等
            sender = None
    if sender is not None:
        name = " ".join(
            x for x in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if x
        ) or getattr(sender, "title", None) or getattr(sender, "username", None) or ""
        return getattr(sender, "id", None), str(name)
    return getattr(msg, "sender_id", None), ""


async def _ingest(obj: Any, chat: Any, store: Store, merger: CodeMerger, cfg,
                  account_id: int) -> dict:
    """obj 可以是 events.NewMessage 的 event，也可以是裸 Message。

    这两种的 `.message` 含义完全不同：event.message 是 Message 对象，
    而 Message.message 是**正文文本**。只写 getattr(obj, "message", obj)
    的话，传裸 Message 时会拿到一个字符串，正文就被当成空 ——
    结果是回溯历史时全部 skipped:empty。
    """
    inner = getattr(obj, "message", None)
    msg = obj if inner is None or isinstance(inner, str) else inner
    text = getattr(msg, "message", None) or ""

    date = getattr(msg, "date", None)
    if isinstance(date, datetime):
        posted_at = iso(date if date.tzinfo else date.replace(tzinfo=timezone.utc))
    else:
        posted_at = iso(utcnow())

    sender_id, sender_name = await _sender_info(msg)
    c = cfg.collector
    if c.ignore_senders and (
        str(sender_id) in c.ignore_senders or (sender_name and sender_name in c.ignore_senders)
    ):
        return {"skipped": "sender"}
    chat_id = int(getattr(chat, "id", 0) or getattr(msg, "chat_id", 0) or 0)
    account_id = int(account_id or 0)
    media_ids = []
    if account_id and store.media_enabled(chat_id, account_id):
        media_ids = await media_module.collect(
            msg, chat, store, account_id, _chat_title(chat)
        )
    if not text.strip():
        return {"skipped": "empty", "media_ids": media_ids}

    result = process_message(
        store, merger,
        chat_id=chat_id,
        message_id=int(getattr(msg, "id", 0) or 0),
        text=text,
        posted_at=posted_at,
        chat_title=_chat_title(chat),
        sender_id=sender_id,
        sender_name=sender_name,
        include=c.keywords_include,
        exclude=c.keywords_exclude,
        ignore_senders=c.ignore_senders,
    )
    result["media_ids"] = media_ids
    for s in result.get("saved", []):
        if s["update_only"]:
            log.debug("补码 %s -> %s", s["link_id"], s["pass_code"])
        elif s["share_created"]:
            log.debug(
                "新链接 %s 码=%s 备注=%s",
                s["link_id"], s["pass_code"] or "无", s["remark"],
            )
    if result.get("saved"):
        # 管理页靠这个显示「最后采到的是什么、什么时候、哪个群」
        store.set_json(last_event_key(account_id), {
            "at": iso(utcnow()),
            "chat_id": chat_id,
            "chat_title": _chat_title(chat),
            "link_id": result["saved"][0]["link_id"],
            "new_links": sum(1 for s in result["saved"] if s.get("share_created")),
        })
    return result


async def _resolve_chats(client, watch: list[str]) -> tuple[list[Any], list[str]]:
    """解析监控目标。返回 (解析成功的实体, 解析失败的原始写法)。

    失败的以前只写一行日志 —— 5 个群挂了 1 个，页面上看不出来，
    用户只会觉得「这个群怎么不采了」。现在把失败清单交回给 Collector，
    显示在账号状态里。
    """
    chats: list[Any] = []
    failed: list[str] = []
    for spec in watch:
        try:
            chats.append(await client.get_entity(spec))
        except Exception as e:
            failed.append(f"{spec}（{type(e).__name__}）")
            log.error("解析群失败 %s：%s（确认账号已加入该群，或改用数字 id）", spec, e)
    return chats, failed


async def _backfill(client, store: Store, merger: CodeMerger, cfg, chats: list[Any],
                    account_id: int, *, catch_up: bool = False, skip=None) -> int:
    """补齐「上次采集位置」到现在的消息，不读监控之前的历史。

    - 这个账号第一次监控某个群（没有游标）：不读历史，把游标推到当前最新，
      之后只收新消息；
    - 已有游标：从上次位置一直补到现在，程序离线期间的消息不会漏；
    - backfill_limit 是分页大小；设为 0 表示不补历史，直接跳到最新。

    catch_up=True 是「运行中的定期补偿」：这时游标一定已经建立，跑的是
    「把实时链路漏掉的补回来」。所以 backfill_limit=0 也不能把游标推到最新 ——
    那是「跳过历史」的语义，用在补偿上会直接把漏掉的消息永久跳过去。

    skip(chat_id, message_id) 可选，返回 True 表示这条已经处理过、不用再看。
    补偿回溯从游标重扫时必然遇到一堆实时链路刚处理完的消息，靠它在内存里
    跳过，省掉重复的解析、落库和缩略图触发。

    返回真正处理过的消息条数（跳过的不算），补偿时用它判断要不要提示。
    """
    page_size = int(cfg.collector.backfill_limit or 0)
    if catch_up and page_size <= 0:
        # 补偿不看 backfill_limit：这里补的是刚刚的缺口，不是历史
        page_size = 200
    total = 0
    for chat in chats:
        chat_id = int(getattr(chat, "id", 0) or 0)
        key = cursor_key(account_id, chat_id)
        title = _chat_title(chat)
        # 老库的游标键不带账号，读一次后迁到新键
        saved = store.get_state(key) or store.get_state(f"last_message_id:{chat_id}") or ""

        if not saved:
            # 第一次监控这个群：不把监控之前的历史倒灌进来
            latest = await client.get_messages(chat, limit=1)
            if latest:
                store.set_state(key, str(latest[0].id))
            log.info("首次监控 %s：跳过历史，游标置为 %s", title, store.get_state(key))
            continue

        if page_size <= 0:
            # 只收新消息：把游标推到当前最新，避免突然补一大堆历史
            latest = await client.get_messages(chat, limit=1)
            if latest:
                store.set_state(key, str(latest[0].id))
            log.info("跳过离线补齐 %s（backfill_limit=0），游标置为 %s",
                     title, store.get_state(key))
            continue

        last = int(saved)
        n, max_id = 0, last
        while True:
            batch = [
                msg async for msg in client.iter_messages(
                    chat, min_id=max_id, reverse=True, limit=page_size
                )
            ]
            if not batch:
                break
            for msg in batch:
                mid = int(getattr(msg, "id", 0) or 0)
                if skip is not None and skip(chat_id, mid):
                    continue
                await _ingest(msg, chat, store, merger, cfg, account_id)
                n += 1
            batch_max = max(int(getattr(m, "id", 0) or 0) for m in batch)
            if batch_max <= max_id:
                break
            max_id = batch_max
            store.set_state(key, str(max_id))
            if len(batch) < page_size:
                break
        total += n
        if n:
            log.info("离线补齐 %s：补进 %s 条，游标 %s -> %s", title, n, last, max_id)
    return total


def watch_specs(store: Store, cfg, account: dict | None) -> tuple[list, str]:
    """监听哪些群：这个账号在管理页勾的；一条都没勾过就退回 config.toml。"""
    acc = account or {}
    account_id = int(acc.get("id") or 0)
    watched = store.list_watched(account_id=account_id or None)
    active = [w for w in watched if w["enabled"] or w["media_enabled"]]
    if active:
        owner = acc.get("name") or acc.get("alias") or acc.get("phone") or ""
        return [int(w["chat_id"]) for w in active], f"数据库（{owner} 的勾选）"
    return list(cfg.collector.watch or []), "config.toml"


def preflight(store: Store, cfg, account: dict | None) -> list[str]:
    """启动采集前的检查。缺什么当场列出来，别等任务/子进程起来再报。

    每个账号单独检查：凭据、群勾选。

    **不检查代理**：没绑就是直连，这是合法配置 —— 部署在能直连 Telegram 的机器上
    （海外 VPS、部分家宽）根本不需要代理。国内不挂代理自然会连不上，
    那时 get_client() 会给出带代理提示的报错，比在这里拦一道清楚。
    """
    problems: list[str] = []
    cred = resolve_credentials(cfg, store)
    missing = missing_credentials(cred)
    if missing:
        problems.append("缺少 Telegram 凭据：api_id / api_hash（去「设置 → 应用凭据」填）")
    specs, _ = watch_specs(store, cfg, account)
    if not specs:
        problems.append("这个账号没有勾选要监听的群（去「账号 → 群组」里勾选并保存）")
    return problems


def mark_stopped(store: Store, account_id: int) -> None:
    """把心跳和状态改成「没在跑」。

    心跳不失效的话，管理页还会显示「在运行」最多 90 秒（过期阈值），
    用户点了停止却看到还在跑，会以为按钮没用。
    """
    with contextlib.suppress(Exception):
        store.set_json(heartbeat_key(account_id), {})
        st = store.get_json(status_key(account_id)) or {}
        store.set_json(
            status_key(account_id),
            {**st, "state": "stopped", "stopped_at": iso(utcnow())},
        )


def build_client(cfg, store: Store | None = None):
    """给 tools/login.py 这类离线脚本建客户端。

    多账号：用数据库里「当前使用」那个账号的 session 文件；库里还没有账号记录
    就退回 config.toml 里 telegram.session 指定的那个（老行为）。
    """
    from telethon import TelegramClient  # 延迟导入：只跑 Web 时不需要 telethon

    if store is not None:
        session = TelegramSession(cfg, store).session_path()
    else:
        session = session_file(cfg.session_path())
    session.parent.mkdir(parents=True, exist_ok=True)
    proxy = proxy_from_account(store, store.active_account() if store else None) or ProxyConfig()
    proxy.validate()
    if store is not None:
        cred = resolve_credentials(cfg, store)
    else:
        cred = {"api_id": cfg.telegram.api_id, "api_hash": cfg.telegram.api_hash}
    return TelegramClient(
        _make_session(session), cred["api_id"], cred["api_hash"],
        **proxy.telethon_kwargs(),
    )
