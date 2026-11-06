"""视频转发：识别视频、相册整组转、失败原因、统计。

真的 Telegram 请求一律用假 client 顶上，测试不碰网络。
"""

from __future__ import annotations

import asyncio

import pytest

from app import forwarder
from app.parser import CodeMerger
from app.store import Store


class FakeDoc:
    def __init__(self, mime: str, attributes=None):
        self.mime_type = mime
        self.attributes = attributes or []


class FakeMessage:
    def __init__(self, mid: int, *, grouped=None, video=False, mime: str = "",
                 sticker=False, gif=False, video_note=False, photo=False,
                 attributes=None):
        self.id = mid
        self.grouped_id = grouped
        self.video = object() if video else None
        self.video_note = object() if video_note else None
        self.sticker = object() if sticker else None
        self.gif = object() if gif else None
        self.photo = object() if photo else None
        doc = None
        if mime:
            doc = FakeDoc(mime, attributes)
        self.document = doc


class FakeClient:
    """参数签名必须和 Telethon 真实方法一致（entity / messages / from_peer / as_album）。

    早期这里写成 `message_ids=`，假 client 跟着一起错，测试全绿 ——
    但真跑起来直接 TypeError。见 test_forward_call_matches_telethon_signature。
    """

    def __init__(self, siblings: list | None = None, error: Exception | None = None):
        self.siblings = siblings or []
        self.error = error
        self.get_calls: list[list[int]] = []
        self.forwards: list[dict] = []

    async def get_messages(self, chat, ids=None):
        self.get_calls.append(list(ids or []))
        return self.siblings

    async def forward_messages(self, entity=None, messages=None, from_peer=None,
                               **kwargs):
        if self.error is not None:
            raise self.error
        self.forwards.append({
            "entity": entity,
            "ids": list(messages or []),
            "from_peer": from_peer,
            **kwargs,
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
    assert forwarder.detect_video(FakeMessage(1, video_note=True)) is True
    # 图片、纯文本不算
    assert forwarder.detect_video(FakeMessage(1, mime="image/jpeg")) is False
    assert forwarder.detect_video(FakeMessage(1)) is False


def test_video_sticker_is_not_a_video():
    """video 表情同时带 Sticker + Video 属性，Telethon 的 .video 也会返回真 ——
    必须优先按表情排掉，否则真的会把表情当视频转走。"""
    sticker = FakeMessage(1, video=True, sticker=True, mime="video/webm")
    assert forwarder.detect_video(sticker) is False
    assert forwarder.media_kind(sticker) == "表情"


def test_gif_is_not_a_video():
    gif = FakeMessage(1, gif=True, mime="video/mp4")
    assert forwarder.detect_video(gif) is False
    assert forwarder.media_kind(gif) == "GIF"


def test_document_with_sticker_attribute_is_not_a_video():
    """document 里带 Sticker/Animated 属性的 video mime 也要排掉。"""
    fake = FakeMessage(1, mime="video/webm", attributes=[
        type("DocumentAttributeSticker", (), {})(),
    ])
    assert forwarder.detect_video(fake) is False


def test_bot_and_collector_agree_on_what_is_a_video():
    """两边口径不一致就会出现「采集侧转了、bot 丢掉」。"""
    from app.telegram_bot import TelegramBotRunner

    is_video = TelegramBotRunner._is_video_message
    # 表情：采集侧说是表情，bot 侧看到的是 sticker
    assert forwarder.detect_video(FakeMessage(1, video=True, sticker=True)) is False
    assert is_video({"sticker": {"is_video": True}}) is False
    # GIF：两边都不算
    assert forwarder.detect_video(FakeMessage(1, gif=True, mime="video/mp4")) is False
    assert is_video({"animation": {"file_id": "x"}}) is False
    # 真视频：两边都算
    assert forwarder.detect_video(FakeMessage(1, video=True)) is True
    assert is_video({"video": {"file_id": "x"}}) is True
    assert is_video({"document": {"mime_type": "video/mp4"}}) is True


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
    # 整组要用 as_album 转，否则到目标群会散成两条
    assert client.forwards[0]["as_album"] is True

    # 相册里第二条消息也会触发事件，不能重复转
    second = asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(11, grouped=777, video=True), TARGET,
        forwarded_albums=albums,
    ))
    assert second["skipped"] == "album"
    assert len(client.forwards) == 1


