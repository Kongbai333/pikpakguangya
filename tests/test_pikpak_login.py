"""PikPak 登录态：续期 → 密码自动重登 → 标记需要人工登录。

真实网络请求走不到（也不该在测试里发生），所以注入一个假 client，
只看「什么时候发什么请求、状态怎么落库」。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import integrations
from app.integrations import (
    PIKPAK_STATE_KEY,
    PIKPAK_TOKEN_URL,
    PikPakAuthError,
    pikpak_access_token,
)
from app.store import Store, iso, utcnow

USER = "user@example.com"
PASSWORD = "p@ssw0rd"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """按顺序吐出预置响应，并记下每一次请求。"""

    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json or {}))
        if not self.responses:
            raise AssertionError(f"没有预置响应了，却又请求了 {url}")
        return self.responses.pop(0)

    def get(self, url, headers=None):
        self.calls.append(("GET", url, {}))
        return self.responses.pop(0)

    # _maybe_client / _pikpak_storage 会把它当上下文管理器用
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "pikpak.db")
    s.init()
    return s


def seed(store, *, expires_in_seconds: int = 7200, password: str = "",
         refresh_token: str = "r1", access_token: str = "a1"):
    state = {
        "username": USER,
        "user_id": "u1",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": iso(utcnow() + timedelta(seconds=expires_in_seconds)),
        "updated_at": iso(utcnow()),
    }
    if password:
        state["password"] = password
    store.set_json(PIKPAK_STATE_KEY, state)
    return state


def state_of(store) -> dict:
    return store.get_json(PIKPAK_STATE_KEY, {}) or {}


def token_body(access: str = "a2", refresh: str = "r2", expires_in: int = 7200) -> dict:
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in}


# ---------- 未过期：不该白花一个请求 ----------
def test_fresh_token_is_used_without_any_request(store):
    seed(store)
    client = FakeClient()
    assert pikpak_access_token(store, client=client) == "a1"
    assert client.calls == []


def test_token_without_expiry_is_refreshed(store):
    """老库里没有 expires_at（这次改动之前登录的），不能傻用旧 token。"""
    seed(store)
    state = state_of(store)
    state.pop("expires_at")
    store.set_json(PIKPAK_STATE_KEY, state)

    client = FakeClient(FakeResponse(200, token_body()))
    assert pikpak_access_token(store, client=client) == "a2"
    assert [c[1] for c in client.calls] == [PIKPAK_TOKEN_URL]


# ---------- 续期 ----------
def test_expired_token_is_refreshed_and_expiry_recorded(store):
    seed(store, expires_in_seconds=-60)
    client = FakeClient(FakeResponse(200, token_body(expires_in=7200)))

    assert pikpak_access_token(store, client=client) == "a2"
    state = state_of(store)
    assert state["refresh_token"] == "r2"
    assert state["expires_at"] > iso(utcnow())      # ISO 时间可直接比大小
    assert state["login_required"] is False
    assert state["last_error"] == ""


# ---------- 续期失败 → 用保存的密码自动重登 ----------
def test_refresh_failure_falls_back_to_password_relogin(store):
    seed(store, expires_in_seconds=-60, password=PASSWORD)
    client = FakeClient(
        FakeResponse(403, {"error": "invalid_grant"}, "forbidden"),   # 续期
        FakeResponse(200, {"captcha_token": "c1"}),                    # captcha/init
        FakeResponse(200, {**token_body("a9", "r9"), "sub": "u1"}),     # signin
    )

    assert pikpak_access_token(store, client=client) == "a9"
    urls = [c[1] for c in client.calls]
    assert urls == [
        PIKPAK_TOKEN_URL,
        integrations.PIKPAK_CAPTCHA_URL,
        integrations.PIKPAK_LOGIN_URL,
    ]
    state = state_of(store)
    assert state["access_token"] == "a9"
    assert state["refresh_token"] == "r9"
    assert state["password"] == PASSWORD          # 继续留着，下次还能重登
    assert state["login_required"] is False
    assert state["last_auto_login_at"] == ""      # 成功后清掉冷却标记


def test_no_saved_password_marks_login_required(store):
    """没存密码就只能人工登录，但要把原因说清楚（旧版这里只会静默 403）。"""
    seed(store, expires_in_seconds=-60)
    client = FakeClient(FakeResponse(403, {"error": "invalid_grant"}, "forbidden"))

    with pytest.raises(PikPakAuthError) as err:
        pikpak_access_token(store, client=client)
    detail = str(err.value)
    assert "403" in detail
    assert "没有保存密码" in detail

    state = state_of(store)
    assert state["login_required"] is True
    assert "403" in state["last_error"]
    assert len(client.calls) == 1                  # 没有密码就不该去轰登录接口


def test_wrong_password_is_reported_not_retried_immediately(store):
    seed(store, expires_in_seconds=-60, password="wrong")
    client = FakeClient(
        FakeResponse(403, {}, "forbidden"),
        FakeResponse(200, {"captcha_token": "c1"}),
        FakeResponse(400, {
            "error": "invalid_account_or_password",
            "error_description": "帐号或密码错误",
        }, "帐号或密码错误"),
    )
    with pytest.raises(PikPakAuthError) as err:
        pikpak_access_token(store, client=client)
    assert "帐号或密码错误" in str(err.value)
    assert "invalid_account_or_password" in str(err.value)
    assert state_of(store)["login_required"] is True

    # 冷却期内再要 token，只允许试一次续期，不能再打登录接口
    again = FakeClient(FakeResponse(403, {}, "forbidden"))
    with pytest.raises(PikPakAuthError) as err2:
        pikpak_access_token(store, client=again)
    assert "半小时" in str(err2.value)
    assert len(again.calls) == 1


# ---------- 状态展示 ----------
def test_status_reflects_login_required(store):
    seed(store, expires_in_seconds=-60, password=PASSWORD)
    with pytest.raises(PikPakAuthError):
        # 续期失败 + 密码重登也失败（比如 PikPak 返回 5xx）
        pikpak_access_token(store, client=FakeClient(
            FakeResponse(403, {}, "forbidden"),
            FakeResponse(500, {}, "boom"),
        ))

    status = integrations._pikpak_status(store)
    assert status["connected"] is True          # 有密码，理论上还能连
    assert status["needs_login"] is True         # 但现在确实用不了
    assert status["auto_login"] is True
    assert status["last_error"]


def test_status_without_login(store):
    status = integrations._pikpak_status(store)
    assert status["connected"] is False
    assert status["needs_login"] is False
    assert status["auto_login"] is False


# ---------- 记住密码的开关 ----------
def test_login_can_clear_saved_password(store):
    seed(store)
    body = {**token_body("a3", "r3"), "sub": "u1"}
    integrations._store_pikpak_login(
        store, username=USER, password=PASSWORD, body=body, remember=False,
    )
    assert "password" not in state_of(store)
    assert state_of(store)["access_token"] == "a3"

    integrations._store_pikpak_login(
        store, username=USER, password=PASSWORD, body=body, remember=True,
    )
    assert state_of(store)["password"] == PASSWORD


# ---------- 保活 ----------
def test_keepalive_skips_when_not_logged_in(store):
    assert integrations.pikpak_keepalive(store)["skipped"] == "未登录"


def test_keepalive_reports_error_without_raising(store):
    seed(store, expires_in_seconds=-60, password=PASSWORD)
    # 直接调保活：它会自己连网，这里把 _client 换掉，别真发请求
    original = integrations._client
    integrations._client = lambda proxy: FakeClient(
        FakeResponse(403, {}, "forbidden"),
        FakeResponse(502, {}, "bad gateway"),
    )
    try:
        result = integrations.pikpak_keepalive(store)
    finally:
        integrations._client = original
    assert result["ok"] is False
    assert result["error"]
