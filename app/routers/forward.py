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
    # 是否在视频下面跟一条「来源 + 原消息链接」
    link_source: bool = True


class ForwardLinkSourceIn(BaseModel):
    account_id: int | None = None
    enabled: bool = True


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
            # 先用 Telethon 拿到 peer：普通群在这里要做一次 chat_id 换算
            peer = await ctx.call(
                ctx.session_for(account).forward_target_peer(body.chat_id)
            )
            bot_chat = forwarder.bot_chat_id(peer)
            try:
                me = await bot_api.get_me(token, ctx.system_proxy())
                chat = await bot_api.get_chat(token, bot_chat, ctx.system_proxy())
            except bot_api.BotApiError as e:
                # getChat 能过说明 id 和成员身份都对；过不了当场报，别等转发才发现
                raise HTTPException(
                    status_code=400, detail=f"bot 进不了这个群：{e}"
                ) from e
            title = str(chat.get("title") or "").strip() or str(peer.get("title") or "")
            forwarder.save_target(
                store, aid,
                chat_id=bot_chat,
                raw_id=peer["raw_id"],
                access_hash=peer.get("access_hash"),
                title=title,
                kind=peer.get("kind") or "channel",
                mode="bot",
                bot_username=str(me.get("username") or ""),
                bot_user_id=int(me.get("id") or 0),
                link_source=body.link_source,
            )
            forwarder.save_relay_target(
                store, chat_id=bot_chat, title=title,
                link_source=body.link_source,
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
            link_source=body.link_source,
        )
        forwarder.clear_relay_target(store)
        return snapshot(body.account_id)

    @router.post("/api/forward/invite-bot")
    async def api_forward_invite_bot(body: ForwardTargetIn) -> dict:
        """让这个账号把 bot 拉进目标群，并用 Bot API 验证结果。

        bot 自己加不了群（Bot API 没这个接口），所以只能借账号的手。
        """
        account = account_of(body.account_id) or {}
        aid = int(account.get("id") or 0)
        if not aid:
            raise HTTPException(status_code=400, detail="还没有可用的 Telegram 账号")
        bot = resolve_bot_settings(store)
        token = str(bot.get("token") or "")
        if not token:
            raise HTTPException(
                status_code=400, detail="还没配置 Bot Token（设置 → Telegram Bot）",
            )
        try:
            me = await bot_api.get_me(token, ctx.system_proxy())
        except bot_api.BotApiError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        username = str(me.get("username") or "")

        result = await ctx.call(
            ctx.session_for(account).invite_bot(int(body.chat_id), username)
        )

        # 拉进去之后再问一次 Telegram：真的在里面才算成功
        verified = False
        try:
            await bot_api.get_chat(
                token, int(body.chat_id), ctx.system_proxy(),
            )
        except bot_api.BotApiError as e:
            return {
                "status": 200,
                "invited": bool(result.get("invited")),
                "verified": False,
                "detail": f"邀请求发出了，但 bot 现在还看不到这个群：{e}",
                **result,
            }
        verified = True

        detail = "bot 已经在群里"
        if result.get("broadcast"):
            detail += "；频道还得把它设成管理员（要有发消息权限），否则发不出去"
        return {
            "status": 200,
            "invited": bool(result.get("invited")),
            "already": bool(result.get("already")),
            "verified": verified,
            "detail": detail,
            **result,
        }

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

    @router.get("/api/forward/diagnose")
    async def api_forward_diagnose(account_id: int | None = None) -> dict:
        """逐项查「日志说转了、群里却没有」到底卡在哪一步。"""
        account = account_of(account_id) or {}
        aid = int(account.get("id") or 0)
        target = forwarder.load_target(store, aid)
        relay = forwarder.load_relay_target(store)
        bot = resolve_bot_settings(store)
        token = str(bot.get("token") or "")
        proxy = ctx.system_proxy()
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        mode = "bot" if str(target.get("mode") or "direct") == "bot" else "direct"
        add(
            "转发模式",
            True,
            "经 Bot（采集号 → bot → 群）" if mode == "bot" else "直连（采集号 → 群）",
        )

        if mode == "bot":
            add(
                "bot 目标群",
                bool(relay.get("chat_id")),
                f"{relay.get('title') or '未命名'}（chat_id={relay.get('chat_id')}）"
                if relay.get("chat_id")
                else "没绑定目标群，bot 收到视频也不会投递",
            )
            add("Bot Token", bool(token), "已配置" if token else "没配（设置 → Telegram Bot）")
            if token:
                try:
                    me = await bot_api.get_me(token, proxy)
                except bot_api.BotApiError as e:
                    add("bot 身份", False, str(e))
                else:
                    add(
                        "bot 身份", True,
                        f"@{me.get('username') or '?'}（id={me.get('id')}）",
                    )
                    if relay.get("chat_id"):
                        try:
                            chat = await bot_api.get_chat(
                                token, int(relay["chat_id"]), proxy,
                            )
                        except bot_api.BotApiError as e:
                            add("bot 在目标群", False, str(e))
                        else:
                            add("bot 在目标群", True, str(chat.get("title") or ""))

            # 采集号能不能被 bot 接收：bot 会自动放行所有采集账号
            collector_uid = int(account.get("user_id") or 0)
            bot_uid = int(bot.get("user_id") or 0)
            allowed = {int(a.get("user_id") or 0) for a in store.list_accounts()}
            allowed.discard(0)
            if bot_uid:
                allowed.add(bot_uid)
            if collector_uid:
                ok = collector_uid in allowed
                if collector_uid == bot_uid:
                    detail = f"采集号就是 bot 配置的用户（{collector_uid}）"
                elif ok:
                    detail = (
                        f"采集号 {collector_uid} 已作为采集账号放行"
                        f"（命令仍只认 {bot_uid}）"
                    )
                else:
                    detail = (
                        f"采集号 {collector_uid} 不在允许列表 {sorted(allowed)}，"
                        "它的私聊会被忽略"
                    )
                add("采集号能被 bot 接收", ok, detail)
            else:
                add(
                    "采集号能被 bot 接收",
                    False,
                    "账号还没同步到 user_id（去账号页重新登录 / 刷新一次）",
                )

            runner = getattr(ctx.app.state, "telegram_bot", None)
            status = runner.status() if runner is not None else {}
            add(
                "bot 轮询",
                bool(status.get("running")),
                "运行中"
                if status.get("running")
                else f"没在跑：{status.get('error') or '检查 Bot Token / 是否被 Webhook 占用'}",
            )
            if status.get("last_update_at"):
                add("bot 最近收到消息", True, str(status["last_update_at"]))

            if runner is not None and hasattr(runner, "copy_rate_status"):
                rate = runner.copy_rate_status()
                used = int((rate.get("chats") or {}).get(
                    str(int(relay.get("chat_id") or 0)), 0
                ))
                add(
                    "目标群发送额度",
                    True,
                    f"最近 {rate.get('window_seconds')} 秒已发 {used}/{rate.get('limit')} 条"
                    "（Bot API 约 20 条/分钟/群，超了会自动排队）",
                )

        watching = [
            w for w in store.list_watched(account_id=aid or None)
            if w.get("forward_enabled")
        ]
        add(
            "已开转发的源群",
            bool(watching),
            "、".join(w["title"] or str(w["chat_id"]) for w in watching)
            or "一个都没打开",
        )

        return {
            "status": 200,
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "stats": forwarder.load_stats(store, aid),
            "relay_stats": forwarder.load_relay_stats(store),
        }

    @router.post("/api/forward/link-source")
    def api_forward_link_source(body: ForwardLinkSourceIn) -> dict:
        """开关「附带来源链接」。不用重新绑定，直接改现有目标。"""
        account = account_of(body.account_id) or {}
        aid = int(account.get("id") or 0)
        target = forwarder.load_target(store, aid)
        if not target.get("chat_id"):
            raise HTTPException(status_code=400, detail="还没有转发目标")
        target["link_source"] = bool(body.enabled)
        store.set_json(forwarder.target_key(aid), target)
        relay = forwarder.load_relay_target(store)
        if relay.get("chat_id"):
            relay["link_source"] = bool(body.enabled)
            store.set_json(forwarder.RELAY_TARGET_KEY, relay)
        return snapshot(body.account_id)

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