def test_forward_call_matches_telethon_signature():
    """用 Telethon 真正的方法签名 bind 一遍。

    参数名写错（例如把 `messages=` 写成 `message_ids=`）在单元测试里很容易
    被假 client 掩盖，真跑起来才炸。这里直接拿真签名卡住。
    """
    import inspect

    from telethon import TelegramClient

    signature = inspect.signature(TelegramClient.forward_messages)
    signature.bind(
        object(), entity=object(), messages=[1, 2], from_peer=object(),
        as_album=True,
    )


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


# ---------- 账号被限制建群时的出路：绑已有群组 ----------
class FakeEntity:
    def __init__(self, eid, *, megagroup=False, broadcast=False, creator=False,
                 post_messages=False, access_hash=777, title=""):
        self.id = eid
        self.megagroup = megagroup
        self.broadcast = broadcast
        self.creator = creator
        self.access_hash = access_hash
        self.title = title
        self.admin_rights = (
            type("Rights", (), {"post_messages": True}) if post_messages else None
        )


class FakeDialog:
    def __init__(self, entity, name, *, is_group=False, is_channel=False):
        self.entity = entity
        self.name = name
        self.is_group = is_group
        self.is_channel = is_channel


class FakeDialogsClient:
    def __init__(self, dialogs, entity=None):
        self.dialogs = dialogs
        self.entity = entity

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def get_entity(self, chat_id):
        return self.entity


def make_session(store, client):
    from app import config as config_module
    from app.tg import TelegramSession

    session = TelegramSession(config_module.Config(), store)

    async def _authorized():
        return client

    session.require_authorized = _authorized
    return session


def test_candidates_include_where_we_can_post(store):
    client = FakeDialogsClient([
        # 普通群：也列（自己几个人用很常见）
        FakeDialog(FakeEntity(11), "普通群", is_group=True),
        FakeDialog(FakeEntity(22, megagroup=True), "超级群A", is_group=True),
        # 广播频道但没发帖权限：不列
        FakeDialog(FakeEntity(33, broadcast=True), "广播频道B", is_channel=True),
        # 自己是创建者的频道：可以
        FakeDialog(FakeEntity(44, broadcast=True, creator=True), "我的频道", is_channel=True),
        # 管理员且有发帖权：可以
        FakeDialog(FakeEntity(55, broadcast=True, post_messages=True), "合作频道", is_channel=True),
    ])
    session = make_session(store, client)
    chats = asyncio.run(session.list_forward_candidates())

    kinds = {c["title"]: c["kind"] for c in chats}
    assert kinds == {
        "普通群": "群",
        "超级群A": "超级群",
        "我的频道": "频道",
        "合作频道": "频道",
    }
    # 没发帖权限的广播频道不能当目标
    assert "广播频道B" not in kinds

    ids = {c["title"]: c["chat_id"] for c in chats}
    assert ids["普通群"] == 11                 # 普通群保留 Telethon 侧正数 id
    assert ids["超级群A"] == -10022


def test_bot_chat_id_converts_basic_group():
    """Bot API 里普通群的 chat_id 是 Telethon 那个正数 id 的负数。"""
    assert forwarder.bot_chat_id({
        "kind": "channel", "chat_id": -1001234567890, "raw_id": 1234567890,
    }) == -1001234567890
    assert forwarder.bot_chat_id({
        "kind": "chat", "chat_id": 987654321, "raw_id": 987654321,
    }) == -987654321


