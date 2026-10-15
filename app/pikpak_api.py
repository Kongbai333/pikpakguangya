"""PikPak 分享详情接口：拿真实文件名 + 校验提取码。

**为什么需要它**：分享页的 SSR 只渲染「请输入提取码」那一屏，`title` 永远是空的；
真正的详情在 `api-drive.mypikpak.com`，它能直接返回 `title` / `files`，
而且能明确区分「码不对」（`PASS_CODE_ERROR`）和「需要码」（`PASS_CODE_EMPTY`）——
后者用 HTML 那条路永远判不出来。

**两个前提，缺一不可**：

1. 需要 `x-captcha-token`。captcha 的签名算法（client_id + 10 个盐值链式 MD5）
   写死在 PikPak 自己的前端 bundle 里（`pikcloud/D-lJmaqB.js` 的 `calculateCaptchaSign`），
   这里照抄那份实现。**PikPak 前端改版换了盐值就会失效**，所以任何一步失败都返回
   `ok=False`，调用方回退到 HTML 校验，绝不会把链接误判成失效。

2. 这个接口按 IP 地区限制。国内直连返回
   `PROHIBITED / 抱歉，分享功能在当前地区不可用`，**必须走代理**。
   实测同一个分享：直连 PROHIBITED，走 Clash 的 7897 就 OK。

新增状态码 `PASS_CODE_ERROR`（提取码错误），它不属于「失效」——码错了链接本身还在。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from typing import Any

import httpx

from app.proxy import ProxyConfig

log = logging.getLogger("pikpak.api")

# 以下常量全部照抄 PikPak 前端 bundle，别"顺手改一下"
CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CLIENT_VERSION = "undefined"          # 字面量字符串 "undefined"，不是 None
PACKAGE_NAME = "drive.mypikpak.com"
TIMESTAMP = "1791617062253"           # 打包时写死的固定值
SALTS = (
    "fyZ4+p77W1U4zcWBUwefAIFhFxvADWtT1wzolCxhg9q7etmGUjXr",
    "uSUX02HYJ1IkyLdhINEFcCf7l2",
    "iWt97bqD/qvjIaPXB2Ja5rsBWtQtBZZmaHH2rMR41",
    "3binT1s/5a1pu3fGsN",
    "8YCCU+AIr7pg+yd7CkQEY16lDMwi8Rh4WNp5",
    "DYS3StqnAEKdGddRP8CJrxUSFh",
    "crquW+4",
    "ryKqvW9B9hly+JAymXCIfag5Z",
    "Hr08T/NDTX1oSJfHk90c",
    "i",
)

SHARE_ACTION = "GET:/drive/v1/share"
CAPTCHA_INIT_URL = "https://user.mypikpak.com/v1/shield/captcha/init"
SHARE_URL = "https://api-drive.mypikpak.com/drive/v1/share"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# captcha token 有效期 300 秒，内存里缓存一下，省掉一半请求
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()
_DEVICE_ID = ""


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def captcha_sign(device_id: str) -> str:
    """captcha_sign = "1." + 用每个盐值依次 md5（前一次结果 + 盐）。"""
    acc = CLIENT_ID + CLIENT_VERSION + PACKAGE_NAME + device_id + TIMESTAMP
    for salt in SALTS:
        acc = _md5(acc + salt)
    return "1." + acc


def device_id(store=None) -> str:
    """设备号。存在库里，换一次进程也不变（PikPak 那边看着更像同一个客户端）。"""
    global _DEVICE_ID
    if store is not None:
        saved = store.get_state("pikpak.api.device_id", "") or ""
        if saved:
            _DEVICE_ID = saved
            return saved
    if not _DEVICE_ID:
        _DEVICE_ID = uuid.uuid4().hex
    if store is not None:
        store.set_state("pikpak.api.device_id", _DEVICE_ID)
    return _DEVICE_ID


def _headers(dev: str, token: str = "") -> dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://mypikpak.com",
        "Referer": "https://mypikpak.com/",
        "x-device-id": dev,
        "x-client-id": CLIENT_ID,
    }
    if token:
        h["x-captcha-token"] = token
    return h


def _client(proxy: ProxyConfig | None, timeout: float) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "trust_env": False,   # 别偷偷用系统代理，走哪个代理由调用方决定
        "timeout": timeout,
        "follow_redirects": True,
    }
    url = proxy.to_url() if proxy is not None and getattr(proxy, "enabled", False) else ""
    if url:
        kwargs["proxy"] = url
    return httpx.Client(**kwargs)


def _get_token(client: httpx.Client, dev: str, action: str = SHARE_ACTION) -> str:
    """按 action 换 captcha token。分享列表和分享目录是两个 action，token 不通用。"""
    # 并发扫目录时多个线程会一起进来，锁住避免重复去换同一个 token
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(action)
        if cached and cached[1] > time.time():
            return cached[0]
        resp = client.post(
            CAPTCHA_INIT_URL,
            headers=_headers(dev),
            json={
                "client_id": CLIENT_ID,
                "action": action,
                "device_id": dev,
                "captcha_token": "",
                "meta": {
                    "captcha_sign": captcha_sign(dev),
                    "client_version": CLIENT_VERSION,
                    "package_name": PACKAGE_NAME,
                    "user_id": "",
                    "timestamp": TIMESTAMP,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = str(data.get("captcha_token") or "")
        if not token:
            raise ValueError(f"captcha/init 没给 token：{str(data)[:200]}")
        expires = float(data.get("expires_in") or 300)
        _TOKEN_CACHE[action] = (token, time.time() + max(30.0, expires - 30))
        return token


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_share_detail(
    share_id: str,
    pass_code: str | None = None,
    proxy: ProxyConfig | None = None,
    timeout: float = 20.0,
    store=None,
) -> dict:
    """查一个分享的详情。

    返回 `{ok, status, title, file_num, expire_seconds, files, error}`。
    `ok=False` 表示这次没查成（网络/地区限制/改版），调用方应该回退 HTML 校验，
    不要拿 error 去判定链接失效。
    """
    sid = (share_id or "").strip()
    if not sid:
        return {"ok": False, "error": "share_id 为空"}

    dev = device_id(store)
    params: dict[str, Any] = {"share_id": sid, "limit": 100, "thumbnail_size": "SIZE_LARGE"}
    if pass_code:
        params["pass_code"] = pass_code.strip()

    try:
        with _client(proxy, timeout) as client:
            token = _get_token(client, dev)
            resp = client.get(SHARE_URL, params=params, headers=_headers(dev, token))
            if resp.status_code == 400 and "captcha" in resp.text:
                # token 过期/失效，清掉重来一次
                _TOKEN_CACHE.pop(SHARE_ACTION, None)
                token = _get_token(client, dev)
                resp = client.get(SHARE_URL, params=params, headers=_headers(dev, token))
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("PikPak 详情接口失败（回退 HTML 校验）：%s: %s", type(e).__name__, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    status = str(data.get("share_status") or "").strip()
    if status == "PROHIBITED":
        return {
            "ok": False,
            "error": "PikPak 说「分享功能在当前地区不可用」—— 这个接口必须走代理",
        }
    if not status:
        return {"ok": False, "error": f"返回里没有 share_status：{str(data)[:200]}"}

    files = data.get("files") or []
    names = [str(f.get("name") or "") for f in files if f.get("name")]
    title = str(data.get("title") or "").strip()
    if not title and names:
        title = " / ".join(names[:3]) + ("…" if len(names) > 3 else "")

    expire = _to_int(data.get("expiration_left_seconds"))
    if expire is not None and expire < 0:
        expire = None      # -1 = 永久有效，别当成「已过期」

    file_rows = [
        {
            "name": f.get("name"),
            "kind": f.get("kind"),
            "size": _to_int(f.get("size")),
        }
        for f in files[:100]
    ]
    file_num = _to_int(data.get("file_num"))
    sizes = [f["size"] for f in file_rows if f["size"] is not None]
    total_size = (
        sum(sizes)
        if sizes and (file_num is None or file_num <= len(file_rows))
        else None
    )

    return {
        "ok": True,
        "status": status,
        "status_text": str(data.get("share_status_text") or ""),
        "title": title,
        "file_num": file_num,
        "size_bytes": total_size,
        "expire_seconds": expire,
        "files": file_rows,
        "nickname": str((data.get("user_info") or {}).get("nickname") or ""),
        "error": "",
    }
