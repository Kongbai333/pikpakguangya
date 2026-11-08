"""账号采集（app/collector.py）：每个账号一套独立 session / 心跳 / 游标。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import collector as collector_module
from app import config as config_module
from app import listener as L
from app.checkqueue import CheckQueue
from app.collector import Collector
from app.store import Store, iso, utcnow

CHAT_ID = -100111


class FakeClient:
    """够用的 Telethon 替身：事件注册、登录状态、群解析、回溯用的迭代器。"""

    def __init__(self, authorized=True):
        self.handlers: list = []
        self.authorized = authorized
        self.entities_requested: list = []
        self.forwards: list = []

    # --- 事件 ---
    def on(self, event):  # noqa: ANN001
        def deco(fn):
            self.handlers.append(fn)
            return fn
        return deco

    def remove_event_handler(self, fn) -> None:  # noqa: ANN001
        if fn in self.handlers:
            self.handlers.remove(fn)

    # --- 账号 ---
    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):  # noqa: ANN201
        return SimpleNamespace(username="tester", first_name="测试", id=1)

    # --- 群 ---
    async def get_entity(self, spec):  # noqa: ANN001, ANN201
        self.entities_requested.append(spec)
        return SimpleNamespace(id=CHAT_ID, title="测试群")

    def iter_messages(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        async def gen():
            if False:
                yield None
        return gen()

    async def get_messages(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return []

    # --- 转发 ---
    async def forward_messages(self, entity=None, messages=None, from_peer=None,
                               **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.forwards.append({
            "entity": entity, "ids": list(messages or []), "from_peer": from_peer,
        })
        return []


class FakeTg:
    """假的 TelegramSession：只暴露 get_client() / disconnect()。"""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    async def get_client(self):
        self.calls += 1
        return self.client

    async def disconnect(self) -> None:
        return None


def make(tmp_path, *, authorized=True, credentials=True, with_proxy=True,
         watched=True, enabled=False):
    cfg = config_module.Config(
        storage=config_module.StorageConfig(db=str(tmp_path / "c.db")),
        checker=config_module.CheckerConfig(enabled=False),
    )
    store = Store(cfg.db_path())
    store.init()
    if credentials:
        store.set_json(
            "telegram.credentials",
            {"api_id": 12345, "api_hash": "a" * 32, "phone": "+8613800000000"},
        )
    proxy_id = 0
    if with_proxy:
        proxy = store.add_proxy(
            name="测试代理", proxy_type="socks5", addr="127.0.0.1", port=1080
        )
        proxy_id = proxy["id"]
    account = store.add_account(
        phone="+8613800000000", session="test.session", proxy_id=proxy_id
    )
    if enabled:
        store.update_account(account["id"], collect_enabled=1)
    if watched:
        store.add_watched(
            CHAT_ID, "测试群", "testgroup", "群", True, account_id=account["id"]
        )
    client = FakeClient(authorized=authorized)
    tg = FakeTg(client)
    queue = CheckQueue(store, cfg.checker, None)
    return Collector(account, tg, store, cfg, queue), store, tg, client


# ---------- 前置检查 ----------
def test_same_video_is_not_forwarded_twice(tmp_path):
    """重启后补偿回溯会重扫最近 60 分钟，同一条视频不能转两次。"""
    from app import forwarder

    c, store, _, client = make(tmp_path)
    store.set_forward_watched(CHAT_ID, True, account_id=c.account_id)
    forwarder.save_target(
        store, c.account_id, chat_id=-100777, raw_id=0, access_hash=0,
        title="目标群", kind="channel", mode="bot",
        bot_username="my_bot", bot_user_id=1,
    )
    msg = SimpleNamespace(id=321, grouped_id=None, video=object(), caption=None)
    chat = SimpleNamespace(id=CHAT_ID, title="测试群")

    asyncio.run(c._forward_media(client, chat, msg, set()))
    # 模拟重启后的补偿回溯：内存里什么都没有，但库里记着已经转过
    asyncio.run(c._forward_media(client, chat, msg, set()))

    assert len(client.forwards) == 1
    assert forwarder.load_stats(store, c.account_id)["seen"] == 1


def test_concurrent_album_triggers_only_one_forward(tmp_path):
    """相册里的 3 条消息会**并发**进来，每条都去转整组 —— 只能成功一次。

    Telethon 的 handler 并发派发，以前 3 个协程都会在“记录已转”之前通过检查，
    结果同一个视频在目标群出现 3 次。
    """
    from app import forwarder

    c, store, _, client = make(tmp_path)
    store.set_forward_watched(CHAT_ID, True, account_id=c.account_id)
    forwarder.save_target(
        store, c.account_id, chat_id=-100777, raw_id=0, access_hash=0,
        title="目标群", kind="channel", mode="bot",
        bot_username="my_bot", bot_user_id=1,
    )
    chat = SimpleNamespace(id=CHAT_ID, title="测试群")
    msgs = [
        SimpleNamespace(id=100 + i, grouped_id=777, video=object(), caption=None)
        for i in range(3)
    ]

    class SlowClient(type(client)):
        """真实网络会 await，这里必须让出控制权才能复现并发。"""

        async def get_messages(self, *a, **kw):
            await asyncio.sleep(0)
            return await super().get_messages(*a, **kw)

        async def forward_messages(self, entity=None, messages=None,
                                   from_peer=None, **kwargs):
            await asyncio.sleep(0)
            return await super().forward_messages(
                entity=entity, messages=messages, from_peer=from_peer, **kwargs
            )

    slow = SlowClient()

    async def main():
        await asyncio.gather(*[
            c._forward_media(slow, chat, m, set()) for m in msgs
        ])

    asyncio.run(main())
    assert len(slow.forwards) == 1        # 只转了一次整组
    assert forwarder.load_stats(store, c.account_id)["seen"] == 1


def test_failed_forward_can_be_retried(tmp_path):
    """转发失败要释放预约：否则失败一次就永远不再重试。"""
    from app import forwarder

    c, store, _, client = make(tmp_path)
    store.set_forward_watched(CHAT_ID, True, account_id=c.account_id)
    forwarder.save_target(
        store, c.account_id, chat_id=-100777, raw_id=0, access_hash=0,
        title="目标群", kind="channel", mode="bot",
        bot_username="my_bot", bot_user_id=1,
    )
    chat = SimpleNamespace(id=CHAT_ID, title="测试群")
    msg = SimpleNamespace(id=321, grouped_id=None, video=object(), caption=None)

    async def boom(*a, **kw):
        raise RuntimeError("network down")

    client.forward_messages = boom
    asyncio.run(c._forward_media(client, chat, msg, set()))
    assert forwarder.load_stats(store, c.account_id)["failed"] == 1
    assert c._inflight == set()             # 预约已释放

    # 恢复后重试一次应当能成功（没被误记成“已转过”）
    client.forwards.clear()

    async def ok(entity=None, messages=None, from_peer=None, **kw):
        client.forwards.append({"entity": entity, "ids": list(messages or [])})
        return []

    client.forward_messages = ok
    asyncio.run(c._forward_media(client, chat, msg, set()))
    assert len(client.forwards) == 1
    assert forwarder.was_forwarded(store, c.account_id, CHAT_ID, 321) is True


def test_start_blocked_by_preflight(tmp_path):
    c, _, _, _ = make(tmp_path, credentials=False, with_proxy=False, watched=False)

    with pytest.raises(ValueError) as e:
        asyncio.run(c.start())
    assert "不能启动采集" in str(e.value)
    assert c.running is False


def test_start_blocked_when_not_authorized(tmp_path):
    c, _, _, _ = make(tmp_path, authorized=False)
    with pytest.raises(ValueError) as e:
        asyncio.run(c.start())
    assert "还没登录" in str(e.value)


def test_start_without_proxy_is_allowed(tmp_path):
    """没绑代理就是直连，不该被拦住。

    部署在能直连 Telegram 的机器上（海外 VPS、部分家宽）根本不需要代理。
    以前 preflight 强制要求绑代理，这类机器上采集永远起不来。
    """
    c, _, _, _ = make(tmp_path, with_proxy=False)

    async def main():
        st = await c.start()
        assert st["running"] is True
        await c.stop()

    asyncio.run(main())


# ---------- 起停 ----------
def test_start_registers_handlers_and_writes_state(tmp_path):
    c, store, tg, client = make(tmp_path)

    async def main():
        st = await c.start()
        assert st["running"] is True
        assert "数据库（" in st["origin"]
        # 新消息 + 消息编辑，两个 handler
        assert len(client.handlers) == 2
        # 心跳和状态按账号落库，管理页据此显示「在运行」
        assert store.get_json(L.heartbeat_key(c.account_id))["at"]
        assert store.get_json(L.status_key(c.account_id))["state"] == "running"
        assert store.get_json(L.status_key(c.account_id))["mode"] == "embedded"
        await c.stop()

    asyncio.run(main())


def test_stop_cleans_up(tmp_path):
    c, store, _, client = make(tmp_path)

    async def main():
        await c.start()
        out = await c.stop()
        assert out["stopped"] is True
        assert out["how"] == "embedded"
        assert c.running is False
        assert client.handlers == []                        # handler 摘干净了
        assert store.get_json(L.heartbeat_key(c.account_id)) == {}
        assert store.get_json(L.status_key(c.account_id))["state"] == "stopped"

    asyncio.run(main())


def test_start_is_idempotent(tmp_path):
    c, _, _, client = make(tmp_path)

    async def main():
        await c.start()
        again = await c.start()
        assert again.get("already_running") is True
        assert len(client.handlers) == 2        # 不会重复注册
        await c.stop()

    asyncio.run(main())


def test_stop_when_not_running(tmp_path):
    c, _, _, _ = make(tmp_path)
    out = asyncio.run(c.stop())
    assert out["stopped"] is False
    assert "没在跑" in out["reason"]


# ---------- 每个账号一套 session ----------
def test_uses_its_own_session_client(tmp_path):
    """账号采集走自己的 TelegramSession；session 文件按账号分开，互不抢锁。"""
    c, _, tg, client = make(tmp_path)

    async def main():
        await c.start()
        assert await tg.get_client() is client
        await c.stop()

    asyncio.run(main())


def test_heartbeat_keeps_ticking(tmp_path):
    """心跳是「还在采」的唯一依据，得一直在写。"""
    c, store, _, _ = make(tmp_path)

    async def main():
        await c.start()
        first = store.get_json(L.heartbeat_key(c.account_id))["at"]
        # 手动再打一次，模拟心跳 task 到点
        await asyncio.sleep(0)
        L._beat(store, c.account_id)
        assert store.get_json(L.heartbeat_key(c.account_id))["at"] >= first
        await c.stop()

    asyncio.run(main())


def test_status_shape(tmp_path):
    c, _, _, _ = make(tmp_path)
    st = c.status()
    assert st["embedded"] is True
    assert st["account_id"] == c.account_id
    assert st["running"] is False
    assert st["chats"] == []


def test_heartbeat_fresh(tmp_path):
    c, store, _, _ = make(tmp_path)
    assert c.heartbeat_fresh() is False
    store.set_json(L.heartbeat_key(c.account_id), {"at": iso(utcnow()), "pid": 1})
    assert c.heartbeat_fresh() is True


# ---------- 单条消息处理：重试 + 交给补偿回溯兜底 ----------
def test_ingest_retries_transient_failure(tmp_path, monkeypatch):
    """瞬时错误（DB 被占用、Telegram 抖一下）重试一两次就该过去。"""
    c, _, _, _ = make(tmp_path)
    calls = {"n": 0}

    async def flaky(obj, chat, store, merger, cfg, account_id):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is locked")
        return {"saved": []}

    monkeypatch.setattr(collector_module.L, "_ingest", flaky)
    monkeypatch.setattr(collector_module, "INGEST_RETRY_SLEEP", 0)

    asyncio.run(
        c._ingest_once(SimpleNamespace(id=77), SimpleNamespace(id=CHAT_ID), None, "新消息")
    )
    assert calls["n"] == 3
    assert c._already_done(CHAT_ID, 77) is True


def test_failed_message_is_left_for_catchup(tmp_path, monkeypatch):
    """重试都过不去的消息不能算「处理过」—— 否则补偿回溯也会跳过它，就真漏了。"""
    c, _, _, _ = make(tmp_path)

    async def boom(*a, **kw):  # noqa: ANN002, ANN003
        raise RuntimeError("boom")

    monkeypatch.setattr(collector_module.L, "_ingest", boom)
    monkeypatch.setattr(collector_module, "INGEST_RETRY_SLEEP", 0)

    asyncio.run(
        c._ingest_once(SimpleNamespace(id=5), SimpleNamespace(id=CHAT_ID), None, "新消息")
    )
    assert c._already_done(CHAT_ID, 5) is False


def test_done_ids_is_bounded(tmp_path, monkeypatch):
    """每群记住的 id 有上限，长时间运行不会把内存撑爆。"""
    c, _, _, _ = make(tmp_path)
    monkeypatch.setattr(collector_module, "DONE_IDS_PER_CHAT", 3)
    for i in range(1, 6):
        c._mark_done(CHAT_ID, i)
    assert c._already_done(CHAT_ID, 5) is True
    assert c._already_done(CHAT_ID, 1) is False     # 最早的被挤掉
    assert c._already_done(-999, 5) is False        # 没见过的群


# ---------- 管理者：按开关自动拉起 ----------
def _manager(tmp_path, monkeypatch):
    """一个账号开采集 + 一个账号关着，Telegram 全部换成假的。"""
    cfg = config_module.Config(
        storage=config_module.StorageConfig(db=str(tmp_path / "m.db")),
        checker=config_module.CheckerConfig(enabled=False),
    )
    store = Store(cfg.db_path())
    store.init()
    store.set_json(
        "telegram.credentials",
        {"api_id": 12345, "api_hash": "a" * 32, "phone": "+8613800000000"},
    )
    proxy = store.add_proxy(
        name="测试代理", proxy_type="socks5", addr="127.0.0.1", port=1080
    )
    first = store.add_account(
        phone="+8613800000000", session="first.session", proxy_id=proxy["id"]
    )
    second = store.add_account(phone="+8613900000000", session="second.session")
    store.update_account(first["id"], collect_enabled=1)
    store.add_watched(
        CHAT_ID, "测试群", "testgroup", "群", True, account_id=first["id"]
    )
    monkeypatch.setattr(
        collector_module, "TelegramSession",
        lambda cfg, store, account=None: FakeTg(FakeClient()),
    )
    return collector_module.CollectorManager(store, cfg), store, first, second


async def _until(pred, *, tries=150, step=0.02):
    """等到条件成立（看门狗是按间隔跑的，测试里把间隔调得很小）。"""
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


def test_manager_starts_only_enabled_accounts(tmp_path, monkeypatch):
    manager, _, first, second = _manager(tmp_path, monkeypatch)

    async def main():
        results = await manager.start_enabled()
        assert [r["account_id"] for r in results] == [first["id"]]
        assert manager.is_running(first["id"]) is True
        assert manager.is_running(second["id"]) is False
        await manager.shutdown()
        assert manager.running is False

    asyncio.run(main())


def test_watchdog_restarts_a_dead_collector(tmp_path, monkeypatch):
    """采集任务自己挂掉（断网、session 被踢）时自动拉起，不用重启进程。"""
    manager, _, first, _ = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(collector_module, "WATCHDOG_SECONDS", 0.05)

    async def main():
        await manager.start_enabled()
        worker = manager.worker(first["id"])
        assert worker.running is True

        # 模拟任务自己结束（不是用户点的停止，所以采集开关还是 1）
        worker._stop.set()
        assert await _until(lambda: not worker.running) is True

        manager.start_watchdog()
        try:
            assert await _until(lambda: manager.is_running(first["id"])) is True
        finally:
            await manager.stop_watchdog()
        await manager.shutdown()

    asyncio.run(main())


def test_watchdog_does_not_fight_the_user(tmp_path, monkeypatch):
    """用户点了「停止采集」，开关会被置 0，看门狗不能又给拉起来。"""
    manager, store, first, _ = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(collector_module, "WATCHDOG_SECONDS", 0.05)

    async def main():
        await manager.start_enabled()
        store.update_account(first["id"], collect_enabled=0)
        await manager.stop_account(first["id"])

        manager.start_watchdog()
        try:
            for _ in range(10):
                await asyncio.sleep(0.03)
            assert manager.is_running(first["id"]) is False
        finally:
            await manager.stop_watchdog()
        await manager.shutdown()

    asyncio.run(main())


def test_watchdog_backs_off_when_start_keeps_failing(tmp_path, monkeypatch):
    """起不来的账号要退避，不能每轮都重试、把日志刷爆。"""
    manager, store, first, _ = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(collector_module, "WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(collector_module, "WATCHDOG_MAX_BACKOFF", 0.5)
    calls = {"n": 0}

    async def always_fail(account_id):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise ValueError("起不来（比如群勾选被取消了）")

    monkeypatch.setattr(manager, "start_account", always_fail)

    async def main():
        await manager.start_enabled()
        await manager.stop_account(first["id"])

        manager.start_watchdog()
        try:
            # 0.3 秒 ≈ 6 个巡检周期。没有退避的话会试 6 次，退避后只试 2 次左右。
            for _ in range(15):
                await asyncio.sleep(0.02)
            assert 1 <= calls["n"] <= 4, f"退避没生效，试了 {calls['n']} 次"
            assert manager.is_running(first["id"]) is False
        finally:
            await manager.stop_watchdog()
        await manager.shutdown()

    asyncio.run(main())