def test_bind_existing_group_target(store):
    entity = FakeEntity(1234567890, megagroup=True, access_hash=555, title="我的超级群")
    session = make_session(store, FakeDialogsClient([], entity=entity))
    peer = asyncio.run(session.forward_target_peer(-1001234567890))
    assert peer["kind"] == "channel"
    assert peer["raw_id"] == 1234567890
    assert peer["chat_id"] == -1001234567890
    assert peer["access_hash"] == 555
    assert peer["title"] == "我的超级群"


def test_basic_chat_target_uses_input_peer_chat(store):
    entity = FakeEntity(987, megagroup=False, broadcast=False, title="小群")
    session = make_session(store, FakeDialogsClient([], entity=entity))
    peer = asyncio.run(session.forward_target_peer(987))
    assert peer["kind"] == "chat"
    assert peer["chat_id"] == 987

    forwarder.save_target(
        store, 1, chat_id=peer["chat_id"], raw_id=peer["raw_id"],
        access_hash=peer.get("access_hash"), title=peer["title"], kind=peer["kind"],
    )
    client = FakeClient()
    asyncio.run(forwarder.forward_message(
        client, object(), FakeMessage(1, video=True), forwarder.load_target(store, 1),
    ))
    assert type(client.forwards[0]["entity"]).__name__ == "InputPeerChat"


def test_spamreported_error_is_explained():
    from app.tg import _friendly

    error = Exception(
        "You're spamreported, you can't create channels or chats. "
        "(caused by CreateChannelRequest)"
    )
    text = _friendly(error)
    assert "spamreported" in text
    assert "@SpamBot" in text
    assert "绑定已有群组" in text


# ---------- bot 中转 ----------
class FakeBotForwardClient:
    """采集号这一侧：只记下把消息转给谁了。"""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.forwards: list[dict] = []

    async def get_messages(self, chat, ids=None):
        return []

    async def forward_messages(self, entity=None, messages=None, from_peer=None,
                               **kwargs):
        if self.error is not None:
            raise self.error
        self.forwards.append({"to": entity, "ids": list(messages or [])})
        return []


def test_forward_to_bot_targets_the_bot_not_the_group():
    target = {
        "mode": "bot",
        "chat_id": -1001234567890,        # 目标群（bot 才会用）
        "bot_username": "my_forward_bot",
        "bot_user_id": 777,
    }
    client = FakeBotForwardClient()
    result = asyncio.run(forwarder.forward_to_bot(
        client, object(), FakeMessage(9, video=True), target,
    ))
    assert result == {"ok": True, "count": 1}
    # 关键：转给 bot，不是转给那个群
    assert client.forwards[0]["to"] == "my_forward_bot"


def test_forward_to_bot_without_binding_reports_reason():
    client = FakeBotForwardClient()
    result = asyncio.run(forwarder.forward_to_bot(
        client, object(), FakeMessage(1, video=True), {"mode": "bot", "chat_id": -1},
    ))
    assert result["ok"] is False
    assert "绑定 bot" in result["error"]
    assert client.forwards == []


def test_forward_to_bot_explains_missing_start():
    client = FakeBotForwardClient(error=Exception("PeerIdInvalidError: Could not find the input entity"))
    target = {"mode": "bot", "chat_id": -1, "bot_username": "b"}
    result = asyncio.run(forwarder.forward_to_bot(
        client, object(), FakeMessage(1, video=True), target,
    ))
    assert result["ok"] is False
    assert "/start" in result["error"]


def test_relay_target_and_stats_round_trip(store):
    assert forwarder.load_relay_target(store) == {}
    forwarder.save_relay_target(store, chat_id=-100777, title="搬运目标群")
    assert forwarder.load_relay_target(store)["chat_id"] == -100777

    forwarder.note_relay_result(store, ok=True, count=3)
    forwarder.note_relay_result(store, ok=False, error="chat not found")
    stats = forwarder.load_relay_stats(store)
    assert stats["copied"] == 3
    assert stats["failed"] == 1
    assert "chat not found" in stats["last_error"]

    forwarder.clear_relay_target(store)
    assert forwarder.load_relay_target(store) == {}


