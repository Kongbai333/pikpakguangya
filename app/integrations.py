"""第三方网盘账号接入：PikPak 登录 + 光鸭 Token。"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import pikpak_api
from app.guangya_login import GuangyaAuth, GuangyaLoginError
from app.proxy import ProxyConfig
from app.routers.context import WebContext
from app.store import iso, utcnow

log = logging.getLogger("integrations")

PIKPAK_STATE_KEY = "integrations.pikpak"

PIKPAK_CLIENT_ID = "YNxT9w7GMdWvEOKa"
PIKPAK_CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"
PIKPAK_CAPTCHA_URL = "https://user.mypikpak.com/v1/shield/captcha/init"
PIKPAK_LOGIN_URL = "https://user.mypikpak.com/v1/auth/signin"
PIKPAK_TOKEN_URL = "https://user.mypikpak.com/v1/auth/token"
PIKPAK_ABOUT_URL = "https://api-drive.mypikpak.com/drive/v1/about"

# access_token 提前 5 分钟算过期：宁可比 PikPak 早一点换，别让请求卡在临界点上
PIKPAK_TOKEN_MARGIN = timedelta(minutes=5)
# 自动重登失败后多久才允许再试：密码改过了还反复硬试，容易被风控
PIKPAK_RETRY_COOLDOWN = timedelta(minutes=30)


class PikPakAuthError(ValueError):
    """PikPak 登录态用不了：续期失败，自动重登也没成。"""

# 容量缓存：进设置页先给缓存值，过期只在后台线程里刷新，接口不等网盘。
STORAGE_TTL = 120.0
PIKPAK_STORAGE_CACHE = "integrations.cache.pikpak_storage"
GUANGYA_STORAGE_CACHE = "integrations.cache.guangya_storage"
_STORAGE_LOCKS = {"pikpak": threading.Lock(), "guangya": threading.Lock()}


class PikPakLoginIn(BaseModel):
    username: str = ""
    password: str = ""
    # 记住密码：掉线后用密码自动重登。密码明文存在本机数据库里，等同代理密码。
    remember: bool = True


class GuangyaTokenIn(BaseModel):
    token: str = ""


class GuangyaCodeIn(BaseModel):
    phone: str = ""


class GuangyaLoginIn(BaseModel):
    code: str = ""


class GuangyaFolderIn(BaseModel):
    folder_id: str = ""
    folder_path: str = "/"


def _identity_meta(username: str) -> dict[str, str]:
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", username):
        return {"email": username}
    if re.match(r"^\d{8,18}$", username):
        return {"phone_number": username}
    return {"username": username}


def _client(proxy: ProxyConfig | None) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "trust_env": False,
        "timeout": 25.0,
        "follow_redirects": True,
    }
    if proxy is not None and getattr(proxy, "enabled", False):
        kwargs["proxy"] = proxy.to_url()
    return httpx.Client(**kwargs)


def _headers(device_id: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://mypikpak.com",
        "Referer": "https://mypikpak.com/",
        "x-device-id": device_id,
        "x-client-id": PIKPAK_CLIENT_ID,
    }


def _mask_account(value: str) -> str:
    value = value or ""
    if "@" in value:
        name, domain = value.split("@", 1)
        return f"{name[:2]}***@{domain}"
    if len(value) > 6:
        return f"{value[:3]}***{value[-3:]}"
    return value


def _pikpak_state(store) -> dict:
    data = store.get_json(PIKPAK_STATE_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def _save_pikpak_state(store, **fields) -> dict:
    """读改写：只补自己的字段，别把别的线程刚写进去的新 token 冲掉。"""
    data = _pikpak_state(store)
    data.update(fields)
    store.set_json(PIKPAK_STATE_KEY, data)
    return data


def _expires_at(seconds: Any) -> str:
    """把 PikPak 返回的 expires_in（秒）换成绝对时间；没给就记空（视为需刷新）。"""
    try:
        secs = int(float(seconds or 0))
    except (TypeError, ValueError):
        secs = 0
    if secs <= 0:
        return ""
    return iso(utcnow() + timedelta(seconds=secs))


def _token_usable(data: dict) -> bool:
    """access_token 是否还在有效期内（留 PIKPAK_TOKEN_MARGIN 余量）。

    没记过期时间就当它不可用：多续一次只是多一个请求，
    而拿着过期 token 去请求会直接被回 401，后者更难看。
    """
    if not str(data.get("access_token") or ""):
        return False
    raw = str(data.get("expires_at") or "")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires - PIKPAK_TOKEN_MARGIN > utcnow()


def _response_detail(response: httpx.Response) -> str:
    """把失败响应压成一句能看懂的话：状态码 + PikPak 自己的错误描述。"""
    detail = (response.text or "").strip()
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict):
            parts = [
                str(body.get("error_code") or body.get("error") or ""),
                str(body.get("error_description") or body.get("message") or ""),
            ]
            text = " ".join(p for p in parts if p)
            if text:
                detail = text
    return f"HTTP {response.status_code} {detail}".strip()[:300]


@contextlib.contextmanager
def _maybe_client(proxy: ProxyConfig | None, client: httpx.Client | None):
    """没传 client 就自己建一个（测试里注入假 client，不去碰网络）。"""
    if client is not None:
        yield client
        return
    with _client(proxy) as http:
        yield http


def pikpak_signin(store, proxy: ProxyConfig | None = None, *,
                  username: str, password: str,
                  client: httpx.Client | None = None) -> dict:
    """账号密码登录：先换 captcha_token，再换 token。返回 PikPak 的原始响应体。"""
    device_id = pikpak_api.device_id(store)
    with _maybe_client(proxy, client) as http:
        captcha = http.post(
            PIKPAK_CAPTCHA_URL,
            headers=_headers(device_id),
            json={
                "client_id": PIKPAK_CLIENT_ID,
                "action": f"POST:{PIKPAK_LOGIN_URL}",
                "device_id": device_id,
                "meta": _identity_meta(username),
            },
        )
        if captcha.status_code >= 400:
            raise PikPakAuthError(
                f"PikPak 验证码初始化失败：{_response_detail(captcha)}"
            )
        captcha_token = str(captcha.json().get("captcha_token") or "")
        if not captcha_token:
            raise PikPakAuthError("PikPak 没有返回 captcha_token")

        response = http.post(
            PIKPAK_LOGIN_URL,
            headers=_headers(device_id),
            json={
                "client_id": PIKPAK_CLIENT_ID,
                "client_secret": PIKPAK_CLIENT_SECRET,
                "grant_type": "password",
                "username": username,
                "password": password,
                "captcha_token": captcha_token,
            },
        )
    if response.status_code >= 400:
        raise PikPakAuthError(f"PikPak 登录失败：{_response_detail(response)}")
    body = response.json()
    if body.get("error"):
        raise PikPakAuthError(
            str(body.get("error_description") or body.get("error"))
        )
    return body


def _store_pikpak_login(store, *, username: str, password: str, body: dict,
                        remember: bool = True) -> dict:
    """登录成功后落库。remember=False 把之前存的密码也清掉。"""
    access_token = str(body.get("access_token") or "")
    refresh_token = str(body.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise PikPakAuthError("PikPak 登录返回缺少 token")
    fields: dict[str, Any] = {
        "username": username,
        "user_id": str(body.get("sub") or ""),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _expires_at(body.get("expires_in")),
        "updated_at": iso(utcnow()),
        "login_required": False,
        "last_error": "",
        "last_auto_login_at": "",
    }
    if remember and password:
        # 明文存：PikPak 没有免密登录，不存密码就无从「自动重登」
        fields["password"] = password
    data = _save_pikpak_state(store, **fields)
    if not (remember and password):
        data.pop("password", None)
        store.set_json(PIKPAK_STATE_KEY, data)
    return data


def _pikpak_refresh_token(store, proxy: ProxyConfig | None = None, *,
                          client: httpx.Client | None = None) -> str:
    """用 refresh_token 换新的 access_token。失败一律抛 PikPakAuthError。"""
    data = _pikpak_state(store)
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token:
        raise PikPakAuthError("还没有 PikPak 登录态，请先在设置里登录")
    device_id = pikpak_api.device_id(store)
    with _maybe_client(proxy, client) as http:
        response = http.post(
            PIKPAK_TOKEN_URL,
            headers=_headers(device_id),
            json={
                "client_id": PIKPAK_CLIENT_ID,
                "client_secret": PIKPAK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if response.status_code >= 400:
        raise PikPakAuthError(f"PikPak 续期失败：{_response_detail(response)}")
    body = response.json()
    access_token = str(body.get("access_token") or "")
    if not access_token:
        raise PikPakAuthError(
            f"PikPak 续期没返回 access_token：{str(body)[:200]}"
        )
    _save_pikpak_state(
        store,
        access_token=access_token,
        refresh_token=str(body.get("refresh_token") or refresh_token),
        expires_at=_expires_at(body.get("expires_in")),
        updated_at=iso(utcnow()),
        login_required=False,
        last_error="",
    )
    return access_token


def _relogin_in_cooldown(data: dict) -> bool:
    raw = str(data.get("last_auto_login_at") or "")
    if not raw:
        return False
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return utcnow() - at < PIKPAK_RETRY_COOLDOWN


def _pikpak_password_relogin(store, proxy: ProxyConfig | None = None, *,
                             client: httpx.Client | None = None) -> str:
    """refresh_token 活不了了，用存下来的密码重新登录一次。

    密码存的是明文（就存在本机 pikpak.db 里，和代理密码一个级别），
    没有密码就只能等人工登录 —— 这里把话说清楚，别假装成功。
    """
    data = _pikpak_state(store)
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    if not username or not password:
        raise PikPakAuthError(
            "PikPak 登录态已失效，而且没有保存密码，只能手动重新登录"
        )
    if _relogin_in_cooldown(data):
        raise PikPakAuthError(
            "PikPak 自动重登刚失败过，半小时后再试（防止反复硬试被风控）"
        )
    # 先记时间再发请求：并发的另一个线程看到时间就会退让，不会一起轰登录接口
    _save_pikpak_state(store, last_auto_login_at=iso(utcnow()))
    body = pikpak_signin(
        store, proxy, username=username, password=password, client=client,
    )
    _store_pikpak_login(store, username=username, password=password, body=body)
    log.info("PikPak 登录态失效，已用保存的密码自动重登")
    return str(body.get("access_token") or "")


def pikpak_access_token(store, proxy: ProxyConfig | None = None, *,
                        force: bool = False,
                        client: httpx.Client | None = None) -> str:
    """拿一个能用的 access_token。

    顺序：未过期直接用 → refresh_token 续期 → 存了密码就自动重登。
    全都不行时把原因记进状态（login_required / last_error）再抛错，
    让设置页和 Bot 能说清「为什么说我不在线」。
    """
    data = _pikpak_state(store)
    if not force and _token_usable(data):
        return str(data["access_token"])

    errors: list[str] = []
    if data.get("refresh_token"):
        try:
            return _pikpak_refresh_token(store, proxy, client=client)
        except PikPakAuthError as e:
            errors.append(str(e))
    try:
        return _pikpak_password_relogin(store, proxy, client=client)
    except PikPakAuthError as e:
        errors.append(str(e))

    detail = "；".join(errors) or "没有可用的 PikPak 登录态"
    _save_pikpak_state(store, login_required=True, last_error=detail)
    raise PikPakAuthError(detail)


def pikpak_keepalive(store, proxy: ProxyConfig | None = None) -> dict:
    """后台保活：快到期的续一次，续不上就用密码重登。给定时任务用。"""
    data = _pikpak_state(store)
    if not data.get("refresh_token") and not data.get("password"):
        return {"ok": False, "skipped": "未登录"}
    try:
        pikpak_access_token(store, proxy)
    except PikPakAuthError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}

def _pikpak_about(client: httpx.Client, device_id: str, access_token: str):
    return client.get(
        PIKPAK_ABOUT_URL,
        headers={
            **_headers(device_id),
            "Authorization": f"Bearer {access_token}",
        },
    )


def _pikpak_storage(store, proxy: ProxyConfig | None = None) -> dict:
    access_token = pikpak_access_token(store, proxy)
    device_id = pikpak_api.device_id(store)
    with _client(proxy) as client:
        response = _pikpak_about(client, device_id, access_token)
        if response.status_code in (401, 403):
            # 拿到手时还有效，用的时候被踢了：强制重拿一次再试
            access_token = pikpak_access_token(store, proxy, force=True)
            response = _pikpak_about(client, device_id, access_token)
    if response.status_code >= 400:
        raise PikPakAuthError(f"读取容量失败：{_response_detail(response)}")
    body = response.json()
    quota = body.get("quota") or {}
    total = int(quota.get("limit") or 0)
    used = int(quota.get("usage") or 0)
    return {
        "used": used,
        "total": total,
        "percent": round(used / total * 100, 1) if total else 0,
    }


def _refresh_storage(store, cache_key: str, lock_key: str, loader) -> None:
    """真正去网盘查一次容量并写缓存；失败保留旧数据，只记错误。"""
    with _STORAGE_LOCKS[lock_key]:
        try:
            data = loader()
        except Exception as e:
            cache = store.get_json(cache_key, {}) or {}
            cache.update({"at": time.time(), "error": str(e)})
            store.set_json(cache_key, cache)
            return
        store.set_json(cache_key, {"at": time.time(), "data": data, "error": ""})


def _storage_state(store, cache_key: str, lock_key: str, loader,
                   *, force: bool = False) -> dict:
    cache = store.get_json(cache_key, {}) or {}
    at = float(cache.get("at") or 0)
    stale = not cache.get("data") or (time.time() - at) >= STORAGE_TTL
    spawned = False
    if stale and force:
        # 登录 / 退出后要立刻反映真实容量，同步查一次
        _refresh_storage(store, cache_key, lock_key, loader)
        cache = store.get_json(cache_key, {}) or {}
    elif stale and _STORAGE_LOCKS[lock_key].acquire(blocking=False):
        _STORAGE_LOCKS[lock_key].release()
        spawned = True
        threading.Thread(
            target=_refresh_storage,
            args=(store, cache_key, lock_key, loader),
            daemon=True,
        ).start()
    return {
        "storage": cache.get("data"),
        "storage_error": str(cache.get("error") or ""),
        # spawned 兜住「线程刚起步、还没拿到锁」的窗口，页面才能安排一次补查
        "storage_refreshing": spawned or _STORAGE_LOCKS[lock_key].locked(),
    }


def _pikpak_status(store, proxy: ProxyConfig | None = None, with_storage: bool = False,
                   force_storage: bool = False) -> dict:
    data = _pikpak_state(store)
    # 只看 refresh_token 字符串在不在会骗人：token 早失效了页面还写「已登录」。
    # 有密码也算「能连上」，因为掉线后能自动重登。
    connected = bool(data.get("refresh_token") or data.get("password"))
    out = {
        "connected": connected,
        # 三个状态分清楚：能连 / 需要人工重新登录 / 未登录
        "needs_login": bool(data.get("login_required")),
        "auto_login": bool(data.get("password")),
        "last_error": str(data.get("last_error") or ""),
        "account": _mask_account(str(data.get("username") or "")),
        "updated_at": data.get("updated_at") or "",
        "expires_at": data.get("expires_at") or "",
        "user_id": data.get("user_id") or "",
        "storage": None,
    }
    if connected and with_storage:
        out.update(_storage_state(
            store, PIKPAK_STORAGE_CACHE, "pikpak",
            lambda: _pikpak_storage(store, proxy), force=force_storage,
        ))
    return out


def _guangya_status(auth: GuangyaAuth, with_storage: bool = False,
                    force_storage: bool = False) -> dict:
    out = auth.status()
    if out.get("connected") and with_storage:
        out.update(_storage_state(
            auth.store, GUANGYA_STORAGE_CACHE, "guangya",
            auth.storage, force=force_storage,
        ))
    return out


def _reset_stale_guangya_folder(store) -> None:
    """换账号或目录被删后，设置里可能还留着旧目录 id，登录后校验一次。"""
    auth = GuangyaAuth(store)
    folder_id = str(auth.status().get("root_folder_id") or "")
    if not folder_id:
        return
    try:
        ok, _ = auth.check_folder(folder_id)
    except Exception:
        return
    if not ok:
        auth.set_folder("", "/")


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    router = APIRouter()

    @router.get("/api/integrations/status")
    def status(storage: bool = True) -> dict:
        # storage=false 只读本地登录态（秒回），容量数据要连网盘、慢得多；
        # 秒传页只关心有没有登录，不该被容量查询拖住。
        return {
            "status": 200,
            "pikpak": _pikpak_status(store, ctx.system_proxy(), with_storage=storage),
            "guangya": _guangya_status(GuangyaAuth(store), with_storage=storage),
        }

    @router.post("/api/integrations/pikpak/login")
    def pikpak_login(payload: PikPakLoginIn) -> dict:
        username = (payload.username or "").strip()
        password = payload.password or ""
        if not username or not password:
            raise HTTPException(status_code=400, detail="请填 PikPak 账号和密码")

        try:
            body = pikpak_signin(
                store, ctx.system_proxy(), username=username, password=password,
            )
            _store_pikpak_login(
                store, username=username, password=password, body=body,
                remember=payload.remember,
            )
        except PikPakAuthError as e:
            raise HTTPException(status_code=400, detail=f"PikPak {e}") from e

        store.set_json(PIKPAK_STORAGE_CACHE, {})
        return {
            "status": 200,
            "pikpak": _pikpak_status(
                store, ctx.system_proxy(), with_storage=True, force_storage=True,
            ),
        }

    @router.post("/api/integrations/pikpak/logout")
    def pikpak_logout() -> dict:
        # 退出登录会把保存的密码一并清掉（否则「退出」等于退不掉）
        store.set_json(PIKPAK_STATE_KEY, {})
        store.set_json(PIKPAK_STORAGE_CACHE, {})
        return {"status": 200, "pikpak": _pikpak_status(store)}

    @router.post("/api/integrations/guangya/send-code")
    def guangya_send_code(payload: GuangyaCodeIn) -> dict:
        try:
            result = GuangyaAuth(store).send_code(payload.phone)
        except GuangyaLoginError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"status": 200, "guangya": _guangya_status(GuangyaAuth(store), with_storage=True)}

    @router.post("/api/integrations/guangya/login")
    def guangya_login(payload: GuangyaLoginIn) -> dict:
        try:
            result = GuangyaAuth(store).login(payload.code)
        except GuangyaLoginError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        store.set_json(GUANGYA_STORAGE_CACHE, {})
        _reset_stale_guangya_folder(store)
        return {
            "status": 200,
            "guangya": _guangya_status(
                GuangyaAuth(store), with_storage=True, force_storage=True,
            ),
        }

    @router.post("/api/integrations/guangya/token")
    def guangya_token(payload: GuangyaTokenIn) -> dict:
        try:
            result = GuangyaAuth(store).connect_token(payload.token)
        except GuangyaLoginError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        store.set_json(GUANGYA_STORAGE_CACHE, {})
        _reset_stale_guangya_folder(store)
        return {
            "status": 200,
            "guangya": _guangya_status(
                GuangyaAuth(store), with_storage=True, force_storage=True,
            ),
        }

    @router.post("/api/integrations/guangya/logout")
    def guangya_logout() -> dict:
        store.set_json(GUANGYA_STORAGE_CACHE, {})
        return {"status": 200, "guangya": GuangyaAuth(store).logout()}

    @router.get("/api/integrations/guangya/folders")
    def guangya_folders(parent_id: str = "") -> dict:
        try:
            result = GuangyaAuth(store).list_folders(parent_id)
        except GuangyaLoginError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"status": 200, **result}

    @router.post("/api/integrations/guangya/folder")
    def guangya_folder(payload: GuangyaFolderIn) -> dict:
        return {
            "status": 200,
            "guangya": GuangyaAuth(store).set_folder(payload.folder_id, payload.folder_path),
        }

    return router
