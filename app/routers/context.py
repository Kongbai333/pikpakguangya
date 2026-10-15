"""路由共享上下文：数据库、配置、Telegram 会话和跨模块小工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Coroutine

from fastapi import FastAPI, HTTPException

from app import config as config_module
from app import listener as listener_module
from app import proxy as proxy_module
from app.store import Store
from app.tg import LoginError, TelegramSession


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WebContext:
    """一次服务生命周期内共享的依赖。"""

    def __init__(self, app: FastAPI, cfg: config_module.Config,
                 store: Store, tg: TelegramSession) -> None:
        self.app = app
        self.cfg = cfg
        self.store = store
        self.tg = tg

    @property
    def collector(self):
        return self.app.state.collector

    def account_proxy(self, account: dict | None) -> proxy_module.ProxyConfig | None:
        """账号直接绑定的代理；没绑则直连。Telegram 连接（采集 / 登录）走这条。"""
        return proxy_module.proxy_from_account(self.store, account)

    def system_proxy(self) -> proxy_module.ProxyConfig | None:
        """系统代理：设置里单独选的那条全局出口。

        校验、PikPak 详情接口、秒传、Bot API 这些「不属于某个 TG 账号」的请求
        走它。没配就是直连。和账号绑的代理互不影响。
        """
        return proxy_module.system_proxy(self.store)

    def session_for(self, account: dict | None):
        """账号唯一的 TelegramSession；采集和网页端共用，避免 session 抢锁。"""
        return self.app.state.collector.session_for(account) or self.tg

    async def call(self, coro: Coroutine):
        """把 tg 层的 LoginError 转成 400，页面直接显示 detail。"""
        try:
            return await coro
        except (LoginError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    async def set_collect(self, account_id: int, enabled: bool) -> dict:
        """打开 / 关闭某个账号的采集，并把开关状态持久化。

        和 Web 端 `/api/accounts/{id}/collect` 同一套语义：启动失败要把
        collect_enabled 回滚，否则下次重启会一直尝试一个起不来的账号。
        Telegram Bot 的账号菜单也走这里，免得两处逻辑走偏。
        """
        account_id = int(account_id)
        if not self.store.get_account(account_id):
            raise ValueError("没有这个账号")
        if not enabled:
            self.store.update_account(account_id, collect_enabled=0)
            await self.collector.stop_account(account_id)
            return {"enabled": False, "accounts": self.accounts_payload()}

        self.store.update_account(account_id, collect_enabled=1)
        try:
            await self.collector.start_account(account_id)
        except Exception as e:
            self.store.update_account(account_id, collect_enabled=0)
            detail = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            raise ValueError(detail) from e
        return {"enabled": True, "accounts": self.accounts_payload()}

    def accounts_payload(self) -> list[dict]:
        """账号列表。不连 Telegram —— 页面会定期刷，不能让它等网络。

        session_exists：登录态文件还在不在；logged_in：上次同步到的用户信息。
        proxy：这个账号绑定的代理；没绑就是直连（直连是合法配置，不是错误）。
        """
        proxies = {p["id"]: p for p in self.store.list_proxies()}
        manager = self.app.state.collector
        out = []
        for a in self.store.list_accounts():
            path = self.tg.account_session_path(a["session"])
            proxy = proxies.get(a.get("proxy_id") or 0)
            px = self.account_proxy(a)
            worker = manager.worker(a["id"])
            collecting = bool(worker and worker.running)
            if collecting:
                collect_error = worker.status()["error"]
            else:
                collect_error = (
                    self.store.get_json(listener_module.status_key(a["id"])) or {}
                ).get("error", "")
            if px:
                proxy_from = f"代理：{proxy.get('name') or proxy.get('addr') or proxy.get('id')}"
                proxy_masked = px.masked()
            elif a.get("proxy_id"):
                # 绑的代理被删了。这时是直连，能跑 —— 提示一下就行，别当成故障
                proxy_from = "代理已删除，当前直连"
                proxy_masked = "直连"
            else:
                proxy_from = "直连"
                proxy_masked = "直连"
            # 只有「代理在、但 Telethon 根本用不了」才算没就绪：
            # 目前就是 ee 开头的 MTProto secret（fake-TLS，Telethon 不做域名伪装）
            proxy_warning = proxy_module.secret_warning(px.secret) if px else ""
            out.append({
                **a,
                "session_file": str(path) if path else "",
                "session_exists": bool(path and path.exists()),
                "logged_in": a.get("user_id") is not None,
                "proxy_name": (proxy or {}).get("name") or "",
                "proxy_masked": proxy_masked,
                "proxy_from": proxy_from,
                "proxy_ready": not proxy_warning,
                "proxy_warning": proxy_warning,
                "proxy_type": px.proxy_type if px else "",
                "proxy_loaded": bool(px),
                # 以运行状态为准兜底：worker 在跑时开关一定是打开态
                "collect_enabled": bool(a.get("collect_enabled")) or collecting,
                "collecting": collecting,
                "collect_error": collect_error,
            })
        return out