# ---------- Bot API 小工具 ----------
def test_bot_api_error_is_translated():
    from app import bot_api

    assert "找不到这个群" in bot_api._friendly("Bad Request: chat not found")
    assert "没有发消息权限" in bot_api._friendly("Bad Request: not enough rights")
    assert "限流" in bot_api._friendly("Too Many Requests: retry after 12")


def test_copy_messages_uses_batch_api(monkeypatch):
    from app import bot_api

    calls: list[tuple[str, dict]] = []

    async def fake_call(token, method, payload=None, **kwargs):
        calls.append((method, payload or {}))
        return [{"message_id": 1}, {"message_id": 2}] if method == "copyMessages" else {"message_id": 9}

    monkeypatch.setattr(bot_api, "call", fake_call)

    asyncio.run(bot_api.copy_messages("t", -100, 42, [1, 2]))
    assert calls[0][0] == "copyMessages"
    assert calls[0][1]["message_ids"] == [1, 2]

    calls.clear()
    asyncio.run(bot_api.copy_messages("t", -100, 42, [7]))
    assert calls[0][0] == "copyMessage"
    assert calls[0][1]["message_id"] == 7


# ---------- bot 收到视频后再发到群 ----------
def _make_bot(store):
    from app import config as config_module
    from app.telegram_bot import TelegramBotRunner

    return TelegramBotRunner(store, config_module.Config())


def test_bot_relay_copies_single_video(store):
    forwarder.save_relay_target(store, chat_id=-100777, title="目标群")
    bot = _make_bot(store)
    sent: list[tuple[str, dict]] = []

    async def fake_api_post(client, conf, method, payload):
        sent.append((method, payload))
        return {}

    bot._api_post = fake_api_post
    conf = {"token": "1:abc", "user_id": 42}
    message = {
        "message_id": 5,
        "chat": {"id": 42, "type": "private"},
        "video": {"file_id": "x"},
    }
    assert asyncio.run(bot._relay_video(message, conf, None)) is True
    assert sent[0][0] == "copyMessage"
    assert sent[0][1]["chat_id"] == -100777
    assert sent[0][1]["from_chat_id"] == 42
    assert forwarder.load_relay_stats(store)["copied"] == 1


def test_bot_ignores_messages_when_no_relay_target(store):
    bot = _make_bot(store)
    message = {
        "message_id": 5,
        "chat": {"id": 42, "type": "private"},
        "video": {"file_id": "x"},
    }
    assert asyncio.run(bot._relay_video(message, {"token": "1:a", "user_id": 42}, None)) is False


def test_bot_relay_batches_album(store, monkeypatch):
    from app import telegram_bot as tgb

    monkeypatch.setattr(tgb, "ALBUM_WAIT_SECONDS", 0.05)
    forwarder.save_relay_target(store, chat_id=-100777, title="目标群")
    bot = _make_bot(store)
    sent: list[tuple[str, dict]] = []

    async def fake_api_post(client, conf, method, payload):
        sent.append((method, payload))
        return {}

    bot._api_post = fake_api_post
    conf = {"token": "1:abc", "user_id": 42}

    def album_msg(msg_id: int) -> dict:
        return {
            "message_id": msg_id,
            "chat": {"id": 42, "type": "private"},
            "media_group_id": "777",
            "video": {"file_id": "x"},
        }

    async def main():
        await bot._relay_video(album_msg(10), conf, None)
        await bot._relay_video(album_msg(11), conf, None)
        await asyncio.gather(*bot._album_tasks.values())

    asyncio.run(main())
    assert len(sent) == 1                  # 两条消息只发一次
    assert sent[0][0] == "copyMessages"
    assert sent[0][1]["message_ids"] == [10, 11]


