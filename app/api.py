"""FastAPI 服务：组合各业务路由 + 托管静态页。

启动： python -m app.api      或   uvicorn app.api:create_app --factory --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__, config as config_module
from app import collector as collector_module
from app import integrations
from app import log_router
from app import log_setup
from app import pikpak
from app import proxy as proxy_module
from app import timeutil
from app import transfer as transfer_module
from app import transfer_router
from app import telegram_bot
from app import web_auth
from app import web_public
from app.routers import accounts, chats, links, listener, media, settings, static
from app.routers.context import WebContext
from app.store import Store
from app.tg import TelegramSession

log = logging.getLogger("api")


def create_app(cfg=None, tg_session=None) -> FastAPI:
    cfg = cfg or config_module.load()
    admin_auth = web_auth.AdminAuth(cfg.admin)
    store = Store(cfg.db_path(), tz=timeutil.resolve(cfg.app.timezone))
    store.init()
    # 管理页用的 Telegram 会话（懒连接）。测试里可以注入假的。
    tg = tg_session or TelegramSession(cfg, store)
    # 老库升级：把 config.toml 里那个 session 登记成账号，否则账号页是空的。
    # 放在 tg 之后 —— 顺手把默认手机号填进去，省得用户看到一行空手机号。
    if store.ensure_default_account(cfg.session_path().name):
        row = store.active_account() or {}
        phone = (tg.credentials() or {}).get("phone") or ""
        if row.get("id") and phone:
            store.update_account(row["id"], phone=phone)
        log.info("账号表是空的，已把 %s 登记为第一个账号", cfg.session_path().name)
    # 老库的监听勾选没有账号归属，挂到刚登记的「当前使用」账号上
    moved = store.assign_orphan_watched()
    if moved:
        log.info("已把 %s 个监听群归到当前账号", moved)
    # 以前全局出口 = 「当前使用」账号绑的代理，现在拆成了独立的系统代理设置。
    # 不迁的话升级完校验/秒传/Bot 会突然变直连。
    if proxy_module.adopt_system_proxy(store):
        log.info("已把当前账号绑定的代理设为系统代理（可在「设置 → 代理」里改）")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx: WebContext = app.state.ctx
        task = None
        if cfg.checker.enabled:
            task = asyncio.create_task(
                pikpak.run_forever(store, cfg.checker, api_proxy_resolver=ctx.system_proxy)
            )
            log.info("后台校验已启动，每 %s 分钟一批", cfg.checker.interval_minutes)

        # 采集按账号开关自动拉起。放后台跑，启动失败只影响那个账号，不挡服务启动。
        # 看门狗等自动拉起跑完再开：不然两边可能同时去 start 同一个账号。
        async def _auto_collect() -> None:
            with contextlib.suppress(Exception):
                await app.state.collector.start_enabled()
            # 采集任务自己挂掉（断网、session 被踢、回溯抛异常）时自动拉起，
            # 否则页面一直显示「没在运行」，只有重启进程才会恢复。
            app.state.collector.start_watchdog()

        auto_task = asyncio.create_task(_auto_collect())
        app.state.transfers.start()
        await app.state.telegram_bot.start()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await app.state.telegram_bot.stop()
            app.state.transfers.stop()
            auto_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await auto_task
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            with contextlib.suppress(Exception):
                await app.state.collector.shutdown()
            with contextlib.suppress(Exception):
                await tg.disconnect()

    app = FastAPI(title="PikPak 链接库", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.tg = tg
    app.state.collector = collector_module.CollectorManager(store, cfg)
    app.state.telegram_bot = telegram_bot.TelegramBotRunner(store, cfg)

    ctx = WebContext(app, cfg, store, tg)
    app.state.ctx = ctx
    # 秒传任务队列：解析 PikPak 分享、逐个调光鸭秒传接口，状态落库供页面轮询。
    app.state.transfers = transfer_module.TransferManager(
        store, proxy_resolver=ctx.system_proxy
    )
    app.state.telegram_bot.transfer_manager = app.state.transfers

    @app.middleware("http")
    async def require_admin_auth(request, call_next):
        """保护管理接口，只放行前台浏览和登录所需的最小 API。"""
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        # 前台总开关：关掉之后公开接口一起拒。只藏页面不拦接口，
        # 数据照样能直接拉走，等于没关。
        # 这个判断放在「是不是公开接口」之前 —— 挂在那之后的话，
        # 以后往 PUBLIC_PATHS 里加路径却忘了同步 web_auth 的白名单，
        # 就会静默失效。
        if path in web_public.PUBLIC_PATHS and not web_public.is_enabled(store):
            return JSONResponse(
                status_code=403,
                content={"detail": "前台浏览页已关闭，可在「设置」里重新打开"},
            )
        if not path.startswith("/api/") or web_auth.is_public_api(path):
            return await call_next(request)
        if not admin_auth.configured:
            return JSONResponse(
                status_code=503,
                content={"detail": "未配置管理员密码，请设置 config.toml 的 [admin] password"},
            )
        if not admin_auth.verify(request.cookies.get(web_auth.COOKIE_NAME)):
            return JSONResponse(status_code=401, content={"detail": "请先登录管理后台"})
        return await call_next(request)

    # Telegram、采集和 PikPak 详情统一按账号取直接代理。
    if hasattr(tg, "proxy_resolver"):
        tg.proxy_resolver = ctx.account_proxy
    app.state.telegram_bot.proxy_resolver = ctx.system_proxy
    app.state.telegram_bot.accounts_provider = ctx.accounts_payload
    app.state.telegram_bot.account_session_for = ctx.session_for
    app.state.telegram_bot.collect_control = ctx.set_collect
    app.state.collector.queue.api_proxy_resolver = ctx.system_proxy

    for router in (
        web_auth.build_router(admin_auth),
        integrations.build_router(ctx),
        accounts.build_router(ctx),
        chats.build_router(ctx),
        links.build_router(ctx),
        listener.build_router(ctx),
        settings.build_router(ctx),
        media.build_router(ctx),
        transfer_router.build_router(ctx),
        log_router.build_router(),
    ):
        app.include_router(router)
    # 静态路由必须最后注册，否则 /{filename} 兜底会挡住前面的接口
    static.mount_assets(app)
    app.include_router(static.build_router(store))

    return app


def main() -> None:
    import uvicorn

    log_setup.setup_logging()
    cfg = config_module.load()
    uvicorn.run(
        create_app(cfg),
        host=cfg.api.host,
        port=cfg.api.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
