"""日志初始化、每日切割和清理。"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app import config as config_module

LOG_DIR = config_module.ROOT / "logs"
LOG_NAME = "server.log"
RETENTION_DAYS = 14
_STDIO_LOGS = ("server.out.log", "server.err.log")
_cleanup_lock = threading.Lock()
_log_handler: TimedRotatingFileHandler | None = None


def setup_logging() -> None:
    """配置每日日志，并清理启动前的过期文件。"""
    global _log_handler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOG_DIR / LOG_NAME,
        when="midnight",
        interval=1,
        backupCount=RETENTION_DAYS,
        encoding="utf-8",
    )
    handlers: list[logging.Handler] = [handler]
    # 后台模式的 stdout/stderr 已重定向到文件，再加 StreamHandler 会重复写一份
    if sys.stdout and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    _log_handler = handler
    # 这些库的常规 INFO 大多是每个请求/每次更新一条，量很大；保留 WARNING 以上。
    for logger_name in ("httpx", "httpcore", "telethon", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    cleanup_logs(RETENTION_DAYS)


def _log_paths() -> list[Path]:
    if not LOG_DIR.exists():
        return []
    try:
        entries = list(LOG_DIR.iterdir())
    except OSError:
        return []
    names = (LOG_NAME, *_STDIO_LOGS)
    return sorted(
        (
            path for path in entries
            if path.is_file()
            and (
                path.name in names
                or path.name.startswith(LOG_NAME + ".")
            )
        ),
        key=lambda path: path.name,
    )


def log_files_info() -> dict:
    files: list[dict] = []
    total = 0
    for path in _log_paths():
        try:
            stat = path.stat()
        except OSError:
            continue
        size = int(stat.st_size)
        total += size
        files.append({
            "name": path.name,
            "size_bytes": size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        })
    return {
        "dir": str(LOG_DIR),
        "retention_days": RETENTION_DAYS,
        "files": files,
        "total_bytes": total,
    }


def cleanup_logs(keep_days: int = RETENTION_DAYS, *,
                 clear_current: bool = False) -> dict:
    """清理过期日志；clear_current 时先轮转当前日志再删掉全部历史。"""
    keep_days = max(0, min(int(keep_days or 0), 3650))
    if clear_current:
        keep_days = 0
    deleted: list[str] = []
    freed = 0
    cleared = False
    with _cleanup_lock:
        if clear_current:
            cleared = _rotate_current()
        cutoff = time.time() - keep_days * 86400
        for path in _log_paths():
            # 历史日志和旧的 stdout/stderr 都尝试清理；被进程占用的文件会跳过
            if (
                path.name != LOG_NAME
                and not path.name.startswith(LOG_NAME + ".")
                and path.name not in _STDIO_LOGS
            ):
                continue
            if path.name == LOG_NAME:
                continue   # 当前日志已经轮转/清空，不直接删
            try:
                stat = path.stat()
            except OSError:
                continue
            if keep_days > 0 and stat.st_mtime >= cutoff:
                continue
            try:
                size = int(stat.st_size)
                path.unlink()
            except OSError:
                continue
            deleted.append(path.name)
            freed += size
    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "keep_days": keep_days,
        "clear_current": bool(clear_current),
        "cleared": cleared,
        **log_files_info(),
    }


def _rotate_current() -> bool:
    """轮转当前日志，成功后 server.log 是空文件，旧内容进入当天备份。"""
    handler = _log_handler
    if handler is None:
        return False
    try:
        handler.doRollover()
        return True
    except OSError:
        return False