COLLECTOR_UID = 7680856969
ADMIN_UID = 7250823742


def _add_collector_account(store, user_id: int = COLLECTOR_UID) -> int:
    account = store.add_account(phone="+8600000000000", session="collector.session")
    aid = int(account["id"])
    store.update_account(aid, user_id=user_id)
    return aid


def test_relay_senders_include_collector_accounts(store):
    assert _add_collector_account(store) > 0
    bot = _make_bot(store)
    assert bot._relay_senders() == {COLLECTOR_UID}


def test_bot_relays_video_from_collector_account(store):
    """采集号和管理员号不是同一个时，采集号转发来的视频也要能收。"""
    _add_collector_account(store)
    forwarder.save_relay_target(store, chat_id=-100777, title="目标群")
    bot = _make_bot(store)
    sent: list[tuple[str, dict]] = []

    async def fake_api_post(client, conf, method, payload):
        sent.append((method, payload))
        return {}

    bot._api_post = fake_api_post
    conf = {"token": "1:abc", "user_id": ADMIN_UID}
    message = {
        "message_id": 5,
        "from": {"id": COLLECTOR_UID},
        "chat": {"id": COLLECTOR_UID, "type": "private"},
        "video": {"file_id": "x"},
    }
    asyncio.run(bot._handle_message(message, conf, CodeMerger(0), None))
    assert [m for m, _ in sent] == ["copyMessage"]
    assert sent[0][1]["chat_id"] == -100777


def test_collector_account_cannot_run_commands(store):
    """采集号的私聊只用来中转视频，不能拿它建秒传任务、跑 /cleanup。"""
    _add_collector_account(store)
    bot = _make_bot(store)
    sent: list[tuple[str, dict]] = []

    async def fake_api_post(client, conf, method, payload):
        sent.append((method, payload))
        return {}

    bot._api_post = fake_api_post
    conf = {"token": "1:abc", "user_id": ADMIN_UID}
    message = {
        "message_id": 6,
        "from": {"id": COLLECTOR_UID},
        "chat": {"id": COLLECTOR_UID, "type": "private"},
        "text": "/tasks",
    }
    asyncio.run(bot._handle_message(message, conf, CodeMerger(0), None))
    assert sent == []      # 既不回命令，也不建任务


def test_unknown_sender_is_still_ignored(store):
    _add_collector_account(store)
    bot = _make_bot(store)
    sent: list[tuple[str, dict]] = []

    async def fake_api_post(client, conf, method, payload):
        sent.append((method, payload))
        return {}

    bot._api_post = fake_api_post
    conf = {"token": "1:abc", "user_id": ADMIN_UID}
    message = {
        "message_id": 7,
        "from": {"id": 999999999},
        "chat": {"id": 999999999, "type": "private"},
        "text": "/tasks",
    }
    asyncio.run(bot._handle_message(message, conf, CodeMerger(0), None))
    assert sent == []


def test_bot_relay_failure_is_recorded(store):
    forwarder.save_relay_target(store, chat_id=-100777, title="目标群")
    bot = _make_bot(store)

    async def fake_api_post(client, conf, method, payload):
        raise RuntimeError("Bad Request: chat not found")

    bot._api_post = fake_api_post

    async def fake_send(client, conf, chat_id, text, **kwargs):
        return 1

    bot._send_message = fake_send
    conf = {"token": "1:abc", "user_id": 42}
    message = {
        "message_id": 5,
        "chat": {"id": 42, "type": "private"},
        "video": {"file_id": "x"},
    }
    # 不抛异常，失败只落库 + 提醒
    assert asyncio.run(bot._relay_video(message, conf, None)) is True
    stats = forwarder.load_relay_stats(store)
    assert stats["failed"] == 1
    assert "chat not found" in stats["last_error"]
