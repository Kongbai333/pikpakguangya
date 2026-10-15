"""按账号并行采集：每个开启采集的账号一套独立 client / 心跳 / 回溯游标。

每个账号一个 session 文件，所以多个账号可以在同一个进程里并行采集；
同一个账号的登录、群列表和采集仍共用它自己的 TelegramSession。

旧的独立进程模式（listen.ps1 + app/runner.py）已删除：采集统一跑在
Web 进程内，程序启动时按账号的采集开关自动拉起。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Any

from app import listener as L
from app.checkqueue import CheckQueue
from app.parser import CodeMerger
from app.store import age_seconds, iso, utcnow
from app.tg import TelegramSession

log = logging.getLogger("collector")

# 停止时等采集任务收尾的时间
STOP_TIMEOUT_SECONDS = 8.0
# 启动后等第一次心跳落库的时间（页面上要能立刻看到「在运行」）
START_WAIT_SECONDS = 4.0
# 运行中定期补偿回溯的间隔。
# 实时 handler 出错时那条消息就没了（游标不动，只有下次重启才会补回来），
# 而服务可能几周不重启 —— 所以每隔一段时间按游标重扫一遍兜底。
# 已经处理过的消息在内存里直接跳过（见 _done_ids），所以这个间隔可以短。
CATCHUP_SECONDS = 120
# 每个群在内存里记住多少条「已处理」的消息 id。
# 补偿回溯从游标重扫时靠它跳过刚处理完的消息；只记成功的，
# 失败的消息不在里面，正好被下一轮补偿捡回来。
DONE_IDS_PER_CHAT = 2000
# 看门狗巡检间隔：账号开着采集开关但任务没在跑时自动拉起
WATCHDOG_SECONDS = 60
# 看门狗重启失败后的退避上限（秒）
WATCHDOG_MAX_BACKOFF = 600
# 单条消息处理失败时的重试次数（DB 被占用这类瞬时错误能扛过去）
INGEST_TRIES = 3
# 重试间隔基数：第 n 次失败后等 n * 基数
INGEST_RETRY_SLEEP = 0.4


class Collector:
    """一个账号的采集任务：独立 session、心跳、群勾选、游标和日志。"""

    def __init__(self, account: dict, session: TelegramSession, store, cfg,
                 queue: CheckQueue) -> None:
        self.account = account
        self.account_id = int(account["id"])
        self.session = session
        self.store = store
        self.cfg = cfg
        self.queue = queue
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._chats: list[Any] = []
        self._origin = ""
        self._error = ""
        # 解析失败的群（账号没加入 / 已注销 / 用户名改了）。挂在状态里给页面看，
        # 否则「5 个群挂了 1 个」只能靠翻日志才发现。
        self._missing_chats: list[str] = []
        # 实时链路已经处理成功的消息 id（每群一份，有上限）。
        # 补偿回溯从游标重扫时必然撞上一堆刚处理完的消息，靠它在内存里跳过，
        # 省掉重复的解析、落库和缩略图触发。只记成功的 —— 失败的消息不在里面，
        # 下一轮补偿正好把它捡回来。
        self._done_ids: dict[int, dict[int, None]] = {}
        # 这个账号最近的事件；内置采集的日志在 Web 控制台，管理页看不到，
        # 所以留一份在内存里给页面显示（重启就没了，无所谓）。
        self._log_lines: deque[str] = deque(maxlen=60)

    # ---------- 日志 / 状态 ----------
    def _note(self, msg: str) -> None:
        self._log_lines.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def note(self, msg: str) -> None:
        """外部（看门狗）往这个账号的日志里补一行。"""
        self._note(msg)

    def log_tail(self) -> str:
        return "\n".join(self._log_lines)

    def _mark_done(self, chat_id: int, msg_id: int) -> None:
        if not chat_id or not msg_id:
            return
        ids = self._done_ids.setdefault(int(chat_id), {})
        ids[int(msg_id)] = None
        # dict 保持插入顺序，超了就丢最早的
        while len(ids) > DONE_IDS_PER_CHAT:
            ids.pop(next(iter(ids)))

    def _already_done(self, chat_id: int, msg_id: int) -> bool:
        return int(msg_id) in self._done_ids.get(int(chat_id), ())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def heartbeat_fresh(self) -> bool:
        hb = self.store.get_json(L.heartbeat_key(self.account_id)) or {}
        age = age_seconds(hb.get("at", ""))
        return age is not None and age < L.HEARTBEAT_STALE_SECONDS

    def status(self) -> dict:
        account = self.store.get_account(self.account_id) or self.account
        name = account.get("alias") or account.get("name") or account.get("phone") or ""
        return {
            "account_id": self.account_id,
            "account": name,
            "embedded": True,
            "pid": os.getpid(),
            "running": self.running,
            "chats": [
                {"id": int(getattr(c, "id", 0) or 0), "title": L._chat_title(c)}
                for c in self._chats
            ],
            "origin": self._origin,
            "error": self._error,
            "missing_chats": list(self._missing_chats),
            "log_tail": self.log_tail(),
            "check_queue": self.queue.status(),
        }

    # ---------- 起停 ----------
    async def start(self) -> dict:
        if self.running:
            return {**self.status(), "already_running": True}

        self.account = self.store.get_account(self.account_id) or self.account
        problems = L.preflight(self.store, self.cfg, self.account)
        if problems:
            raise ValueError("不能启动采集：\n- " + "\n- ".join(problems))

        client = await self.session.get_client()
        if not await client.is_user_authorized():
            await self.session.disconnect()
            raise ValueError("账号还没登录：先在「账号」页登录")

        specs, origin = L.watch_specs(self.store, self.cfg, self.account)
        chats, missing = await L._resolve_chats(client, specs)
        if not chats:
            await self.session.disconnect()
            raise ValueError(
                f"没有可用的群（来源：{origin}），检查账号是否已加入这些群"
                + (f"。解析失败：{'、'.join(missing[:5])}" if missing else "")
            )

        self._chats = chats
        self._origin = origin
        self._error = ""
        self._missing_chats = missing
        if missing:
            log.warning(
                "账号 %s 有 %s 个群解析失败：%s", self.account_id, len(missing), missing
            )
        self._stop = asyncio.Event()
        self._done_ids.clear()
        self._task = asyncio.create_task(self._run(client, chats, origin))
        self._task.add_done_callback(self._task_finished)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_WAIT_SECONDS
        while loop.time() < deadline:
            if self._task.done() or self.heartbeat_fresh():
                break
            await asyncio.sleep(0.1)
        if self._task.done():
            # 任务自己挂了 —— 把原因抛给调用方，别假装启动成功
            err = self._task.exception()
            self._task = None
            await self.session.disconnect()
            raise ValueError(f"采集任务启动就退出了：{err}" if err else "采集任务启动就退出了")
        return self.status()

    async def stop(self) -> dict:
        if not self.running:
            L.mark_stopped(self.store, self.account_id)
            await self.session.disconnect()
            return {"stopped": False, "reason": "采集没在跑", "how": "embedded"}

        self._stop.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_SECONDS)
        self._task = None
        self._chats = []
        await self.session.disconnect()
        L.mark_stopped(self.store, self.account_id)
        return {"stopped": True, "how": "embedded"}

    async def shutdown(self) -> None:
        """Web 进程退出时收尾。不抛异常。"""
        with contextlib.suppress(Exception):
            await self.stop()

    # ---------- 内部 ----------
    def _task_finished(self, task: asyncio.Task) -> None:
        """把任务的异常取走，免得 asyncio 打「Task exception was never retrieved」。

        采集任务在后台自己挂掉时没人 await 它；原因 _run 已经记过日志了，
        这里只是收尾。看门狗会负责把它重新拉起来。
        """
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(L.HEARTBEAT_SECONDS)
            L._beat(self.store, self.account_id)

    def _log_saved(self, result: dict) -> None:
        for s in (result or {}).get("saved", []):
            if s.get("update_only"):
                self._note(f"补码 {s['link_id']} → {s['pass_code']}")
                continue
            if not s.get("share_created"):
                continue
            self._note(f"新链接 {s['link_id']} 码={s['pass_code'] or '无'} 备注={s['remark']}")
            # 新链接立刻排进校验队列，几秒后状态和文件名就出来了
            self.queue.put(s.get("share_id"))

    async def _ingest_once(self, obj, chat, merger, what: str) -> None:
        """处理一条消息，失败重试几次。

        以前是一次不成就算了 —— 而实时链路没有游标，那条消息就永久丢了
        （只有下次重启的回溯才会补回来，而服务可能几周不重启）。
        瞬时错误（DB 被占用、Telegram 抖一下）重试一两次基本都能过；
        真过不去的交给 _catchup 兜底。

        处理成功才记进 _done_ids —— 没记进去的消息，补偿回溯会再捡一次。
        """
        # Event 和裸 Message 的 .id 都是消息 id
        msg_id = int(getattr(obj, "id", 0) or 0)
        chat_id = int(getattr(chat, "id", 0) or 0)
        for attempt in range(INGEST_TRIES):
            try:
                self._log_saved(
                    await L._ingest(obj, chat, self.store, merger, self.cfg, self.account_id)
                )
            except Exception as e:
                if attempt == INGEST_TRIES - 1:
                    self._note(
                        f"{what}处理失败（已重试 {INGEST_TRIES} 次，"
                        f"留给补偿回溯）：{type(e).__name__}: {e}"
                    )
                    log.exception("账号 %s %s处理失败", self.account_id, what)
                    return
                await asyncio.sleep(INGEST_RETRY_SLEEP * (attempt + 1))
            else:
                self._mark_done(chat_id, msg_id)
                return

    async def _catchup(self, client, chats: list[Any], merger: CodeMerger) -> None:
        """运行中定期按游标重扫，把实时链路漏掉的补回来。

        实时链路没有游标：handler 抛异常、Telegram 少推一条 update、
        连接抖一下 —— 这些消息都不会进库，而游标还停在原地，
        只有下次重启的回溯才会补回来（服务可能几周不重启）。

        回溯本身是游标驱动 + 幂等的（add_message 用 INSERT OR IGNORE），
        所以隔一会儿重跑一遍是安全的；刚处理过的消息由 _done_ids 在内存里
        跳过，没有缺口时每群只花一次空请求。跑完游标就跟上了最新，
        重启时也不用从很久以前开始补。
        """
        while True:
            await asyncio.sleep(CATCHUP_SECONDS)
            try:
                n = await L._backfill(
                    client, self.store, merger, self.cfg, chats, self.account_id,
                    catch_up=True, skip=self._already_done,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("账号 %s 补偿回溯出错：%s: %s", self.account_id, type(e).__name__, e)
                continue
            if n:
                self._note(f"补偿回溯补齐 {n} 条（实时链路漏了）")
                log.warning("账号 %s 补偿回溯补齐 %s 条消息", self.account_id, n)

    async def _run(self, client, chats: list[Any], origin: str) -> None:
        from telethon import events

        store, cfg = self.store, self.cfg
        account_id = self.account_id
        merger = CodeMerger(cfg.collector.merge_window_seconds)

        @client.on(events.NewMessage(chats=chats))
        async def _on_new(event):  # noqa: ANN001
            await self._ingest_once(event, event.chat, merger, "新消息")

        @client.on(events.MessageEdited(chats=chats))
        async def _on_edit(event):  # noqa: ANN001
            await self._ingest_once(event, event.chat, merger, "编辑消息")

        me = await client.get_me()
        store.set_json(L.status_key(account_id), {
            "state": "running",
            "mode": "embedded",
            "account_id": account_id,
            "pid": os.getpid(),
            "started_at": iso(utcnow()),
            "stopped_at": "",
            "origin": origin,
            "account": getattr(me, "username", None) or getattr(me, "first_name", "") or "",
            "chats": [
                {"id": int(getattr(c, "id", 0) or 0), "title": L._chat_title(c)}
                for c in chats
            ],
        })
        L._beat(store, account_id)
        self._note(f"开始监听 {len(chats)} 个群（{origin}）")
        if self._missing_chats:
            self._note(f"有 {len(self._missing_chats)} 个群解析失败：{'、'.join(self._missing_chats[:3])}")

        hb_task = asyncio.create_task(self._heartbeat())
        catchup_task: asyncio.Task | None = None
        try:
            await L._backfill(client, store, merger, cfg, chats, account_id)
            self._note("回溯完成，等新消息")
            log.info("账号 %s 采集已启动：%s 个群（%s）", account_id, len(chats), origin)
            # 首次回溯跑完再开补偿，两边都动游标，错开就不用担心互相覆盖
            catchup_task = asyncio.create_task(self._catchup(client, chats, merger))
            await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            st = store.get_json(L.status_key(account_id)) or {}
            store.set_json(
                L.status_key(account_id),
                {**st, "state": "error", "error": self._error},
            )
            self._note(f"采集出错：{self._error}")
            log.exception("账号 %s 采集任务出错", account_id)
            raise
        finally:
            hb_task.cancel()
            if catchup_task is not None:
                catchup_task.cancel()
                # CancelledError 是 BaseException，suppress(Exception) 拦不住 ——
                # 漏出去的话 stop() 会跟着抛，停止采集就整个坏了
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await catchup_task
            with contextlib.suppress(Exception):
                client.remove_event_handler(_on_new)
                client.remove_event_handler(_on_edit)
            L.mark_stopped(store, account_id)
            self._note("已停止")
            log.info("账号 %s 采集已停止", account_id)


class CollectorManager:
    """所有账号采集的管理者：共享一条校验队列，按账号启停。"""

    def __init__(self, store, cfg) -> None:
        self.store = store
        self.cfg = cfg
        # 校验队列全局共享：多账号同时采集时，PikPak 的请求密度不随账号数放大
        self.queue = CheckQueue(store, cfg.checker, None)
        self._workers: dict[int, Collector] = {}
        # 每个账号一个 TelegramSession，采集和网页端共用，避免同一 session 文件开两个 client
        self._sessions: dict[int, TelegramSession] = {}
        self._watchdog: asyncio.Task | None = None

    # ---------- 查询 ----------
    @property
    def running(self) -> bool:
        return any(w.running for w in self._workers.values())

    def is_running(self, account_id: int) -> bool:
        worker = self._workers.get(int(account_id))
        return bool(worker and worker.running)

    def worker(self, account_id: int) -> Collector | None:
        return self._workers.get(int(account_id))

    def session_for(self, account: dict | None) -> TelegramSession | None:
        """账号唯一的 TelegramSession；采集、群列表、状态查询都走它。"""
        if not account or not account.get("id"):
            return None
        account_id = int(account["id"])
        session = self._sessions.get(account_id)
        if session is None:
            session = TelegramSession(self.cfg, self.store, account=account)
            self._sessions[account_id] = session
        return session

    def forget_account(self, account_id: int) -> None:
        """账号删掉后把 worker 一起清掉，别留孤立的 session 对象。"""
        self._workers.pop(int(account_id), None)
        self._sessions.pop(int(account_id), None)

    # ---------- 起停 ----------
    async def start_account(self, account_id: int) -> dict:
        account = self.store.get_account(int(account_id))
        if not account:
            raise ValueError("没有这个账号")
        await self._ensure_queue()
        worker = self._worker_for(account)
        try:
            return await worker.start()
        except Exception:
            await self._stop_queue_if_idle()
            raise

    async def stop_account(self, account_id: int) -> dict:
        worker = self._workers.get(int(account_id))
        if worker is None:
            L.mark_stopped(self.store, int(account_id))
            await self._stop_queue_if_idle()
            return {"stopped": False, "reason": "采集没在跑"}
        result = await worker.stop()
        await self._stop_queue_if_idle()
        return result

    async def restart_account(self, account_id: int) -> None:
        """改代理后重启这个账号的采集；原本没跑就不动。"""
        worker = self._workers.get(int(account_id))
        if worker is None or not worker.running:
            return
        await self.stop_account(account_id)
        with contextlib.suppress(Exception):
            await self.start_account(account_id)

    async def start_enabled(self) -> list[dict]:
        """程序启动时自动拉起所有开了采集开关的账号；单个失败不影响其他账号。"""
        results = []
        for account in self.store.list_accounts():
            if not int(account.get("collect_enabled") or 0):
                continue
            try:
                result = await self.start_account(account["id"])
                results.append({"account_id": account["id"], "ok": True, **result})
            except Exception as e:
                self.mark_error(account["id"], str(e))
                log.warning("账号 %s 自动采集启动失败：%s", account["id"], e)
                results.append({"account_id": account["id"], "ok": False, "error": str(e)})
        return results

    async def shutdown(self) -> None:
        await self.stop_watchdog()
        for account_id in list(self._workers):
            with contextlib.suppress(Exception):
                await self.stop_account(account_id)
        with contextlib.suppress(Exception):
            await self.queue.stop()

    # ---------- 看门狗 ----------
    def start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch())

    async def stop_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _watch(self) -> None:
        """账号开着采集开关、任务却没在跑时自动拉起。

        采集任务挂掉的方式很安静：网络断了、Telegram 把 session 踢了、
        回溯时抛异常 —— 任务就结束了，页面显示「没在运行」，而用户不会天天
        去点那个按钮，于是就这么一直不采。以前只有重启进程才会恢复。

        启动失败按 2 的幂退避（上限 WATCHDOG_MAX_BACKOFF），一个坏账号
        （比如代理被删了）不会把日志刷爆。用户主动停采集时 set_collect 会把
        collect_enabled 置 0，看门狗不会跟用户对着干。
        """
        fails: dict[int, int] = {}
        retry_at: dict[int, float] = {}
        while True:
            await asyncio.sleep(WATCHDOG_SECONDS)
            try:
                await self._sweep(fails, retry_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 巡检本身出错（比如库被占用）不能让看门狗死掉
                log.exception("看门狗巡检出错，下一轮继续")

    async def _sweep(self, fails: dict[int, int], retry_at: dict[int, float]) -> None:
        now = time.monotonic()
        for account in self.store.list_accounts():
            account_id = int(account.get("id") or 0)
            if not account_id:
                continue
            if not int(account.get("collect_enabled") or 0) or self.is_running(account_id):
                fails.pop(account_id, None)
                retry_at.pop(account_id, None)
                continue
            if now < retry_at.get(account_id, 0.0):
                continue
            try:
                await self.start_account(account_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                n = fails.get(account_id, 0) + 1
                fails[account_id] = n
                delay = min(WATCHDOG_SECONDS * (2 ** min(n, 6)), WATCHDOG_MAX_BACKOFF)
                retry_at[account_id] = time.monotonic() + delay
                if n == 1:
                    # 只有第一次写状态：后面多半是同一个原因，
                    # 反复覆盖会把「代理已删除」这类更具体的信息冲掉
                    self.mark_error(account_id, str(e))
                    log.warning(
                        "看门狗：账号 %s 采集没在跑，拉起失败（%s 秒后重试）：%s",
                        account_id, int(delay), e,
                    )
                else:
                    log.debug("看门狗：账号 %s 第 %s 次拉起失败：%s", account_id, n, e)
            else:
                n = fails.pop(account_id, 0)
                retry_at.pop(account_id, None)
                worker = self._workers.get(account_id)
                if worker is not None:
                    worker.note("采集意外停止了，看门狗已自动拉起")
                log.warning(
                    "看门狗：账号 %s 采集已自动拉起（此前连续失败 %s 次）",
                    account_id, n,
                )

    # ---------- 内部 ----------
    async def _ensure_queue(self) -> None:
        if not self.queue.running:
            await self.queue.start()

    async def _stop_queue_if_idle(self) -> None:
        if not self.running and self.queue.running:
            await self.queue.stop()

    def _worker_for(self, account: dict) -> Collector:
        account_id = int(account["id"])
        worker = self._workers.get(account_id)
        if worker is None:
            session = self.session_for(account)
            worker = Collector(account, session, self.store, self.cfg, self.queue)
            self._workers[account_id] = worker
        else:
            worker.account = account
        return worker

    def mark_error(self, account_id: int, error: str) -> None:
        self.store.set_json(L.status_key(account_id), {
            "state": "error",
            "account_id": int(account_id),
            "error": error,
            "stopped_at": iso(utcnow()),
        })
