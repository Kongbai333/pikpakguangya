"""账号与登录：应用凭据、当前登录态、多账号管理。"""

from __future__ import annotations

import contextlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.context import WebContext, iso_now
from app.tg import TelegramSession

log = logging.getLogger("api")


class PhoneIn(BaseModel):
    phone: str = ""
    # 添加账号时选的绑定代理（0 / 不传 = 直连）
    proxy_id: int = 0


class CredentialsIn(BaseModel):
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""


class CodeIn(BaseModel):
    code: str = ""


class PasswordIn(BaseModel):
    password: str = ""


class CollectToggleIn(BaseModel):
    enabled: bool = True


class AccountProxyIn(BaseModel):
    """账号绑定的直接代理。"""
    proxy_id: int = 0


class AccountProfileIn(BaseModel):
    """账号本地资料：备注名和手机号，不会改 Telegram 的登录态。"""
    alias: str | None = None
    phone: str | None = None


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    cfg = ctx.cfg
    tg = ctx.tg
    app = ctx.app
    router = APIRouter()

    def account_session(session_name: str):
        """账号的 session 文件在哪。表里存文件名，目录跟 config.toml 那个同级。"""
        return tg.account_session_path(session_name)

    def discard_session_files(session_name: str) -> int:
        """删账号的 session 文件（含 -journal / -wal / -shm），返回删掉几个。"""
        base = account_session(session_name)
        if base is None:
            return 0
        n = 0
        for suffix in ("", "-journal", "-wal", "-shm"):
            f = Path(f"{base}{suffix}")
            try:
                if f.exists():
                    f.unlink()
                    n += 1
            except OSError as e:
                log.warning("删不掉 session 文件 %s：%s", f, e)
        return n

    def save_me(account_id: int, me: dict, keep_phone: str = "") -> None:
        """把 get_me() 的结果写回账号表。"""
        store.update_account(
            account_id,
            user_id=me.get("id"),
            first_name=me.get("first_name") or "",
            last_name=me.get("last_name") or "",
            username=me.get("username") or "",
            phone=keep_phone or me.get("phone") or "",
            last_login_at=iso_now(),
        )

    def pending(account_id: int):
        """取「正在添加」的那个账号的临时会话（见 api_account_add）。"""
        row = getattr(app.state, "pending_login", None)
        if not row or row.get("account_id") != account_id:
            raise HTTPException(
                status_code=400,
                detail="这个账号的登录流程已经作废了（服务重启过，或者又点了别的按钮），"
                       "关掉弹窗重新点「添加账号」",
            )
        return row["session"]

    async def finish_login(account_id: int, session, result: dict) -> dict:
        me = result.get("me") or {}
        keep = (store.get_account(account_id) or {}).get("phone") or ""
        save_me(account_id, me, keep_phone=keep)
        app.state.pending_login = None
        await session.disconnect()
        log.info("账号 %s 登录成功：%s", account_id,
                 me.get("username") or me.get("name") or me.get("id"))
        return {
            "ok": True,
            "me": me,
            "active_id": (tg.account() or {}).get("id"),
            "accounts": ctx.accounts_payload(),
        }

    @router.get("/api/auth/status")
    async def auth_status() -> dict:
        """当前账号的状态。顺手把 get_me() 的结果写回账号表 ——

        老库升级上来的那一条只有 session 文件名，用户名/手机号是空的，
        靠这次回写自动补上，不用用户手动点「刷新」。
        """
        info = await ctx.session_for(tg.account()).status()
        account = info.get("account") or {}
        me = info.get("me") or {}
        if account.get("id") and me.get("id") and (
            account.get("user_id") != me.get("id")
            or (account.get("username") or "") != (me.get("username") or "")
            or (account.get("first_name") or "") != (me.get("first_name") or "")
        ):
            save_me(account["id"], me, keep_phone=account.get("phone") or "")
            info["account"] = store.get_account(account["id"])
        return {"status": 200, **info}

    @router.post("/api/auth/logout")
    async def auth_logout() -> dict:
        """退出当前账号的登录态（Telethon 会顺手删掉 session 文件）。

        账号那一行留在表里、手机号也留着 —— 这样「添加账号」再填同一个手机号
        就是重新登录，不用再多出一条记录。
        """
        account = tg.account() or {}
        if account.get("id"):
            # 先停采集再用共享 session 退出登录，避免退出过程中采集还在读这个连接
            await ctx.collector.stop_account(account["id"])
        session = ctx.session_for(account)
        result = await ctx.call(session.logout())
        if account.get("id"):
            store.update_account(account["id"], collect_enabled=0)
            store.clear_account_login(account["id"])
        return {"status": 200, **(result or {}), "accounts": ctx.accounts_payload()}

    @router.post("/api/auth/credentials")
    async def auth_credentials_save(payload: CredentialsIn) -> dict:
        cred = await ctx.call(tg.save_credentials(payload.api_id, payload.api_hash, payload.phone))
        return {
            "status": 200,
            "api_id": cred["api_id"],
            "api_hash_set": bool(cred["api_hash"]),
            "phone": cred["phone"],
            "source": cred["source"],
        }

    @router.get("/api/accounts")
    def api_accounts() -> dict:
        return {
            "status": 200,
            "accounts": ctx.accounts_payload(),
            "active_id": (tg.account() or {}).get("id"),
        }

    @router.post("/api/accounts")
    async def api_account_add(payload: PhoneIn) -> dict:
        """「添加账号」第一步：建一条账号记录（用自己的 session 文件）+ 发验证码。

        故意不动当前正在用的账号 —— 新账号拿一个全新的 session 文件，
        所以添加过程中管理页、采集都还能照常用旧账号。
        """
        phone = (payload.phone or "").strip()
        if not phone:
            raise HTTPException(status_code=400, detail="请填手机号，要带国家码（例如 +8613800000000）")
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("+")
        existing = next(
            (x for x in store.list_accounts() if x["phone"] and x["phone"] == phone), None)
        if existing and existing.get("user_id"):
            raise HTTPException(status_code=400, detail=f"{phone} 已经在账号列表里了")
        if existing:
            # 之前退过登录、或者登录态失效了：复用这一行，别再多出一条
            row = existing
            discard_session_files(row["session"])
        else:
            row = store.add_account(
                phone=phone, session=f"acct-{uuid.uuid4().hex[:12]}.session",
                proxy_id=payload.proxy_id,
            )
        if existing and payload.proxy_id:
            store.update_account(row["id"], proxy_id=payload.proxy_id)
            row = store.get_account(row["id"]) or row
        session = TelegramSession(cfg, store, account=row)
        try:
            result = await ctx.call(session.send_code(phone))
        except Exception:
            # 发码失败就把刚建的那行收回去，别在列表里留一条永远登不上的记录
            with contextlib.suppress(Exception):
                await session.disconnect()
            if not existing:
                store.delete_account(row["id"])
                discard_session_files(row["session"])
            raise
        # 之前没走完的添加流程作废（一次只允许一个）
        old = getattr(app.state, "pending_login", None)
        if old:
            with contextlib.suppress(Exception):
                await old["session"].disconnect()
        app.state.pending_login = {"account_id": row["id"], "session": session}
        return {"status": 200, "account_id": row["id"], "phone": phone, **result}

    @router.post("/api/accounts/{account_id}/code")
    async def api_account_code(account_id: int, payload: CodeIn) -> dict:
        session = pending(account_id)
        result = await ctx.call(session.submit_code(payload.code))
        if result.get("need_password"):
            return {"status": 200, "account_id": account_id, **result}
        return {
            "status": 200, "account_id": account_id,
            **await finish_login(account_id, session, result),
        }

    @router.post("/api/accounts/{account_id}/password")
    async def api_account_password(account_id: int, payload: PasswordIn) -> dict:
        session = pending(account_id)
        result = await ctx.call(session.submit_password(payload.password))
        return {
            "status": 200, "account_id": account_id,
            **await finish_login(account_id, session, result),
        }

    @router.post("/api/accounts/{account_id}/activate")
    async def api_account_activate(account_id: int) -> dict:
        """设为「当前使用」。群列表、校验出口跟着它走；采集按账号独立开关。"""
        if not store.get_account(account_id):
            raise HTTPException(status_code=404, detail="没有这个账号")
        row = getattr(app.state, "pending_login", None)
        if row:
            with contextlib.suppress(Exception):
                await row["session"].disconnect()
            app.state.pending_login = None
        store.set_active_account(account_id)
        # 应用层的共享会话跟着切；各账号自己的采集 session 不受影响
        await tg.use_account(store.get_account(account_id))
        return {"status": 200, "active_id": account_id, "accounts": ctx.accounts_payload()}

    @router.post("/api/accounts/{account_id}/collect")
    async def api_account_collect(account_id: int, payload: CollectToggleIn) -> dict:
        """账号采集开关：打开立即开始，关闭立即停止，开关状态持久化。"""
        if not store.get_account(account_id):
            raise HTTPException(status_code=404, detail="没有这个账号")
        try:
            result = await ctx.set_collect(account_id, payload.enabled)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"status": 200, **result}

    @router.put("/api/accounts/{account_id}/proxy")
    async def api_account_proxy(account_id: int, payload: AccountProxyIn) -> dict:
        """改账号绑定的直接代理。"""
        row = store.get_account(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个账号")
        proxy_id = int(payload.proxy_id or 0)
        if proxy_id and not store.get_proxy(proxy_id):
            raise HTTPException(status_code=404, detail="没有这个代理")
        store.update_account(account_id, proxy_id=proxy_id)
        if row["id"] == (tg.account() or {}).get("id"):
            await tg.use_account(store.get_account(account_id))
        # 代理变了，正在跑的采集要重连才会走新出口
        await ctx.collector.restart_account(account_id)
        return {"status": 200, "accounts": ctx.accounts_payload()}

    @router.put("/api/accounts/{account_id}/profile")
    def api_account_profile(account_id: int, payload: AccountProfileIn) -> dict:
        """改账号的本地资料。备注名只影响列表显示，不改 Telegram 资料。"""
        if not store.get_account(account_id):
            raise HTTPException(status_code=404, detail="没有这个账号")
        fields = {}
        if payload.alias is not None:
            fields["alias"] = payload.alias.strip()
        if payload.phone is not None:
            phone = payload.phone.strip()
            if phone and not phone.startswith("+"):
                phone = "+" + phone.lstrip("+")
            fields["phone"] = phone
        if not fields:
            raise HTTPException(status_code=400, detail="没有可保存的修改")
        store.update_account(account_id, **fields)
        return {"status": 200, "accounts": ctx.accounts_payload()}

    @router.post("/api/accounts/{account_id}/sync")
    async def api_account_sync(account_id: int) -> dict:
        """连一下这个账号，把用户名/手机号补进表里。"""
        row = store.get_account(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个账号")
        info = await ctx.session_for(row).status()
        if not info.get("authorized"):
            raise HTTPException(
                status_code=400,
                detail=info.get("error") or "这个账号还没登录（或登录态已失效），删掉重新添加",
            )
        me = info.get("me") or {}
        save_me(account_id, me, keep_phone=row.get("phone") or "")
        return {
            "status": 200,
            "active_id": (tg.account() or {}).get("id"),
            "accounts": ctx.accounts_payload(),
        }

    @router.delete("/api/accounts/{account_id}")
    async def api_account_delete(account_id: int) -> dict:
        """删账号：连同它的 session 文件一起删。"""
        row = store.get_account(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个账号")
        with contextlib.suppress(Exception):
            await ctx.collector.stop_account(account_id)
        ctx.collector.forget_account(account_id)
        removed = discard_session_files(row["session"])
        store.delete_account(account_id)
        await tg.use_account(store.active_account())
        return {
            "status": 200,
            "deleted": True,
            "removed_files": removed,
            "active_id": (tg.account() or {}).get("id"),
            "accounts": ctx.accounts_payload(),
        }

    return router
