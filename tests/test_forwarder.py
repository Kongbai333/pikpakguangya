"""视频转发：识别视频、相册整组转、失败原因、统计。

真的 Telegram 请求一律用假 client 顶上，测试不碰网络。
"""

from __future__ import annotations

import asyncio

import pytest

from app import forwarder
from app.store import Store


class FakeDoc:
    def __init__(self, mime: str):
        self.mime_type = mime


class FakeMessage:
    def __init__(self, mid: int, *, grouped=None, video=False, mime: str = ""):
        self.id = mid
        self.grouped_id = grouped
        self.video = object() if video else None
        self.document = FakeDoc(mime) if mime else None


class FakeClient:
    def __init__(self, siblings: list | None = None, error: Exception | None = None):
        self.siblings = siblings or []
        self.error = error
        self.get_calls: list[list[int]] = []
        self.forwards: list[dict] = []

    async def get_messages(self, chat, ids=None):
        self.get_calls.append(list(ids or []))
        return self.siblings

    async def forward_messages(self, entity=None, message_ids=None, from_peer=None):
        if self.error is not None:
            raise self.error
        self.forwards.append({
            "entity": entity,
            "ids": list(message_ids or []),
            "from_peer": from_peer,
        })
        return []


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "forward.db")
    s.init()
    return s


TARGET = {
    "chat_id": -1001234567890,
    "raw_id": 1234567890,
    "access_hash": 555,
    "title": "PikPak 视频转发",
}


# ---------- 识别视频 ----------
def test_detect_video():
    assert forwarder.detect_video(FakeMessage(1, video=True)) is True
    assert forwarder.detect_video(FakeMessage(1, mime="video/mp4")) is True
    # 图片、纯文本不算
    assert forwarder.detect_video(FakeMessage(1, mime="image/jpeg")) is False
    assert forwarder.detect_video(FakeMessage(1)) is False


# ---------- 相册 ----------
def test_album_ids_picks_only_same_group():
    client = FakeClient(siblings=[
        FakeMessage(10, grouped=777),
        FakeMessage(11, grouped=777),
        FakeMessage(12, grouped=999),      # 同窗口但不是同一相册
        None,
    ])
    ids = asyncio.run(forwarder.album_ids(
        client, chat=object(), message=FakeMessage(11, grouped=777),
    ))
    assert ids == [10, 11]


def test_single_message_needs_no_extra_request():
    client = FakeClient()
    ids = asyncio.run(forwarder.album_ids(
        client, chat=object(), message=FakeMessage(42),
    ))
    assert ids == [42]
    assert client.get_calls == []


def test_album_is_forwarded_once(store):
    client = FakeClient(siblings=[
        FakeMessage(10, grouped=777, video=True),
        FakeMessage(11, grouped=777, video=True),
    ])
    albums: set[int] = set()

    first = asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(10, grouped=777, video=True), TARGET,
        forwarded_albums=albums,
    ))
    assert first == {"ok": True, "count": 2}
    assert client.forwards[0]["ids"] == [10, 11]

    # 相册里第二条消息也会触发事件，不能重复转
    second = asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(11, grouped=777, video=True), TARGET,
        forwarded_albums=albums,
    ))
    assert second["skipped"] == "album"
    assert len(client.forwards) == 1


def test_target_uses_access_hash_not_bare_id():
    """采集是另一条连接，只给 id 会解析不到实体，必须带 access_hash。"""
    client = FakeClient()
    asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(1, video=True), TARGET,
    ))
    entity = client.forwards[0]["entity"]
    assert type(entity).__name__ == "InputPeerChannel"
    assert entity.channel_id == TARGET["raw_id"]
    assert entity.access_hash == TARGET["access_hash"]


# ---------- 失败与目标缺失 ----------
def test_no_target_returns_reason():
    client = FakeClient()
    result = asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(1, video=True), {},
    ))
    assert result["ok"] is False
    assert result["skipped"] == "no-target"
    assert client.forwards == []


def test_protected_chat_error_is_explained():
    error = Exception("CHAT_FORBIDDEN: You can't forward messages from a protected chat")
    client = FakeClient(error=error)
    result = asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(1, video=True), TARGET,
    ))
    assert result["ok"] is False
    assert "禁止转发" in result["error"]


# ---------- 目标与统计 ----------
def test_target_round_trip(store):
    assert forwarder.load_target(store, 1) == {}
    forwarder.save_target(store, 1, chat_id=-1001, raw_id=1, access_hash=9, title="群")
    saved = forwarder.load_target(store, 1)
    assert saved["chat_id"] == -1001
    assert saved["access_hash"] == 9

    forwarder.clear_target(store, 1)
    assert forwarder.load_target(store, 1) == {}

    # 按账号分开，别互相串
    forwarder.save_target(store, 2, chat_id=-1002, raw_id=2, access_hash=8, title="另一个")
    assert forwarder.load_target(store, 1) == {}


def test_stats_accumulate_and_keep_last_error(store):
    forwarder.note_result(store, 1, ok=True, source="群A", count=2)
    forwarder.note_result(store, 1, ok=False, error="boom", source="群B")
    stats = forwarder.load_stats(store, 1)
    assert stats["forwarded"] == 1
    assert stats["failed"] == 1
    assert stats["messages"] == 2
    assert stats["last_error"] == "boom"
    assert stats["last_source"] == "群B"
    assert stats["last_at"]

    # 成功一次要把上一次的错误清掉，不然页面一直红着
    forwarder.note_result(store, 1, ok=True, source="群C", count=1)
    assert forwarder.load_stats(store, 1)["last_error"] == ""
