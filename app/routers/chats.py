"""群组与监听：群列表、加群、监听勾选、监控室。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.context import WebContext, iso_now
from app.store import chat_id_variants

CHATS_CACHE_KEY = "chats.cache"


class TargetIn(BaseModel):
    target: str = ""
    # 用哪个账号加群；不传 = 「当前使用」那个
    account_id: int | None = None


class WatchToggleIn(BaseModel):
    chat_id: int
    enabled: bool = True
    account_id: int | None = None
    title: str = ""
    username: str = ""
    kind: str = ""


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    tg = ctx.tg
    router = APIRouter()

    def watched_account(account_id: int | None) -> dict | None:
        """这次操作针对哪个账号。不传就是「当前使用」那个。"""
        if not account_id:
            return tg.account()
        row = store.get_account(account_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个账号")
        return row

    async def dialogs_of(account: dict | None) -> list[dict]:
        """实时拉这个账号的全部群 / 频道。

        走账号唯一的 TelegramSession：采集在跑时和它共用同一个 client，
        不会出现两个连接同时占一个 session 文件。
        """
        return await ctx.call(ctx.session_for(account).list_dialogs())

    @router.get("/api/chats")
    async def api_chats(account_id: int | None = None) -> dict:
        """某个账号的群列表（实时拉，连不上时回退上次的缓存）。"""
        account = watched_account(account_id)
        aid = (account or {}).get("id") or 0
        watched = store.list_watched(account_id=aid)
        by_id = {w["chat_id"]: w for w in watched}

        def mark(chats: list[dict]) -> list[dict]:
            out = []
            for c in chats:
                d = dict(c)
                w = by_id.get(d.get("chat_id"))
                d["watched"] = bool(w and w["enabled"])
                d["media_enabled"] = bool(w and w.get("media_enabled"))
                out.append(d)
            return out

        cache_key = f"{CHATS_CACHE_KEY}:{aid}"
        cached = store.get_json(cache_key) or {}
        cached_chats = cached.get("chats") or []
        cached_at = cached.get("at", "")

        def payload(chats: list[dict], is_cached: bool, reason: str = "") -> dict:
            return {
                "status": 200,
                "account": account,
                "chats": mark(chats),
                "watched": store.list_watched(only_enabled=True, account_id=aid),
                "cached": is_cached,
                "cached_at": cached_at if is_cached else "",
                "reason": reason,
            }

        try:
            dialogs = await dialogs_of(account)
        except HTTPException as e:
            if cached_chats:
                return payload(
                    cached_chats, True, f"连不上 Telegram（{e.detail}），显示上次拉到的列表"
                )
            raise

        store.set_json(cache_key, {"at": iso_now(), "chats": dialogs})
        return payload(dialogs, False)

    @router.post("/api/chats/join")
    async def api_chats_join(payload: TargetIn) -> dict:
        """加群。用指定账号加（在哪个账号的群组弹窗里点的，就用哪个账号）。"""
        account = watched_account(payload.account_id)
        result = await ctx.call(ctx.session_for(account).join(payload.target))
        if result.get("joined") and result.get("chat_id"):
            # 加入成功后顺手勾上监听，省一步操作
            store.add_watched(
                result["chat_id"], result.get("title", ""), result.get("username", ""), "群",
                True, account_id=(account or {}).get("id"),
            )
        return {"status": 200, **result}

    @router.get("/api/watch")
    def api_watch(account_id: int | None = None) -> dict:
        return {"status": 200, "watched": store.list_watched(account_id=account_id)}

    @router.post("/api/watch/toggle")
    def api_watch_toggle(payload: WatchToggleIn) -> dict:
        if payload.enabled:
            store.add_watched(
                payload.chat_id,
                title=payload.title,
                username=payload.username,
                kind=payload.kind,
                enabled=True,
                account_id=payload.account_id,
            )
            updated = True
        else:
            updated = store.set_watched(
                payload.chat_id, False, account_id=payload.account_id
            )
        return {
            "status": 200,
            "updated": updated,
            "enabled": bool(payload.enabled),
        }

    @router.post("/api/watch/media")
    def api_watch_media(payload: WatchToggleIn) -> dict:
        store.set_media_watched(
            payload.chat_id,
            payload.enabled,
            title=payload.title,
            username=payload.username,
            kind=payload.kind,
            account_id=payload.account_id,
        )
        return {"status": 200, "media_enabled": bool(payload.enabled)}

    @router.get("/api/monitor")
    def api_monitor() -> dict:
        """**不连 Telegram** —— 页面会定期刷，只读数据库 + 采集状态。

        只列**正在监控**的群（enabled=1）：停用过的历史记录不在这里显示，
        账号页的「群组」弹窗里仍然能看到它们（勾掉的状态）。
        """
        stats = store.chat_stats()

        def stats_of(chat_id: int) -> dict:
            """超级群的 id 有 `-100` 前缀和不带两种写法，两边都得试。

            群列表（dialog）给的是带前缀的，messages 表里存的是 Telethon 的
            entity.id（不带前缀），不兼容的话每个群都会显示「0 链接」。
            """
            for cid in chat_id_variants(chat_id):
                hit = stats.get(str(cid))
                if hit:
                    return hit
            return {}

        by_acc: dict[int, list[dict]] = {}
        for w in store.list_watched_all():
            if w["enabled"] or w["media_enabled"]:
                by_acc.setdefault(w["account_id"], []).append(w)

        active_id = (tg.account() or {}).get("id")
        # 采集按账号独立，这里只表示「有没有账号在采」；每个账号的状态在 a["collecting"] 里
        collecting = ctx.collector.running
        out = []
        for a in ctx.accounts_payload():
            rows = []
            for w in by_acc.get(a["id"], []):
                st = stats_of(w["chat_id"])
                rows.append({
                    **w,
                    "links": int(st.get("links") or 0),
                    "messages": int(st.get("messages") or 0),
                    "last_at": st.get("last_at") or "",
                })
            rows.sort(key=lambda r: (-r["links"], r["title"]))
            out.append({
                **a,
                "watched": rows,
                "links": sum(r["links"] for r in rows),
                "is_active": a["id"] == active_id,
            })
        return {"status": 200, "accounts": out, "active_id": active_id, "collecting": collecting}

    return router
