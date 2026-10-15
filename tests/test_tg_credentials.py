"""应用凭据（api_id / api_hash / phone）：数据库优先，config.toml 兜底。

TelegramSession 的其余部分都要连真实 Telegram，这里只测不需要网络的凭据逻辑。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import config as config_module
from app.store import Store
from app.tg import (
    API_HASH_RE,
    CREDENTIALS_KEY,
    LoginError,
    TelegramSession,
    missing_credentials,
    resolve_credentials,
)

GOOD_HASH = "0123456789abcdef0123456789abcdef"


def make(tmp_path, api_id=0, api_hash="", phone=""):
    cfg = config_module.Config(
        telegram=config_module.TelegramConfig(
            api_id=api_id, api_hash=api_hash, phone=phone,
            session=str(tmp_path / "s" / "collector"),
        ),
        storage=config_module.StorageConfig(db=str(tmp_path / "tg.db")),
        checker=config_module.CheckerConfig(enabled=False),
    )
    store = Store(cfg.db_path())
    store.init()
    return cfg, store, TelegramSession(cfg, store)


# ---------- 来源与回退 ----------
def test_config_used_when_db_empty(tmp_path):
    _, _, tg = make(tmp_path, 111, GOOD_HASH, "+8613800000000")
    cred = tg.credentials()
    assert cred["api_id"] == 111
    assert cred["api_hash"] == GOOD_HASH
    assert cred["phone"] == "+8613800000000"
    assert cred["source"] == "config.toml"


def test_db_wins_over_config(tmp_path):
    _, store, tg = make(tmp_path, 111, GOOD_HASH, "+8613800000000")
    store.set_json(
        CREDENTIALS_KEY, {"api_id": 222, "api_hash": "f" * 32, "phone": "+8613900000000"}
    )
    cred = tg.credentials()
    assert cred["api_id"] == 222
    assert cred["api_hash"] == "f" * 32
    assert cred["phone"] == "+8613900000000"
    assert cred["source"] == "管理页"


def test_falls_back_per_field(tmp_path):
    """数据库只填了 api_id 时，hash / phone 仍从 config.toml 取。"""
    _, store, tg = make(tmp_path, 111, GOOD_HASH, "+8613800000000")
    store.set_json(CREDENTIALS_KEY, {"api_id": 222, "api_hash": "", "phone": ""})
    cred = tg.credentials()
    assert cred["api_id"] == 222
    assert cred["api_hash"] == GOOD_HASH
    assert cred["phone"] == "+8613800000000"


def test_empty_when_nothing_set(tmp_path):
    _, _, tg = make(tmp_path)
    cred = tg.credentials()
    assert cred["api_id"] == 0
    assert cred["api_hash"] == ""
    assert cred["source"] == ""
    assert missing_credentials(cred) == ["api_id", "api_hash"]


def test_missing_ignores_phone(tmp_path):
    """phone 只有命令行登录才必需，不算缺。"""
    _, _, tg = make(tmp_path, 111, GOOD_HASH, "")
    assert missing_credentials(tg.credentials()) == []


# ---------- 建客户端 ----------
def test_build_client_refuses_without_credentials(tmp_path):
    _, _, tg = make(tmp_path)
    with pytest.raises(LoginError) as e:
        tg._build_client()
    assert "api_id" in str(e.value)
    assert "管理页" in str(e.value)


def test_build_client_uses_db_credentials(tmp_path):
    """客户端拿到的是数据库里的 api_id，而不是 config.toml 的。"""
    _, store, tg = make(tmp_path, 111, GOOD_HASH)
    store.set_json(CREDENTIALS_KEY, {"api_id": 333, "api_hash": "f" * 32, "phone": ""})
    client = tg._build_client()
    try:
        assert client.api_id == 333
    finally:
        client.session.close()


# ---------- 保存与校验 ----------
def test_save_normalizes_phone_and_persists(tmp_path):
    cfg, store, tg = make(tmp_path)
    cred = asyncio.run(tg.save_credentials(444, GOOD_HASH, "8613600000000"))
    assert cred["api_id"] == 444
    assert cred["phone"] == "+8613600000000"  # 自动补 +
    assert cred["source"] == "管理页"
    # 换一个实例读，确认真的落库了
    assert TelegramSession(cfg, store).credentials()["api_id"] == 444


def test_save_rejects_bad_api_hash(tmp_path):
    _, _, tg = make(tmp_path)
    with pytest.raises(LoginError) as e:
        asyncio.run(tg.save_credentials(1, "not-a-hash", ""))
    assert "32 位" in str(e.value)


def test_save_rejects_bad_api_id(tmp_path):
    _, _, tg = make(tmp_path)
    with pytest.raises(LoginError):
        asyncio.run(tg.save_credentials(-5, GOOD_HASH, ""))


def test_save_accepts_uppercase_hash(tmp_path):
    _, _, tg = make(tmp_path)
    cred = asyncio.run(tg.save_credentials(1, GOOD_HASH.upper(), ""))
    assert API_HASH_RE.fullmatch(cred["api_hash"])


def test_save_disconnects_so_client_is_rebuilt(tmp_path):
    """凭据改了必须断开，否则还在用旧 api_id 的连接。"""
    _, _, tg = make(tmp_path)
    asyncio.run(tg.save_credentials(1, GOOD_HASH, ""))
    assert tg._client is None


# ---------- 回归：监听器 / 命令行登录也要认得管理页填的凭据 ----------
def test_resolve_credentials_sees_admin_saved(tmp_path):
    cfg, store, tg = make(tmp_path)
    asyncio.run(tg.save_credentials(555, GOOD_HASH, "+8613800000000"))
    cred = resolve_credentials(cfg, store)
    assert cred["api_id"] == 555
    assert missing_credentials(cred) == []


# ---------- session 文件被采集进程占用时 ----------
def test_make_session_sets_busy_timeout(tmp_path):
    """Telethon 的 SQLiteSession 写死了 sqlite3.connect(...)，没传 timeout，
    用的是默认的 5 秒。两个进程共用一个 session 文件时很容易撞上
    database is locked，所以补一个 30 秒的忙等。"""
    from app.tg import _make_session

    d = tmp_path / "s"
    d.mkdir()
    sess = _make_session(d / "collector")
    try:
        assert sess._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        sess.close()


class _LockedClient:
    """connect() 就抛 database is locked，模拟采集进程占着 session 文件。"""

    async def connect(self):
        raise sqlite3.OperationalError("database is locked")


def test_locked_session_gives_actionable_hint(tmp_path, monkeypatch):
    """回归：这个错误以前被说成「连接 Telegram 失败（走代理 …）」，
    会把人往代理方向带，实际问题在 session 文件被另一个进程占着。"""
    _, _, tg = make(tmp_path, 1, GOOD_HASH, "+8613800000000")
    monkeypatch.setattr(tg, "_build_client", lambda: _LockedClient())

    with pytest.raises(LoginError) as e:
        asyncio.run(tg.get_client())
    msg = str(e.value)
    assert "占用" in msg
    assert "代理" not in msg          # 别再提代理，那会误导
