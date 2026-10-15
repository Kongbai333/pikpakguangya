"""第三方网盘账号接入：PikPak 登录 + 光鸭 Token。"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import pikpak_api
from app.guangya_login import GuangyaAuth, GuangyaLoginError
from app.proxy import ProxyConfig
from app.routers.context import WebContext
from app.store import iso, utcnow

PIKPAK_STATE_KEY = "integrations.pikpak"

PIKPAK_CLIENT_ID = "YNxT9w7GMdWvEOKa"
PIKPAK_CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"
PIKPAK_CAPTCHA_URL = "https://user.mypikpak.com/v1/shield/captcha/init"
PIKPAK_LOGIN_URL = "https://user.mypikpak.com/v1/auth/signin"
PIKPAK_TOKEN_URL = "https://user.mypikpak.com/v1/auth/token"
PIKPAK_ABOUT_URL = "https://api-drive.mypikpak.com/drive/v1/about"

# 容量缓存：进设置页先给缓存值，过期只在后台线程里刷新，接口不等网盘。
STORAGE_TTL = 120.0
PIKPAK_STORAGE_CACHE = "integrations.cache.pikpak_storage"
GUANGYA_STORAGE_CACHE = "integrations.cache.guangya_storage"
_STORAGE_LOCKS = {"pikpak": threading.Lock(), "guangya": threading.Lock()}


class PikPakLoginIn(BaseModel):
    username: str = ""
    password: str = ""


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


def _pikpak_refresh_token(store, proxy: ProxyConfig | None = None) -> str:
    data = store.get_json(PIKPAK_STATE_KEY, {}) or {}
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token:
        raise ValueError("PikPak 登录态已失效，请重新登录")
    device_id = pikpak_api.device_id(store)
    with _client(proxy) as client:
        response = client.post(
            PIKPAK_TOKEN_URL,
            headers=_headers(device_id),
            json={
                "client_id": PIKPAK_CLIENT_ID,
                "client_secret": PIKPAK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        body = response.json()
    access_token = str(body.get("access_token") or "")
    if not access_token:
        raise ValueError("PikPak 刷新登录态失败")
    data.update({
        "access_token": access_token,
        "refresh_token": str(body.get("refresh_token") or refresh_token),
        "updated_at": iso(utcnow()),
    })
    store.set_json(PIKPAK_STATE_KEY, data)
    return access_token


def _pikpak_storage(store, proxy: ProxyConfig | None = None) -> dict:
    data = store.get_json(PIKPAK_STATE_KEY, {}) or {}
    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise ValueError("请先登录 PikPak")
    device_id = pikpak_api.device_id(store)
    with _client(proxy) as client:
        response = client.get(
            PIKPAK_ABOUT_URL,
            headers={
                **_headers(device_id),
                "Authorization": f"Bearer {access_token}",
            },
        )
        if response.status_code in (401, 403):
            access_token = _pikpak_refresh_token(store, proxy)
            response = client.get(
                PIKPAK_ABOUT_URL,
                headers={
                    **_headers(device_id),
                    "Authorization": f"Bearer {access_token}",
                },
            )
        response.raise_for_status()
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
    data = store.get_json(PIKPAK_STATE_KEY, {}) or {}
    connected = bool(data.get("refresh_token"))
    out = {
        "connected": connected,
        "account": _mask_account(str(data.get("username") or "")),
        "updated_at": data.get("updated_at") or "",
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

        device_id = pikpak_api.device_id(store)
        meta = _identity_meta(username)
        try:
            with _client(ctx.system_proxy()) as client:
                captcha = client.post(
                    PIKPAK_CAPTCHA_URL,
                    headers=_headers(device_id),
                    json={
                        "client_id": PIKPAK_CLIENT_ID,
                        "action": f"POST:{PIKPAK_LOGIN_URL}",
                        "device_id": device_id,
                        "meta": meta,
                    },
                )
                captcha.raise_for_status()
                captcha_token = str(captcha.json().get("captcha_token") or "")
                if not captcha_token:
                    raise ValueError("PikPak 没有返回 captcha_token")

                response = client.post(
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
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as e:
            text = (e.response.text or "")[:300]
            raise HTTPException(status_code=400, detail=f"PikPak 登录失败：{text}") from e
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"PikPak 登录失败：{e}") from e

        if body.get("error"):
            raise HTTPException(
                status_code=400,
                detail=str(body.get("error_description") or body.get("error")),
            )
        access_token = str(body.get("access_token") or "")
        refresh_token = str(body.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise HTTPException(status_code=400, detail="PikPak 登录返回缺少 token")

        store.set_json(
            PIKPAK_STATE_KEY,
            {
                "username": username,
                "user_id": str(body.get("sub") or ""),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "updated_at": iso(utcnow()),
            },
        )
        store.set_json(PIKPAK_STORAGE_CACHE, {})
        return {
            "status": 200,
            "pikpak": _pikpak_status(
                store, ctx.system_proxy(), with_storage=True, force_storage=True,
            ),
        }

    @router.post("/api/integrations/pikpak/logout")
    def pikpak_logout() -> dict:
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
