"""日志文件的查看与清理接口。

注意路径前缀是 `/api/logs/files` 而不是 `/api/logs`：后者已经被
`routers/listener.py` 占用了（日志**内容**，日志页要看的那个）。
两条路由同名时 FastAPI 只认先注册的那条，而 listener 排在 log_router 前面 ——
所以这里以前是**完全访问不到**的，设置页的日志面板因此一直显示空白。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import log_setup


class LogCleanupIn(BaseModel):
    keep_days: int = log_setup.RETENTION_DAYS
    clear_current: bool = False


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/logs/files")
    def get_logs() -> dict:
        return {"status": 200, **log_setup.log_files_info()}

    @router.post("/api/logs/cleanup")
    def cleanup_logs(payload: LogCleanupIn) -> dict:
        return {
            "status": 200,
            **log_setup.cleanup_logs(
                payload.keep_days,
                clear_current=payload.clear_current,
            ),
        }

    return router
