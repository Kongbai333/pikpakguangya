"""采集状态与日志：按账号聚合。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query

from app import listener as listener_module
from app.config import ROOT
from app.routers.context import WebContext
from app.store import age_seconds


def _tail_file(path: Path, lines: int) -> str:
    try:
        size = path.stat().st_size
        chunk = min(size, max(64 * 1024, min(2 * 1024 * 1024, lines * 1024)))
        with path.open("rb") as f:
            f.seek(size - chunk)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _account_name(account: dict) -> str:
    return account.get("alias") or account.get("name") or account.get("phone") or ""


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    cfg = ctx.cfg
    manager = ctx.collector
    router = APIRouter()

    def account_status(account: dict) -> dict:
        account_id = int(account["id"])
        worker = manager.worker(account_id)
        hb = store.get_json(listener_module.heartbeat_key(account_id)) or {}
        st = store.get_json(listener_module.status_key(account_id)) or {}
        last = store.get_json(listener_module.last_event_key(account_id)) or {}
        age = age_seconds(hb.get("at", ""))
        running = bool(worker and worker.running)
        if running:
            live = worker.status()
            return {
                "account_id": account_id,
                "account": live["account"],
                "enabled": bool(account.get("collect_enabled")),
                "running": True,
                "state": "running",
                "error": live["error"],
                "heartbeat_at": hb.get("at", ""),
                "heartbeat_age_seconds": None if age is None else round(age, 1),
                "started_at": st.get("started_at", ""),
                "stopped_at": "",
                "origin": live["origin"],
                "chats": live["chats"],
                "last_event": last,
                "log_tail": live["log_tail"],
                "problems": [],
            }
        return {
            "account_id": account_id,
            "account": _account_name(account),
            "enabled": bool(account.get("collect_enabled")),
            "running": False,
            "state": st.get("state", ""),
            "error": st.get("error", ""),
            "heartbeat_at": hb.get("at", ""),
            "heartbeat_age_seconds": None if age is None else round(age, 1),
            "started_at": st.get("started_at", ""),
            "stopped_at": st.get("stopped_at", ""),
            "origin": st.get("origin", ""),
            "chats": st.get("chats", []),
            "last_event": last,
            "log_tail": "",
            "problems": (
                listener_module.preflight(store, cfg, account)
                if account.get("collect_enabled") else []
            ),
        }

    @router.get("/api/listener")
    def api_listener() -> dict:
        """采集状态。按账号聚合，顶层的旧字段取自第一个在跑的账号，兼容旧页面。"""
        accounts = [account_status(a) for a in store.list_accounts()]
        running = [a for a in accounts if a["running"]]
        primary = running[0] if running else (accounts[0] if accounts else {})
        active = ctx.tg.account() or {}
        # 汇总「开了开关但没跑起来」的账号问题，页面上直接给出原因
        problems: list[str] = []
        for item in accounts:
            if item["enabled"] and not item["running"]:
                for p in item.get("problems") or []:
                    if p not in problems:
                        problems.append(p)
        return {
            "status": 200,
            "running": bool(running),
            "accounts": accounts,
            "heartbeat_at": primary.get("heartbeat_at", ""),
            "heartbeat_age_seconds": primary.get("heartbeat_age_seconds"),
            "stale_after_seconds": listener_module.HEARTBEAT_STALE_SECONDS,
            "pid": 0,
            "state": primary.get("state", ""),
            "started_at": primary.get("started_at", ""),
            "stopped_at": primary.get("stopped_at", ""),
            "origin": primary.get("origin", ""),
            "account": primary.get("account", ""),
            "chats": primary.get("chats", []),
            "last_event": primary.get("last_event", {}),
            # 采集只跑在各自账号上；顶层 watched 仍给当前使用账号的勾选
            "watched": store.list_watched(
                only_enabled=True, account_id=active.get("id")
            ),
            "chat_stats": store.chat_stats(),
            "log_tail": primary.get("log_tail", ""),
            "check_queue": manager.queue.status(),
            "problems": problems,
        }

    @router.get("/api/activity")
    def api_activity(minutes: int = Query(default=60, ge=10, le=240)) -> dict:
        """仪表盘实时监控：最近 N 分钟按分钟汇总的采集活动 + 校验队列。"""
        return {
            "status": 200,
            **store.activity_stats(minutes),
            "check_queue": manager.queue.status(),
            "collecting": manager.running,
        }

    @router.get("/api/logs")
    def api_logs(lines: int = Query(default=300, ge=20, le=2000)) -> dict:
        """统一日志页：每个账号的采集日志 + 服务进程日志。"""
        sources = []
        for account in store.list_accounts():
            worker = manager.worker(account["id"])
            sources.append({
                "key": f"collector:{account['id']}",
                "name": f"采集 · {_account_name(account) or account['id']}",
                "path": "",
                "text": worker.log_tail() if worker is not None else "",
            })

        server_out = _tail_file(ROOT / "logs" / "server.out.log", lines)
        server_err = _tail_file(ROOT / "logs" / "server.err.log", lines)
        server_app = _tail_file(ROOT / "logs" / "server.log", lines)
        server_parts = []
        if server_app:
            server_parts.append("===== server.log =====\n" + server_app)
        if server_out:
            server_parts.append("===== server.out.log =====\n" + server_out)
        if server_err:
            server_parts.append("===== server.err.log =====\n" + server_err)
        sources.append({
            "key": "server",
            "name": "服务",
            "path": str(ROOT / "logs"),
            "text": "\n\n".join(server_parts),
        })

        return {
            "status": 200,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
        }

    return router
