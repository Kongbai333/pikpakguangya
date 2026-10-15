"""采集到新链接就立刻校验一次，不用等定时任务。

定时校验默认几小时一轮（`checker.interval_minutes`），新链接进来后要等很久
才知道「有效 / 失效」，页面上看着像没在干活。这里在采集落库的瞬间就把这条
丢进队列，后台串行校验，几秒后状态就出来了。

设计取舍：

- **串行 + 间隔**：一次只查一条，中间 `sleep`，别把 PikPak 打出限流。
  宁可慢一点，也别把链接刷成 `ERROR`。
- **队列满就丢**：丢了也不会漏 —— 定时任务那一轮会兜底。丢的时候记一笔，
  页面上能看到。
- **只校验新链接**：重复转发（`share_created=False`）不再检，
  同一个链接被转十次只花一次请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import datetime

from app import pikpak

log = logging.getLogger("checkqueue")

# 队列最多排多少条，超了就丢给定时任务兜底
DEFAULT_MAXSIZE = 300


class CheckQueue:
    """串行校验队列。跑在 Web 进程里，和采集共用一个事件循环。"""

    def __init__(self, store, checker_cfg, default_proxy=None,
                 maxsize: int = DEFAULT_MAXSIZE, on_result=None,
                 api_proxy_resolver=None) -> None:
        self.store = store
        self.cfg = checker_cfg
        self.default_proxy = default_proxy
        self.on_result = on_result
        self.api_proxy_resolver = api_proxy_resolver
        self._q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.done = 0
        self.failed = 0
        self.dropped = 0
        self.last: dict | None = None

    # ---------- 外部接口 ----------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def put(self, share_id) -> bool:
        """入队。返回 False 表示队列满了、这条被丢给定时任务。"""
        try:
            sid = int(share_id or 0)
        except (TypeError, ValueError):
            return False
        if not sid:
            return False
        try:
            self._q.put_nowait(sid)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            log.debug("校验队列满了，丢给定时任务兜底（累计丢 %s 条）", self.dropped)
            return False

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5.0)
        self._task = None

    def status(self) -> dict:
        return {
            "running": self.running,
            "pending": self._q.qsize(),
            "done": self.done,
            "failed": self.failed,
            "dropped": self.dropped,
            "last": self.last or {},
        }

    # ---------- 内部 ----------
    async def _run(self) -> None:
        log.info("新链接即时校验队列已启动")
        while not self._stop.is_set():
            try:
                sid = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._check_one(sid)
            except Exception as e:      # 单条出错不能让整个队列停摆
                self.failed += 1
                log.warning("即时校验 #%s 出错：%s: %s", sid, type(e).__name__, e)
            # 抖动一下，别形成固定节奏
            await asyncio.sleep(max(0.0, self.cfg.min_delay_seconds) * (0.4 + random.random()))
        log.info("新链接即时校验队列已停止")

    async def _check_one(self, share_id: int) -> None:
        rows = self.store.shares_by_ids([share_id])
        if not rows:
            return
        row = rows[0]
        # 采集时刚入的库，可能还没码（码在几十秒后的另一条消息里）——照样先检一次，
        # 补码之后定时任务还会再检一轮，那时就能拿到真文件名了。
        if self.api_proxy_resolver is None:
            api = pikpak._api_proxy(self.store, self.default_proxy)   # noqa: SLF001
        else:
            api = pikpak._api_proxy(   # noqa: SLF001
                self.store, self.default_proxy, self.api_proxy_resolver
            )
        res = await asyncio.to_thread(
            pikpak.check_share, row["url"], self.cfg.timeout_seconds, None,
            row.get("pass_code"), api,
        )
        self.store.save_check(
            row["id"], res.status, title=res.title, file_num=res.file_num,
            size_bytes=res.size_bytes, expire_seconds=res.expire_seconds,
            error=res.error,
        )
        self.done += 1
        self.last = {
            "link_id": row["link_id"],
            "status": res.status,
            "title": res.title,
            "at": datetime.now().strftime("%H:%M:%S"),
        }
        if self.on_result is not None:
            with contextlib.suppress(Exception):
                self.on_result(self.last)
