"""视频转发：创建目标群组、逐群开关、看统计。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import forwarder
from app.routers.context import WebContext


class ForwardGroupIn(BaseModel):
    account_id: int | None = None
    title: str = ""


class ForwardToggleIn(BaseModel):
    chat_id: int
    enabled: bool = True
    account_id: int | None = None


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    router = APIRouter()

    def account_of(account_id: int | None) -> dict | None:
        """这次操作针对哪个账号。不传就是「当前使用」那个。"""
        if not account_id:
            return ctx.tg.account()
        row = store.get_account(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个账号")
        return row

    def snapshot(account_id: int | None) -> dict:
        account = account_of(account_id) or {}
        aid = int(account.get("id") or 0)
        return {
            "status": 200,
            "account_id": aid,
            "target": forwarder.load_target(store, aid),
            "stats": forwarder.load_stats(store, aid),
            "chats": [
                {
                    "chat_id": w["chat_id"],
                    "title": w["title"],
                    "enabled": bool(w.get("enabled")),
                    "forward_enabled": bool(w.get("forward_enabled")),
                }
                for w in store.list_watched(account_id=aid or None)
            ],
        }

    @router.get("/api/forward")
    def api_forward(account_id: int | None = None) -> dict:
        return snapshot(account_id)

    @router.post("/api/forward/group")
    async def api_forward_create_group(body: ForwardGroupIn) -> dict:
        """用这个账号建一个只有自己的超级群，并记成转发目标。"""
        account = account_of(body.account_id)
        aid = int((account or {}).get("id") or 0)
        if not aid:
            raise HTTPException(status_code=400, detail="还没有可用的 Telegram 账号")
        title = (body.title or "").strip() or forwarder.DEFAULT_GROUP_TITLE
        created = await ctx.call(ctx.session_for(account).create_group(title))
        forwarder.save_target(
            store, aid,
            chat_id=created["chat_id"],
            raw_id=created["raw_id"],
            access_hash=created.get("access_hash"),
            title=created.get("title") or title,
            kind=created.get("kind") or "megagroup",
        )
        return snapshot(body.account_id)

    @router.delete("/api/forward/group")
    def api_forward_clear(account_id: int | None = None) -> dict:
        """只解除绑定：群本身留在 Telegram 里，要删去客户端删。"""
        account = account_of(account_id) or {}
        forwarder.clear_target(store, int(account.get("id") or 0))
        return snapshot(account_id)

    @router.post("/api/forward/toggle")
    def api_forward_toggle(body: ForwardToggleIn) -> dict:
        account = account_of(body.account_id) or {}
        updated = store.set_forward_watched(
            body.chat_id, body.enabled, account_id=int(account.get("id") or 0) or None,
        )
        if not updated:
            # 没在监控列表里的群不该凭空多出一个转发开关
            raise HTTPException(status_code=404, detail="这个群还没加进监控列表")
        return {"status": 200, "updated": True, "enabled": bool(body.enabled)}

    return router
