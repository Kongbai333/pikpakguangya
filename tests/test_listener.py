"""监听链路测试：process_message + CodeMerger + Store 串起来跑。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from app import config as config_module
from app import listener
from app.listener import process_message
from app.parser import CodeMerger
from app.store import Store

SID = "EXAMPLEShareId000000000001"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def send(store, merger, text, *, msg_id=1, chat_id=1, at=T0, **kw):
    return process_message(
        store, merger,
        chat_id=chat_id, message_id=msg_id, text=text,
        posted_at=at.isoformat(timespec="seconds"),
        chat_title="测试群", sender_id=100, sender_name="张三", **kw,
    )


def test_link_and_code_in_one_message(store):
    merger = CodeMerger(90)
    res = send(store, merger, f"示例合集 https://mypikpak.com/s/{SID} 提取码：f766")
    assert len(res["saved"]) == 1
    assert res["saved"][0]["share_created"] is True

    item = store.query()["items"][0]
    assert item["LINK"] == f"https://mypikpak.com/s/{SID}"
    assert item["CODE"] == "f766"
    assert item["MSG"] == "示例合集"


def test_code_in_separate_message(store):
    merger = CodeMerger(90)
    send(store, merger, f"https://mypikpak.com/s/{SID}", msg_id=1)
    assert store.query()["items"][0]["CODE"] == "无"

    res = send(store, merger, "f766", msg_id=2, at=T0.replace(minute=1))
    assert res["saved"][0]["update_only"] is True

    item = store.query()["items"][0]
    assert item["CODE"] == "f766"
    assert store.stats()["messages"] == 2


def test_non_link_message_skipped(store):
    merger = CodeMerger(90)
    assert send(store, merger, "大家晚上好") == {"skipped": "no-link"}
    assert store.stats()["messages"] == 0


def test_keyword_filters(store):
    merger = CodeMerger(90)
    text = f"广告 https://mypikpak.com/s/{SID}"
    assert send(store, merger, text, exclude=["广告"]) == {"skipped": "keyword"}
    assert send(store, merger, text, include=["示例"]) == {"skipped": "keyword"}
    assert store.stats()["messages"] == 0


def test_ignore_senders(store):
    merger = CodeMerger(90)
    res = send(store, merger, f"https://mypikpak.com/s/{SID}", ignore_senders=["100"])
    assert res == {"skipped": "sender"}
    res = send(store, merger, f"https://mypikpak.com/s/{SID}", ignore_senders=["张三"])
    assert res == {"skipped": "sender"}


def test_two_links_one_message(store):
    merger = CodeMerger(90)
    res = send(store, merger, "https://mypikpak.com/s/AAAA1111 https://mypikpak.com/s/BBBB2222 提取码：zzzz")
    assert len(res["saved"]) == 2
    assert store.stats()["shares"] == 2
    assert all(it["CODE"] == "zzzz" for it in store.query(size=10)["items"])


def test_reprocess_same_message_is_idempotent(store):
    merger = CodeMerger(90)
    text = f"https://mypikpak.com/s/{SID} 提取码：f766"
    send(store, merger, text, msg_id=1)
    send(store, merger, text, msg_id=1)
    assert store.stats()["shares"] == 1
    assert store.stats()["messages"] == 1


# ---------- 心跳与「最后采到」：管理页靠这两个键看监听器活着没有 ----------
def test_beat_writes_heartbeat(store):
    listener._beat(store, 1)
    hb = store.get_json(listener.heartbeat_key(1))
    assert hb["pid"] == os.getpid()
    assert hb["at"]      # ISO 时间戳，管理页据此算「多久没心跳了」


class _FakeChat:
    def __init__(self, cid, title):
        self.id = cid
        self.title = title


class _FakeMsg:
    """只提供 _ingest 用到的属性，避免测试里真连 Telegram。"""

    def __init__(self, text, mid=1, date=None):
        self.message = text
        self.id = mid
        self.date = date
        self.sender_id = 7
        self.fwd_from = None

    async def get_sender(self):
        return None


def _cfg(store):
    return config_module.Config(
        storage=config_module.StorageConfig(db=str(store.path)),
        checker=config_module.CheckerConfig(enabled=False),
    )


def test_ingest_records_last_event(store):
    merger = CodeMerger(90)
    msg = _FakeMsg(f"https://mypikpak.com/s/{SID} 提取码：f766", mid=42, date=T0)
    chat = _FakeChat(-1003739604004, "搬运家")

    result = asyncio.run(listener._ingest(msg, chat, store, merger, _cfg(store), 1))
    assert result["saved"]

    ev = store.get_json(listener.last_event_key(1))
    assert ev["chat_id"] == -1003739604004
    assert ev["chat_title"] == "搬运家"
    assert ev["link_id"] == SID
    assert ev["new_links"] == 1


def test_ingest_without_link_does_not_touch_last_event(store):
    """纯聊天消息不该把「最后采到」刷掉，否则看不出到底还采不采得到东西。"""
    merger = CodeMerger(90)
    msg = _FakeMsg("今天天气不错", mid=43, date=T0)
    chat = _FakeChat(-1003739604004, "搬运家")

    asyncio.run(listener._ingest(msg, chat, store, merger, _cfg(store), 1))
    assert store.get_json(listener.last_event_key(1)) is None


class _FakeEvent:
    """events.NewMessage 的 event：它的 .message 是 Message 对象，不是文本。"""

    def __init__(self, msg):
        self.message = msg


def test_ingest_accepts_event_and_bare_message(store):
    """回归：两种入参都要能用。

    event.message 是 Message 对象，而 Message.message 是正文文本。
    以前只写 getattr(obj, "message", obj)，传裸 Message（回溯历史走的就是这条路）
    会拿到一个字符串，正文被当成空，于是回溯到的消息全部 skipped:empty。
    """
    merger = CodeMerger(90)
    text = f"https://mypikpak.com/s/{SID} 提取码：f766"
    chat = _FakeChat(-1003739604004, "搬运家")

    # 1) 裸 Message（_backfill 的调用方式）
    r1 = asyncio.run(
        listener._ingest(_FakeMsg(text, mid=1, date=T0), chat, store, merger, _cfg(store), 1)
    )
    assert r1["saved"], f"裸 Message 应当被解析，实际 {r1}"

    # 2) event（_on_new / _on_edit 的调用方式）
    r2 = asyncio.run(
        listener._ingest(
            _FakeEvent(_FakeMsg(text, mid=2, date=T0)), chat, store, merger, _cfg(store), 1
        )
    )
    assert r2["saved"], f"event 应当被解析，实际 {r2}"


# ---------- 补偿回溯：运行中按游标重扫，把实时链路漏掉的补回来 ----------
class _FakeIterClient:
    """_backfill 需要的部分：iter_messages / get_messages。"""

    def __init__(self, messages):
        self.messages = messages          # {chat_id: [msg, ...]}
        self.requests = 0

    def iter_messages(self, chat, **kw):  # noqa: ANN001, ANN003, ANN201
        self.requests += 1
        min_id = int(kw.get("min_id") or 0)
        limit = int(kw.get("limit") or 100)
        batch = sorted(
            (m for m in self.messages.get(chat.id, []) if m.id > min_id),
            key=lambda m: m.id,
        )[:limit]

        async def gen():
            for m in batch:
                yield m
        return gen()

    async def get_messages(self, chat, limit=1):  # noqa: ANN001, ANN201
        items = sorted(self.messages.get(chat.id, []), key=lambda m: m.id)
        return items[-limit:]


def test_backfill_recovers_messages_after_cursor(store):
    """游标之后的消息要补进来 —— 这就是「程序离线期间没漏」的保证。"""
    merger = CodeMerger(90)
    chat = _FakeChat(-1003739604004, "搬运家")
    msgs = [
        _FakeMsg(f"https://mypikpak.com/s/{SID[:-1]}{i} 提取码：f76{i}", mid=i, date=T0)
        for i in (7, 8, 9)
    ]
    client = _FakeIterClient({chat.id: msgs})
    store.set_state(listener.cursor_key(1, chat.id), "6")

    n = asyncio.run(
        listener._backfill(client, store, merger, _cfg(store), [chat], 1, catch_up=True)
    )
    assert n == 3
    assert store.stats()["shares"] == 3
    assert store.get_state(listener.cursor_key(1, chat.id)) == "9"


def test_backfill_skips_already_handled_messages(store):
    """补偿回溯从游标重扫，实时链路刚处理过的消息要跳过，但游标照样前进。

    不跳的话每轮补偿都会把这段时间的消息重新解析、落库、再触发一遍缩略图下载；
    跳过的只是「重复劳动」，游标必须推进 —— 否则下一轮还要再扫一遍。
    """
    merger = CodeMerger(90)
    chat = _FakeChat(-1003739604004, "搬运家")
    msg = _FakeMsg(f"https://mypikpak.com/s/{SID} 提取码：f766", mid=9, date=T0)
    client = _FakeIterClient({chat.id: [msg]})
    cfg = _cfg(store)
    key = listener.cursor_key(1, chat.id)

    store.set_state(key, "5")
    assert asyncio.run(
        listener._backfill(client, store, merger, cfg, [chat], 1, catch_up=True)
    ) == 1
    assert store.stats()["shares"] == 1

    # 实时链路又处理了一遍同一条消息（游标还没跟上），补偿时应当直接跳过
    store.set_state(key, "5")
    seen = {(chat.id, 9)}
    n = asyncio.run(listener._backfill(
        client, store, merger, cfg, [chat], 1, catch_up=True,
        skip=lambda cid, mid: (cid, mid) in seen,
    ))
    assert n == 0
    assert store.get_state(key) == "9"
    assert store.stats()["shares"] == 1        # 没有重复落库


def test_catchup_does_not_jump_the_cursor_when_backfill_limit_is_zero(store):
    """backfill_limit=0 是「跳过历史」的意思，不能用在补偿上。

    补偿跑的是「补刚刚的缺口」，这时把游标推到最新等于把漏掉的消息永久跳过。
    """
    merger = CodeMerger(90)
    chat = _FakeChat(-1003739604004, "搬运家")
    msg = _FakeMsg(f"https://mypikpak.com/s/{SID} 提取码：f766", mid=9, date=T0)
    client = _FakeIterClient({chat.id: [msg]})
    cfg = _cfg(store)
    cfg.collector.backfill_limit = 0
    key = listener.cursor_key(1, chat.id)
    store.set_state(key, "5")

    assert asyncio.run(
        listener._backfill(client, store, merger, cfg, [chat], 1, catch_up=True)
    ) == 1
    assert store.stats()["shares"] == 1
