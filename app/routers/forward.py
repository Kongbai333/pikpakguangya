"""视频转发：创建/绑定目标群组、逐群开关、bot 中转、看统计。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import bot_api, forwarder
from app.routers.context import WebContext
from app.telegram_bot import resolve_bot_settings


class ForwardGroupIn(BaseModel):
    account_id: int | None = None
    title: str = ""


class ForwardToggleIn(BaseModel):
    chat_id: int
    enabled: bool = True
    account_id: int | None = None


class ForwardTargetIn(BaseModel):
    chat_id: int
    account_id: int | None = None
    # direct = 采集号直接发到群；bot = 采集号发给 bot，由 bot 发到群
    mode: str = "direct"


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
        bot = resolve_bot_settings(store)
        return {
            "status": 200,
            "account_id": aid,
            "target": forwarder.load_target(store, aid),
            "stats": forwarder.load_stats(store, aid),
            # bot 侧真正往群复制的那一步，和采集号转发分开统计
            "relay": {
                "chat": forwarder.load_relay_target(store),
                "stats": forwarder.load_relay_stats(store),
                "token_set": bool(bot.get("token")),
            },
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
            mode="direct",
        )
        forwarder.clear_relay_target(store)
        return snapshot(body.account_id)

    @router.get("/api/forward/candidates")
    async def api_forward_candidates(account_id: int | None = None) -> dict:
        """能当转发目标的已有群组（账号被限制建群时的出路）。"""
        account = account_of(account_id)
        chats = await ctx.call(ctx.session_for(account).list_forward_candidates())
        return {"status": 200, "chats": chats}

    @router.post("/api/forward/target")
    async def api_forward_bind_target(body: ForwardTargetIn) -> dict:
        """把已有的群/频道绑成转发目标。

        mode="bot" 时会先用 Bot API 确认 bot 真的能进这个群：
        bot 不在群里的话，转发过去才知道失败就太晚了。
        """
        account = account_of(body.account_id) or {}
        aid = int(account.get("id") or 0)
        if not aid:
            raise HTTPException(status_code=400, detail="还没有可用的 Telegram 账号")
        mode = "bot" if str(body.mode or "").lower() == "bot" else "direct"

        if mode == "bot":
            bot = resolve_bot_settings(store)
            token = str(bot.get("token") or "")
            if not token:
                raise HTTPException(
                    status_code=400,
                    detail="还没配置 Bot Token（设置 → Telegram Bot），bot 中转用不了",
                )
            try:
                me = await bot_api.get_me(token, ctx.system_proxy())
                chat = await bot_api.get_chat(token, int(body.chat_id), ctx.system_proxy())
            except bot_api.BotApiError as e:
                raise HTTPException(
                    status_code=400, detail=f"bot 进不了这个群：{e}"
                ) from e
            title = str(chat.get("title") or "").strip()
            forwarder.save_target(
                store, aid,
                chat_id=int(chat.get("id") or body.chat_id),
                raw_id=0,
                access_hash=0,
                title=title,
                kind="bot",
                mode="bot",
                bot_username=str(me.get("username") or ""),
                bot_user_id=int(me.get("id") or 0),
            )
            forwarder.save_relay_target(
                store, chat_id=int(chat.get("id") or body.chat_id), title=title,
            )
            return snapshot(body.account_id)

        # 直连：从 Telethon 那边取 raw_id / access_hash
        peer = await ctx.call(
            ctx.session_for(account).forward_target_peer(body.chat_id)
        )
        forwarder.save_target(
            store, aid,
            chat_id=peer["chat_id"],
            raw_id=peer["raw_id"],
            access_hash=peer.get("access_hash"),
            title=peer.get("title") or "",
            kind=peer.get("kind") or "channel",
            mode="direct",
        )
        forwarder.clear_relay_target(store)
        return snapshot(body.account_id)

    @router.post("/api/forward/test")
    async def api_forward_test(account_id: int | None = None) -> dict:
        """让 bot 往目标群发一条测试消息，确认权限真的够。"""
        account = account_of(account_id) or {}
        target = forwarder.load_target(store, int(account.get("id") or 0))
        if str(target.get("mode") or "direct") != "bot":
            raise HTTPException(
                status_code=400, detail="只有 bot 中转模式需要这条测试",
            )
        bot = resolve_bot_settings(store)
        token = str(bot.get("token") or "")
        try:
            await bot_api.send_text(
                token, int(target["chat_id"]),
                "PikPak 链接库：视频转发通道测试 ✅", ctx.system_proxy(),
            )
        except bot_api.BotApiError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"status": 200, "ok": True}

    @router.delete("/api/forward/group")
    def api_forward_clear(account_id: int | None = None) -> dict:
        """只解除绑定：群本身留在 Telegram 里，要删去客户端删。"""
        account = account_of(account_id) or {}
        forwarder.clear_target(store, int(account.get("id") or 0))
        forwarder.clear_relay_target(store)
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
