"""设置、健康检查、代理池和 PikPak 详情接口开关。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import __version__, config as config_module
from app import pikpak_api
from app import proxy as proxy_module
from app import telegram_bot
from app import web_public
from app.routers.context import WebContext

log = logging.getLogger("settings")
PIKPAK_API_KEY = "pikpak.api.enabled"


class ProxyItemIn(BaseModel):
    """代理池里的一条代理。

    raw 是「粘贴一条链接」入口：填了就以它为准，自动拆成下面这些字段。
    """
    name: str = ""
    proxy_type: str = "http"
    addr: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    # 只有 mtproto 用：tg://proxy 链接里的 secret
    secret: str = ""
    raw: str = ""


class SystemProxyIn(BaseModel):
    """系统代理。proxies 表里的 id，0 = 直连。"""
    proxy_id: int = 0


class ToggleIn(BaseModel):
    enabled: bool = True


class TelegramBotIn(BaseModel):
    bot_token: str = ""
    user_id: int = 0


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    cfg = ctx.cfg
    tg = ctx.tg
    manager = ctx.collector
    router = APIRouter()

    def validate_proxy_payload(payload: ProxyItemIn) -> dict:
        # 粘贴链接优先：tg://proxy?... / socks5://... / 1.2.3.4:1080:user:pass 都认
        raw = (payload.raw or "").strip()
        if raw:
            try:
                data = proxy_module.parse_proxy_input(raw)
            except proxy_module.ProxyParseError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            # 名称手填了就用手的，链接里没有名字
            if (payload.name or "").strip():
                data["name"] = payload.name.strip()
            return data

        if payload.proxy_type not in proxy_module.SUPPORTED_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"代理类型只支持 {' / '.join(proxy_module.SUPPORTED_TYPES)}",
            )
        if not (payload.addr or "").strip():
            raise HTTPException(status_code=400, detail="代理地址不能为空")
        if not (0 < int(payload.port or 0) < 65536):
            raise HTTPException(status_code=400, detail="代理端口不合法")
        secret = (payload.secret or "").strip()
        if payload.proxy_type == proxy_module.MT_PROXY_TYPE:
            if not secret:
                raise HTTPException(
                    status_code=400, detail="MTProto 代理要填 secret（tg://proxy 链接里的那串）"
                )
            if not proxy_module.SECRET_HEX_RE.match(secret):
                raise HTTPException(
                    status_code=400,
                    detail="MTProto secret 应该是 32 位十六进制（可以带 dd / ee 前缀）",
                )
        return {
            "name": (payload.name or "").strip(),
            "proxy_type": payload.proxy_type,
            "addr": payload.addr.strip(),
            "port": int(payload.port),
            "username": (payload.username or "").strip(),
            "password": payload.password or "",
            "secret": secret,
        }

    @router.get("/api/health")
    def api_health() -> dict:
        return {"status": 200, "version": __version__, "db": str(cfg.db_path())}

    @router.get("/api/settings")
    def api_settings() -> dict:
        """设置页要的东西：应用凭据 + 环境信息。

        api_hash 和 Bot Token 永不回显，只回「是否已设置」。
        """
        cred = tg.credentials()
        proxy = ctx.system_proxy()
        proxy_text = proxy.masked() if proxy is not None else "直连"
        bot = ctx.app.state.telegram_bot.status()
        return {
            "status": 200,
            "credentials": {
                "api_id": cred["api_id"],
                "api_hash_set": bool(cred["api_hash"]),
                "phone": cred["phone"],
                "source": cred["source"],
            },
            "bot": bot,
            "paths": {
                "db": str(cfg.db_path()),
                "session": str(tg.session_path()),
                "root": str(config_module.ROOT),
            },
            "version": __version__,
            "proxy_masked": proxy_text,
        }

    @router.put("/api/settings/telegram-bot")
    async def api_telegram_bot_save(payload: TelegramBotIn) -> dict:
        try:
            telegram_bot.save_bot_settings(
                store,
                payload.bot_token,
                payload.user_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await ctx.app.state.telegram_bot.restart()
        return {"status": 200, "bot": ctx.app.state.telegram_bot.status()}

    @router.get("/api/proxies")
    def api_proxies() -> dict:
        return {"status": 200, "proxies": store.list_proxies()}

    @router.post("/api/proxies")
    def api_proxy_add(payload: ProxyItemIn) -> dict:
        data = validate_proxy_payload(payload)
        return {"status": 200, "proxy": store.add_proxy(**data)}

    # 注意：这两条必须注册在 /api/proxies/{proxy_id} 之前，
    # 否则 "system" 会被当成 proxy_id 去解析，直接 422。
    @router.get("/api/proxies/system")
    def api_system_proxy() -> dict:
        """系统代理：不属于任何 TG 账号的那些请求走它。

        PikPak 校验、分享详情接口、秒传、Telegram Bot API 都用这一条。
        账号采集不走它 —— 那是账号自己在「代理池」里绑的那条。
        """
        proxy_id = proxy_module.system_proxy_id(store)
        row = store.get_proxy(proxy_id) if proxy_id else None
        cfg = proxy_module.system_proxy(store)
        return {
            "status": 200,
            "proxy_id": int(row["id"]) if row else 0,
            # 选了但用不了（MTProto 代理代理不了 HTTP）时为 False
            "usable": bool(cfg),
            "masked": cfg.masked() if cfg else "直连",
            "proxies": store.list_proxies(),
        }

    @router.put("/api/proxies/system")
    def api_system_proxy_put(payload: SystemProxyIn) -> dict:
        try:
            proxy_module.set_system_proxy(store, payload.proxy_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return api_system_proxy()

    @router.post("/api/proxies/parse")
    def api_proxy_parse(payload: ProxyItemIn) -> dict:
        """把粘贴的一行解析成字段，给添加表单做预览。"""
        raw = (payload.raw or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="先粘贴一条代理链接")
        try:
            data = proxy_module.parse_proxy_input(raw)
        except proxy_module.ProxyParseError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        data["secret_warning"] = proxy_module.secret_warning(data.get("secret") or "")
        return {"status": 200, **data}

    @router.put("/api/proxies/{proxy_id}")
    async def api_proxy_edit(proxy_id: int, payload: ProxyItemIn) -> dict:
        if not store.get_proxy(proxy_id):
            raise HTTPException(status_code=404, detail="没有这个代理")
        data = validate_proxy_payload(payload)
        store.update_proxy(proxy_id, **data)
        # 绑了这条代理的账号，采集要重连才会走新出口
        for account in store.list_accounts():
            if int(account.get("proxy_id") or 0) == proxy_id:
                await manager.restart_account(account["id"])
        return {"status": 200, "proxy": store.get_proxy(proxy_id)}

    @router.delete("/api/proxies/{proxy_id}")
    async def api_proxy_delete(proxy_id: int) -> dict:
        affected = [
            a for a in store.list_accounts()
            if int(a.get("proxy_id") or 0) == proxy_id
        ]
        if not store.delete_proxy(proxy_id):
            raise HTTPException(status_code=404, detail="没有这个代理")
        # 代理没了，正在跑的采集要停掉 —— 连接还挂在旧出口上，得重连才会变。
        # 采集开关保留：重新打开就走直连（直连是合法配置，不是故障）。
        for account in affected:
            await manager.stop_account(account["id"])
            manager.mark_error(
                account["id"], "绑定的代理已删除，采集已停止。重新打开采集会走直连"
            )
        return {"status": 200, "deleted": True, "accounts": ctx.accounts_payload()}

    @router.post("/api/proxies/{proxy_id}/test")
    async def api_proxy_test(proxy_id: int) -> dict:
        row = store.get_proxy(proxy_id)
        if not row:
            raise HTTPException(status_code=404, detail="没有这个代理")
        proxy_cfg = proxy_module.from_proxy_row(row)
        result = await asyncio.to_thread(proxy_module.test_connectivity, proxy_cfg, 10.0)
        return {"status": 200, **result}

    @router.get("/api/pikpak-api")
    def api_pikpak_api() -> dict:
        p = ctx.system_proxy()
        # 不回显 proxy_url：它是 socks5://user:password@host:port，带明文密码，
        # 而前端只用得到 masked 版本。
        return {
            "status": 200,
            "enabled": store.get_state(PIKPAK_API_KEY, "1") != "0",
            "proxy_ready": bool(p and p.to_url()),
            "proxy_masked": p.masked() if p else "",
            "proxy_from": "系统代理" if p else "直连（没配系统代理）",
            "action": pikpak_api.SHARE_ACTION,
        }

    @router.put("/api/pikpak-api")
    def api_pikpak_api_put(payload: ToggleIn) -> dict:
        store.set_state(PIKPAK_API_KEY, "1" if payload.enabled else "0")
        return {"status": 200, "enabled": bool(payload.enabled)}

    @router.get("/api/web-public")
    def api_web_public() -> dict:
        """前台浏览页开关。关掉后 `/` 和公开的 items / stats 接口一起拒绝。"""
        return {"status": 200, "enabled": web_public.is_enabled(store)}

    @router.put("/api/web-public")
    def api_web_public_put(payload: ToggleIn) -> dict:
        web_public.set_enabled(store, payload.enabled)
        log.info("前台浏览页已%s", "打开" if payload.enabled else "关闭")
        return {"status": 200, "enabled": bool(payload.enabled)}

    return router
