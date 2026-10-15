"""管理后台登录：单管理员密码 + 签名 HttpOnly Cookie。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import AdminConfig

COOKIE_NAME = "pikpak_admin"
PUBLIC_API_PATHS = {
    "/api/items",
    "/api/stats",
    "/api/health",
    "/api/web-auth/login",
    "/api/web-auth/session",
    "/api/web-auth/logout",
}

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300
BLOCK_SECONDS = 300


def is_public_api(path: str) -> bool:
    return path in PUBLIC_API_PATHS


class LoginIn(BaseModel):
    password: str = ""


class AdminAuth:
    """配置只保存一个管理员密码，Cookie 使用该密码做 HMAC 签名。"""

    def __init__(self, cfg: AdminConfig):
        self.password = (cfg.password or "").strip()
        self.ttl_seconds = max(1, int(cfg.session_hours or 24)) * 3600
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.password)

    def _signature(self, expires: int) -> str:
        message = f"pikpak-admin:{expires}".encode()
        return hmac.new(self.password.encode(), message, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + self.ttl_seconds
        return f"{expires}.{self._signature(expires)}"

    def verify(self, token: str | None) -> bool:
        if not self.configured or not token or "." not in token:
            return False
        raw_expires, signature = token.split(".", 1)
        try:
            expires = int(raw_expires)
        except ValueError:
            return False
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(signature, self._signature(expires))

    def check_password(self, password: str) -> bool:
        return self.configured and secrets.compare_digest(
            self.password.encode(),
            (password or "").encode(),
        )

    def set_cookie(self, response: Response, token: str, *, secure: bool) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=self.ttl_seconds,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )

    @staticmethod
    def clear_cookie(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        until = self._blocked_until.get(key, 0)
        if until <= now:
            self._blocked_until.pop(key, None)
            return False
        return True

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        attempts = [
            ts for ts in self._failures.get(key, [])
            if now - ts <= FAILURE_WINDOW_SECONDS
        ]
        attempts.append(now)
        self._failures[key] = attempts
        if len(attempts) >= MAX_FAILURES:
            self._blocked_until[key] = now + BLOCK_SECONDS
            self._failures.pop(key, None)

    def clear_failures(self, key: str) -> None:
        self._failures.pop(key, None)
        self._blocked_until.pop(key, None)


def build_router(auth: AdminAuth) -> APIRouter:
    router = APIRouter()

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @router.post("/api/web-auth/login")
    def login(payload: LoginIn, request: Request, response: Response) -> dict:
        if not auth.configured:
            raise HTTPException(
                status_code=503,
                detail="未配置管理员密码，请在 config.toml 里设置 [admin] password",
            )
        key = client_key(request)
        if auth.is_blocked(key):
            raise HTTPException(status_code=429, detail="密码错误次数过多，请稍后再试")
        if not auth.check_password(payload.password):
            auth.record_failure(key)
            raise HTTPException(status_code=401, detail="管理员密码错误")
        auth.clear_failures(key)
        auth.set_cookie(
            response,
            auth.issue(),
            secure=request.url.scheme == "https",
        )
        return {"status": 200, "authenticated": True}

    @router.get("/api/web-auth/session")
    def session(request: Request) -> dict:
        return {
            "status": 200,
            "authenticated": auth.verify(request.cookies.get(COOKIE_NAME)),
            "configured": auth.configured,
        }

    @router.post("/api/web-auth/logout")
    def logout(response: Response) -> dict:
        auth.clear_cookie(response)
        return {"status": 200, "authenticated": False}

    return router
