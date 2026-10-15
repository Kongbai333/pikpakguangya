"""Telegram Bot API 私聊控制：查询、搜索并创建光鸭秒传任务。

Bot Token 和用户 ID 存在本机数据库。运行时不走 Telethon，也不接收群消息；
通过 getUpdates 长轮询处理配置用户的私聊命令和 PikPak 分享链接，并主动推送任务进度。
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app import transfer as transfer_module
from app import integrations as integrations_module
from app.guangya_login import GuangyaAuth
from app.listener import process_message
from app.parser import CodeMerger, extract_links
from app.store import STATUS_LABELS, Store, iso, utcnow

log = logging.getLogger("telegram_bot")

BOT_SETTINGS_KEY = "telegram.bot"
BOT_OFFSET_KEY = "telegram.bot.offset"
BOT_LAST_SEEN_KEY = "telegram.bot.last_seen_at"
BOT_NOTIFY_STATE_KEY = "telegram.bot.notify_state"
TELEGRAM_API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_SECONDS = 25
SEARCH_PAGE_SIZE = 5
SEARCH_SESSION_LIMIT = 30
PROGRESS_BATCH_SIZE = 10
PROGRESS_MAX_SECONDS = 5.0
PROGRESS_TICK_SECONDS = 1.0
BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s\"']+", re.I)

BOT_COMMANDS = (
    {"command": "tasks", "description": "秒传任务 · 可暂停 / 终止 / 重试"},
    {"command": "search", "description": "搜索链接 · 结果可直接秒传"},
    {"command": "links", "description": "链接库统计"},
    {"command": "accounts", "description": "账号与采集开关"},
    {"command": "system", "description": "运行状态"},
    {"command": "cleanup", "description": "清理失效链接"},
    {"command": "help", "description": "用法说明"},
)
CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩"

TRANSFER_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "paused": "已暂停",
    "finished": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
ACTIVE_JOB_STATUSES = ("queued", "running", "paused")
TERMINAL_JOB_STATUSES = ("finished", "failed", "canceled")


class _BotTokenLogFilter(logging.Filter):
    """httpx 会把请求 URL 写进日志，只遮掉 Telegram Bot 路径里的 Token。"""

    @staticmethod
    def _redact(value: Any) -> Any:
        text = str(value)
        redacted = BOT_URL_RE.sub(r"\1***", text)
        return redacted if redacted != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = BOT_URL_RE.sub(r"\1***", record.msg)
        if isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(self._redact(v) for v in record.args)
        return True


def _install_bot_token_log_filter() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _BotTokenLogFilter) for f in logger.filters):
        logger.addFilter(_BotTokenLogFilter())


_install_bot_token_log_filter()


def resolve_bot_settings(store: Store) -> dict:
    """读取机器人配置；无效或缺失的字段统一返回空值。"""
    data = store.get_json(BOT_SETTINGS_KEY) or {}
    token = str(data.get("token") or "").strip()
    try:
        user_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    return {
        "token": token,
        "user_id": user_id,
    }


def save_bot_settings(store: Store, token: str, user_id: int) -> dict:
    """保存机器人配置。Token 留空表示沿用已保存的 Token。"""
    old = resolve_bot_settings(store)
    token = (token or "").strip() or old["token"]
    if not token:
        raise ValueError("请填写 Bot Token")
    if not BOT_TOKEN_RE.fullmatch(token):
        raise ValueError("Bot Token 格式不正确，应从 @BotFather 复制完整 Token")
    try:
        user_id = int(user_id or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("TG 用户 ID 必须是数字") from e
    if user_id <= 0:
        raise ValueError("请填写有效的 TG 用户 ID")
    store.set_json(BOT_SETTINGS_KEY, {
        "token": token,
        "user_id": user_id,
    })
    # 换 Bot 或换用户后，旧游标没有继续沿用的意义。
    if old["token"] != token or old["user_id"] != user_id:
        store.set_state(BOT_OFFSET_KEY, "0")
        store.set_json(BOT_NOTIFY_STATE_KEY, {})
    return {
        "token": token,
        "user_id": user_id,
    }


def _sender_name(user: dict) -> str:
    parts = [
        str(user.get("first_name") or "").strip(),
        str(user.get("last_name") or "").strip(),
    ]
    return " ".join(x for x in parts if x) or str(user.get("username") or "").strip()


def _fmt_bytes(value: Any) -> str:
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    if number < 1024:
        return f"{number} B"
    units = ("KB", "MB", "GB", "TB")
    size = float(number)
    unit = -1
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.2f} {units[unit]}"


def _target_folder(job: dict) -> str:
    path = str(job.get("target_folder_path") or "").strip()
    if path and path != "/":
        return path
    folder_id = str(job.get("target_folder_id") or "").strip()
    return folder_id or "根目录"


class TelegramBotRunner:
    """后台长轮询器。配置保存后由设置接口主动重启。"""

    def __init__(self, store: Store, cfg) -> None:
        self.store = store
        self.cfg = cfg
        self.proxy_resolver: Callable[[], Any] | None = None
        self.transfer_manager = None
        self.accounts_provider: Callable[[], list[dict]] | None = None
        self.account_session_for: Callable[[dict | None], Any] | None = None
        # 采集开关：由 Web 层注入（WebContext.set_collect），和网页端同一套逻辑
        self.collect_control: Callable[[int, bool], Any] | None = None
        self._task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._job_events: asyncio.Queue[tuple[int, dict]] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_error = ""
        self._last_update_at = ""
        self._ignored_senders: set[int] = set()
        self._search_sessions: dict[str, dict] = {}
        self._suppressed_jobs: set[int] = set()
        self._pending_account_actions: dict[int, dict] = {}

    def status(self) -> dict:
        conf = resolve_bot_settings(self.store)
        return {
            "token_set": bool(conf["token"]),
            "user_id": conf["user_id"],
            "running": bool(self._task and not self._task.done()),
            "error": self._last_error,
            "last_update_at": self._last_update_at,
            "last_seen_at": self.store.get_state(BOT_LAST_SEEN_KEY, "") or "",
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        conf = resolve_bot_settings(self.store)
        if not conf["token"] or conf["user_id"] <= 0:
            return
        self._last_error = ""
        self._stop.clear()
        self._job_events = asyncio.Queue()
        loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="telegram-bot")
        self._event_task = asyncio.create_task(
            self._job_event_loop(), name="telegram-bot-events"
        )
        self._prime_job_events()
        if self.transfer_manager is not None:
            self.transfer_manager.set_progress_callback(
                lambda job_id, event: loop.call_soon_threadsafe(
                    self._queue_job_event, int(job_id), dict(event or {})
                )
            )
        log.info("Telegram Bot 已启动：只接收用户 %s 的私聊", conf["user_id"])

    async def stop(self) -> None:
        self._stop.set()
        if self.transfer_manager is not None:
            self.transfer_manager.set_progress_callback(None)
        tasks = [self._task, self._event_task]
        self._task = None
        self._event_task = None
        self._job_events = None
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def restart(self) -> None:
        async with self._lock:
            await self.stop()
            await self.start()

    def _proxy_url(self) -> str | None:
        """机器人沿用当前账号的直接代理，避免国内环境直连 Bot API 失败。"""
        if self.proxy_resolver is None:
            return None
        try:
            proxy = self.proxy_resolver()
        except Exception:
            log.warning("解析 Telegram Bot 代理失败，将尝试直连", exc_info=True)
            return None
        if proxy is None:
            return None
        try:
            return proxy.httpx_proxy()
        except Exception:
            log.warning("生成 Telegram Bot 代理地址失败，将尝试直连", exc_info=True)
            return None

    @staticmethod
    def _offset(store: Store) -> int:
        try:
            return int(store.get_state(BOT_OFFSET_KEY, "0") or 0)
        except ValueError:
            return 0

    async def _run(self) -> None:
        merger = CodeMerger(int(self.cfg.collector.merge_window_seconds or 0))
        while not self._stop.is_set():
            conf = resolve_bot_settings(self.store)
            if not conf["token"] or conf["user_id"] <= 0:
                self._last_error = ""
                return

            timeout = httpx.Timeout(
                POLL_TIMEOUT_SECONDS + 10,
                connect=15.0,
                read=POLL_TIMEOUT_SECONDS + 10,
                write=10.0,
                pool=10.0,
            )
            try:
                async with httpx.AsyncClient(
                    proxy=self._proxy_url(), timeout=timeout
                ) as client:
                    with contextlib.suppress(Exception):
                        await self._api_post(
                            client, conf, "setMyCommands",
                            {"commands": list(BOT_COMMANDS)},
                        )
                    while not self._stop.is_set():
                        current = resolve_bot_settings(self.store)
                        if current != conf:
                            merger.clear()
                            break

                        params: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
                        offset = self._offset(self.store)
                        if offset > 0:
                            params["offset"] = offset
                        url = f"{TELEGRAM_API_BASE}/bot{conf['token']}/getUpdates"
                        response = await client.get(url, params=params)
                        try:
                            data = response.json()
                        except ValueError as e:
                            raise RuntimeError(
                                f"Telegram Bot API 返回了非 JSON（HTTP {response.status_code}）"
                            ) from e
                        if not response.is_success or not data.get("ok"):
                            detail = data.get("description") or response.text[:200]
                            if response.status_code == 409:
                                detail = "Bot Token 正在被 Webhook 或另一个 getUpdates 占用"
                            raise RuntimeError(f"Telegram Bot API 错误：{detail}")

                        self._last_error = ""
                        for update in data.get("result") or []:
                            if not isinstance(update, dict):
                                continue
                            await self._handle_update(update, conf, merger, client)
                            try:
                                update_id = int(update.get("update_id") or 0)
                            except (TypeError, ValueError):
                                update_id = 0
                            if update_id > 0:
                                self.store.set_state(BOT_OFFSET_KEY, str(update_id + 1))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                detail = str(e).replace(conf["token"], "***")
                self._last_error = detail
                log.warning("Telegram Bot 轮询失败：%s", detail)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5)

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return "", ""
        parts = stripped.split(maxsplit=1)
        command = parts[0][1:].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        return command, argument

    @staticmethod
    def _help_text() -> str:
        return (
            "📖 <b>用法说明</b>\n"
            "━━━━━━━━━━━━━━\n"
            "<b>直接发消息就能用</b>\n"
            "• 发 PikPak 分享链接 → 自动加入秒传队列\n"
            "• 提取码可以另发一条（4 位码）→ 自动补到上一条链接\n"
            "• 一条消息里放多个链接 → 一起处理\n"
            "• 链接会自动入库，不用先手动收藏\n\n"
            "<b>命令</b>\n"
            "<code>/tasks</code>　　秒传任务（可暂停 / 终止 / 重试）\n"
            "<code>/search</code>　 搜索链接，结果可直接秒传\n"
            "<code>/links</code>　　链接库统计，点分类看具体链接\n"
            "<code>/accounts</code> 账号与采集开关\n"
            "<code>/system</code>　 运行状态\n"
            "<code>/cleanup</code>　清理失效链接\n"
            "<code>/cancel</code>　 退出当前操作"
        )

    def _tasks_text(self) -> str:
        """秒传任务列表。

        只列出来，不带操作按钮 —— 10 个任务就是 10 行按钮，在手机上把消息撑得
        很长，而且每个任务的可选操作还不一样，扫一眼根本对不上号。
        要暂停 / 重试请到网页的「秒传」页，那里有完整的任务控制。
        """
        jobs = self.store.list_transfer_jobs(limit=10)
        if not jobs:
            return "暂无秒传任务。\n\n把 PikPak 分享链接发给我就能建一个。"

        counts = self.store.transfer_job_counts()
        running_count = sum(counts.get(status, 0) for status in ACTIVE_JOB_STATUSES)
        handled_count = sum(counts.get(status, 0) for status in TERMINAL_JOB_STATUSES)
        lines = [
            "📋 <b>秒传任务</b>",
            "━━━━━━━━━━━━━━",
            f"🔄 进行中：<b>{running_count}</b>　✅ 已处理：<b>{handled_count}</b>",
            "",
        ]
        for index, job in enumerate(jobs, start=1):
            status = str(job.get("status") or "")
            label = TRANSFER_STATUS_LABELS.get(status, status or "未知")
            total = int(job.get("total_files") or 0)
            done = sum(
                int(job.get(key) or 0)
                for key in ("completed_files", "failed_files", "skipped_files")
            )
            progress = float(job.get("progress") or 0)
            name = str(job.get("title") or job.get("share_url") or f"任务 #{job.get('id')}")
            name = " ".join(name.split())[:100]
            number = CIRCLED_NUMBERS[index - 1] if index <= len(CIRCLED_NUMBERS) else str(index)
            summary = (
                f"{number} <b>{html.escape(name)}</b>\n"
                f"   <code>#{job.get('id')}</code> · {html.escape(label)} · {progress:.1f}%"
            )
            if total:
                summary += f" · 已处理 {done}/{total}"
            lines.append(summary)

            message = " ".join(str(job.get("message") or "").split())
            if message and status in ("failed", "canceled"):
                lines.append(f"   原因：{html.escape(message[:140])}")
            lines.append("")

        lines.append("要暂停 / 重试请到网页的「秒传」页。")
        return "\n".join(lines)

    # 状态分类 → 展示用的一行。按钮和数字都从这里来，保证「显示多少条」
    # 和「点进去看到多少条」永远一致。
    LINK_STATUS_ROWS: tuple[tuple[str, str], ...] = (
        ("OK", "✅ 有效"),
        ("PASS_CODE_EMPTY", "🔐 需提取码"),
        ("PASS_CODE_ERROR", "⚠️ 提取码错误"),
        ("NOT_FOUND", "❌ 已失效"),
        ("DELETED", "🗑 已删除"),
        ("EXPIRED", "⌛ 已过期"),
        ("SENSITIVE_RESOURCE", "🚫 违规资源"),
        ("ERROR", "❗ 检测出错"),
        ("unchecked", "⏳ 未检测"),
    )

    def _links_view(self) -> tuple[str, dict | None]:
        """链接库统计。数字不是死的 —— 有内容的分类点一下就能看具体链接。"""
        stats = self.store.stats()
        counts = self.store.check_stats()
        lines = [
            "🔗 <b>链接情况</b>",
            "━━━━━━━━━━━━━━",
            f"📚 总链接　　<b>{int(stats.get('shares') or 0)}</b>",
        ]
        buttons: list[dict] = []
        for status, label in self.LINK_STATUS_ROWS:
            count = int(counts.get(status) or 0)
            lines.append(f"{label}　<b>{count}</b>")
            if count:
                buttons.append({
                    "text": f"{label} {count}",
                    "callback_data": f"st:{status}",
                })
        lines.append(f"🔑 带提取码　<b>{int(stats.get('with_code') or 0)}</b>")

        last_checked = str(stats.get("last_checked_at") or "").strip()
        if last_checked:
            lines.extend(["", f"🕒 最近检测：{html.escape(last_checked)}"])
        sources = self.store.source_stats()
        if sources:
            total_sources = sum(int(item.get("total") or 0) for item in sources)
            lines.extend(["", "📌 <b>来源构成</b>"])
            for item in sources[:10]:
                name = str(item.get("source") or "(未知来源)")
                count = int(item.get("total") or 0)
                percent = count / total_sources * 100 if total_sources else 0
                lines.append(
                    f"• {html.escape(name)}：<b>{count}</b> 条（{percent:.1f}%）"
                )

        keyboard = None
        if buttons:
            lines.extend(["", "点下面的分类可以直接看链接。"])
            # 一行两个：按钮文字本来就带数字，挤一行会被截断
            keyboard = {
                "inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            }
        return "\n".join(lines), keyboard

    def _accounts(self) -> list[dict]:
        if self.accounts_provider is not None:
            try:
                return list(self.accounts_provider() or [])
            except Exception:
                log.warning("读取账号状态失败", exc_info=True)
        return self.store.list_accounts()

    @staticmethod
    def _account_name(account: dict) -> str:
        name = (
            account.get("alias")
            or account.get("name")
            or account.get("phone")
            or account.get("username")
        )
        return str(name or f"账号 #{account.get('id')}")

    @staticmethod
    def _account_state_text(account: dict) -> str:
        if account.get("collecting"):
            return "采集中"
        if account.get("collect_enabled"):
            return "已开启未运行"
        if account.get("logged_in") or account.get("session_exists") or account.get("user_id"):
            return "在线待命"
        return "未登录"

    def _system_status_text(self) -> str:
        conf = resolve_bot_settings(self.store)
        guangya = GuangyaAuth(self.store).status()
        pikpak = integrations_module._pikpak_status(self.store)
        accounts = self._accounts()
        collecting = sum(1 for account in accounts if account.get("collecting"))
        enabled = sum(1 for account in accounts if account.get("collect_enabled"))
        logged = sum(
            1 for account in accounts
            if account.get("logged_in") or account.get("session_exists")
        )
        lines = [
            "🖥️ <b>系统情况</b>",
            "━━━━━━━━━━━━━━",
            f"🤖 Telegram Bot：<b>{'运行中' if self._task and not self._task.done() else '未运行'}</b>",
            f"👤 接收用户：<code>{conf['user_id'] or '未配置'}</code>",
            f"☁️ 光鸭：<b>{'已登录' if guangya.get('connected') else '未登录'}</b>",
            f"📦 PikPak：<b>{'已登录' if pikpak.get('connected') else '未登录'}</b>",
            f"📡 采集：<b>{collecting} 个运行</b> · {enabled} 个已开启",
            f"👥 TG 账号：{logged}/{len(accounts)} 在线",
        ]
        if accounts:
            lines.extend(["", "账号状态："])
            for account in accounts[:10]:
                lines.append(
                    f"• {html.escape(self._account_name(account))}："
                    f"{html.escape(self._account_state_text(account))}"
                )
        return "\n".join(lines)

    def _cleanup_status_counts(self) -> dict[str, int]:
        keep = {"unchecked", "", "OK", "PASS_CODE_EMPTY"}
        return {
            status: count
            for status, count in self.store.check_stats().items()
            if status not in keep
        }

    def _cleanup_text(self) -> str:
        counts = self._cleanup_status_counts()
        total = sum(counts.values())
        if not total:
            return "🧹 <b>链接清理</b>\n\n当前没有需要清理的链接。"
        lines = [
            "🧹 <b>链接清理</b>",
            "━━━━━━━━━━━━━━",
            f"预计清理：<b>{total}</b> 条",
            "将保留：未检测、有效、需提取码",
            "",
            "清理明细：",
        ]
        for status, count in sorted(counts.items(), key=lambda item: -item[1]):
            label = STATUS_LABELS.get(status, status)
            lines.append(f"• {html.escape(label)}：{count} 条")
        lines.append("\n此操作不可恢复，是否继续？")
        return "\n".join(lines)

    @staticmethod
    def _cleanup_keyboard() -> dict:
        return {
            "inline_keyboard": [[
                {"text": "确认清理", "callback_data": "cleanup:confirm"},
                {"text": "取消", "callback_data": "cleanup:cancel"},
            ]]
        }

    def _cleanup_links_sync(self) -> dict:
        counts = self._cleanup_status_counts()
        ids: list[int] = []
        for status in counts:
            page = 1
            while True:
                result = self.store.query_admin(
                    status=status, page=page, size=500,
                    sort="id", direction="asc",
                )
                items = result.get("items") or []
                ids.extend(int(item["ID"]) for item in items)
                total = int(result.get("total") or 0)
                if not items or page * 500 >= total:
                    break
                page += 1
        deleted = 0
        for start in range(0, len(ids), 500):
            deleted += self.store.delete_shares(ids[start:start + 500])
        return {"deleted": deleted, "counts": counts}

    def _cleanup_result_text(self, result: dict) -> str:
        lines = [
            "🧹 <b>链接清理完成</b>",
            "━━━━━━━━━━━━━━",
            f"已清理：<b>{int(result.get('deleted') or 0)}</b> 条",
        ]
        counts = result.get("counts") or {}
        if counts:
            lines.append("")
            for status, count in sorted(counts.items(), key=lambda item: -item[1]):
                label = STATUS_LABELS.get(status, status)
                lines.append(f"• {html.escape(label)}：{count} 条")
        return "\n".join(lines)

    def _accounts_text(self) -> str:
        accounts = self._accounts()
        if not accounts:
            return "👤 <b>账号管理</b>\n\n当前没有 TG 账号。"
        lines = ["👤 <b>账号管理</b>", "━━━━━━━━━━━━━━", f"共 {len(accounts)} 个账号"]
        for account in accounts:
            lines.append(
                f"• {html.escape(self._account_name(account))}："
                f"{html.escape(self._account_state_text(account))}"
            )
        return "\n".join(lines)

    def _accounts_keyboard(self) -> dict:
        rows = []
        for account in self._accounts():
            account_id = int(account.get("id") or 0)
            if not account_id:
                continue
            label = f"{self._account_name(account)} · {self._account_state_text(account)}"
            rows.append([{
                "text": label[:60],
                "callback_data": f"acct:menu:{account_id}",
            }])
        return {"inline_keyboard": rows}

    def _account_menu_text(self, account: dict) -> str:
        account_id = int(account.get("id") or 0)
        worker_state = ""
        for item in self._accounts():
            if int(item.get("id") or 0) == account_id:
                worker_state = self._account_state_text(item)
                break
        lines = [
            "👤 <b>账号管理</b>",
            "━━━━━━━━━━━━━━",
            f"账号：<b>{html.escape(self._account_name(account))}</b>",
            f"状态：{html.escape(worker_state or '未登录')}",
            f"手机号：<code>{html.escape(str(account.get('phone') or '未设置'))}</code>",
            "",
            "请选择操作：",
        ]
        return "\n".join(lines)

    def _account_menu_keyboard(self, account_id: int) -> dict:
        account = self.store.get_account(account_id) or {}
        collecting = False
        for item in self._accounts():
            if int(item.get("id") or 0) == int(account_id):
                collecting = bool(item.get("collecting"))
                break
        on = collecting or bool(account.get("collect_enabled"))
        toggle = "⏹ 关闭采集" if on else "▶️ 打开采集"
        return {
            "inline_keyboard": [
                [{"text": toggle, "callback_data": f"acct:collect:{account_id}"}],
                [
                    {"text": "添加群组", "callback_data": f"acct:add:{account_id}"},
                    {"text": "群组采集", "callback_data": f"acct:groups:{account_id}"},
                ],
                [{"text": "返回账号列表", "callback_data": "acct:list"}],
            ]
        }

    def _account_groups_page(self, account_id: int, page: int = 1) -> tuple[str, dict]:
        account = self.store.get_account(account_id)
        if not account:
            return "账号不存在。", {"inline_keyboard": [[
                {"text": "返回账号列表", "callback_data": "acct:list"},
            ]]}
        watched = self.store.list_watched(account_id=account_id)
        watched.sort(key=lambda row: (not bool(row.get("enabled")), row.get("title") or ""))
        page_size = 8
        total_pages = max(1, (len(watched) + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), total_pages))
        chunk = watched[(page - 1) * page_size:page * page_size]
        lines = [
            "📡 <b>群组采集</b>",
            "━━━━━━━━━━━━━━",
            f"账号：<b>{html.escape(self._account_name(account))}</b>",
            f"已开启：{sum(1 for row in watched if row.get('enabled'))}/{len(watched)}",
            f"第 {page}/{total_pages} 页",
        ]
        if not watched:
            lines.append("")
            lines.append("还没有群组，请先使用「添加群组」。")
        rows: list[list[dict]] = []
        for row in chunk:
            enabled = bool(row.get("enabled"))
            title = str(row.get("title") or row.get("chat_id") or "未命名群组")
            rows.append([{
                "text": f"{'✅' if enabled else '⭕'} {title}"[:60],
                "callback_data": (
                    f"acct:t:{account_id}:{int(row['chat_id'])}:{page}"
                ),
            }])
        nav: list[dict] = []
        if page > 1:
            nav.append({"text": "上一页", "callback_data": f"acct:p:{account_id}:{page - 1}"})
        if page < total_pages:
            nav.append({"text": "下一页", "callback_data": f"acct:p:{account_id}:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "返回账号", "callback_data": f"acct:menu:{account_id}"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    async def _handle_account_callback(self, callback_id: str, data: str,
                                        sender_id: int, chat_id: int,
                                        message_id: int, conf: dict,
                                        client: httpx.AsyncClient) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        text = ""
        keyboard = None
        answered = False
        if action in ("list", "menu"):
            self._pending_account_actions.pop(sender_id, None)

        if action == "list":
            text = self._accounts_text()
            keyboard = self._accounts_keyboard()
        elif action == "menu" and len(parts) >= 3:
            account = self.store.get_account(int(parts[2]))
            if account:
                text = self._account_menu_text(account)
                keyboard = self._account_menu_keyboard(int(account["id"]))
            else:
                text = "账号不存在。"
                keyboard = {"inline_keyboard": [[
                    {"text": "返回账号列表", "callback_data": "acct:list"},
                ]]}
        elif action == "add" and len(parts) >= 3:
            account = self.store.get_account(int(parts[2]))
            if account:
                self._pending_account_actions[sender_id] = {"account_id": int(account["id"])}
                text = (
                    "➕ <b>添加群组</b>\n"
                    "━━━━━━━━━━━━━━\n"
                    f"账号：{html.escape(self._account_name(account))}\n\n"
                    "请发送群组 <code>@username</code> 或 <code>t.me</code> 链接。\n"
                    "发送「取消」或 /cancel 可退出。"
                )
                keyboard = {"inline_keyboard": [[
                    {"text": "取消添加", "callback_data": f"acct:menu:{account['id']}"},
                ]]}
            else:
                text = "账号不存在。"
                keyboard = {"inline_keyboard": [[
                    {"text": "返回账号列表", "callback_data": "acct:list"},
                ]]}
        elif action == "collect" and len(parts) >= 3:
            account_id = int(parts[2])
            account = self.store.get_account(account_id)
            if not account:
                text = "账号不存在。"
                keyboard = {"inline_keyboard": [[
                    {"text": "返回账号列表", "callback_data": "acct:list"},
                ]]}
            elif self.collect_control is None:
                await self._answer_callback(client, conf, callback_id, "采集服务未启动")
                return
            else:
                # 以「当前实际在跑」为准判断要切到哪边，别只看数据库里的开关
                running = any(
                    int(item.get("id") or 0) == account_id and item.get("collecting")
                    for item in self._accounts()
                )
                want = not (running or bool(account.get("collect_enabled")))
                try:
                    await self.collect_control(account_id, want)
                except Exception as e:
                    await self._answer_callback(
                        client, conf, callback_id, f"操作失败：{str(e)[:150]}"
                    )
                    return
                account = self.store.get_account(account_id) or account
                text = self._account_menu_text(account)
                keyboard = self._account_menu_keyboard(account_id)
                answered = True
                await self._answer_callback(
                    client, conf, callback_id, "已打开采集" if want else "已关闭采集"
                )
        elif action in ("groups", "p") and len(parts) >= 3:
            account_id = int(parts[2])
            try:
                page = int(parts[3]) if action == "p" and len(parts) >= 4 else 1
            except (TypeError, ValueError):
                page = 1
            text, keyboard = self._account_groups_page(account_id, page)
        elif action == "t" and len(parts) >= 4:
            account_id = int(parts[2])
            chat_id_target = int(parts[3])
            rows = self.store.list_watched(account_id=account_id)
            row = next((item for item in rows if int(item["chat_id"]) == chat_id_target), None)
            if row:
                enabled = not bool(row.get("enabled"))
                if enabled:
                    self.store.add_watched(
                        chat_id_target,
                        title=row.get("title") or "",
                        username=row.get("username") or "",
                        kind=row.get("kind") or "",
                        enabled=True,
                        account_id=account_id,
                    )
                else:
                    self.store.set_watched(chat_id_target, False, account_id=account_id)
                try:
                    page = int(parts[4]) if len(parts) > 4 else 1
                except (TypeError, ValueError):
                    page = 1
                text, keyboard = self._account_groups_page(account_id, page)
                answered = True
                await self._answer_callback(
                    client, conf, callback_id, "已开启采集" if enabled else "已关闭采集"
                )
            else:
                await self._answer_callback(client, conf, callback_id, "群组不存在")
                return
        else:
            await self._answer_callback(client, conf, callback_id, "不支持的操作")
            return

        if text:
            await self._refresh_panel(client, conf, chat_id, message_id, text, keyboard)
        # 上面某些分支已经答过回调了，重复回答会被 Telegram 拒掉
        if action != "t" and not answered:
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id)

    async def _finish_add_group(self, action: dict, target: str, user_id: int,
                                conf: dict, client: httpx.AsyncClient) -> None:
        account_id = int(action.get("account_id") or 0)
        account = self.store.get_account(account_id)
        if not account:
            await self._send_message(client, conf, user_id, "账号不存在。")
            return
        if self.account_session_for is None:
            await self._send_message(client, conf, user_id, "账号会话服务未启动。")
            return
        try:
            session = self.account_session_for(account)
            result = await session.join(target)
        except Exception as e:
            await self._send_message(
                client, conf, user_id, f"添加群组失败：{e}"
            )
            return
        if result.get("joined") and result.get("chat_id"):
            self.store.add_watched(
                int(result["chat_id"]),
                title=result.get("title") or "",
                username=result.get("username") or "",
                kind="群",
                enabled=True,
                account_id=account_id,
            )
            await self._send_message(
                client, conf, user_id,
                f"✅ 已加入「{result.get('title') or target}」，并开启该群采集。",
            )
            return
        if result.get("pending_approval"):
            await self._send_message(
                client, conf, user_id,
                "已提交入群申请，等管理员批准后请在账号页确认。",
            )
            return
        await self._send_message(
            client, conf, user_id, f"未加入群组：{result.get('message') or target}"
        )

    def _search_page(self, keyword: str, page: int = 1,
                     status: str = "") -> tuple[str, int, int, list[dict]]:
        """搜一页。返回 (文本, 当前页, 总页数, 这一页的条目)。

        把条目也返回出去，是为了给每条生成「秒传 / 复制」按钮 —— 只返回文本的话
        调用方还得再查一次库才能拿到 ID。

        status 非空时按校验状态筛选（关键词可以为空）—— `/links` 里的数字点进来
        走的就是这条路。
        """
        keyword = " ".join(keyword.split())
        if not keyword and not status:
            return "用法：/search 关键词", 1, 1, []

        result = self.store.query_admin(
            q=keyword, page=1, size=SEARCH_PAGE_SIZE, status=status
        )
        total = int(result.get("total") or 0)
        total_pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
        page = max(1, min(int(page or 1), total_pages))
        result = self.store.query_admin(
            q=keyword, page=page, size=SEARCH_PAGE_SIZE, status=status
        )
        items = result.get("items") or []
        scope = STATUS_LABELS.get(status, status) if status else f"「{keyword}」"
        if not items:
            return (
                f"🔎 <b>链接列表</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"{html.escape(scope)}：没有匹配的链接。"
            ), page, total_pages, []

        lines = [
            "🔎 <b>链接列表</b>",
            "━━━━━━━━━━━━━━",
            f"筛选：{html.escape(scope)}",
            f"共 <b>{total}</b> 条 · 第 <b>{page}/{total_pages}</b> 页",
            "",
        ]
        for index, item in enumerate(items, start=1):
            title = item.get("TITLE") or item.get("MSG") or item.get("LINK") or ""
            title = " ".join(str(title).split())[:100]
            item_status = STATUS_LABELS.get(str(item.get("STATUS") or ""), "未检测")
            hidden = " · 已隐藏" if item.get("HIDDEN") else ""
            number = CIRCLED_NUMBERS[index - 1] if index <= len(CIRCLED_NUMBERS) else str(index)
            lines.append(
                f"{number} <b>#{item.get('ID')} [{html.escape(item_status)}]</b>\n"
                f"{html.escape(title)}{html.escape(hidden)}\n"
                f"提取码：<code>{html.escape(str(item.get('CODE') or '无'))}</code>\n"
                f"{html.escape(str(item.get('LINK') or ''))}\n"
            )
        return "\n".join(lines), page, total_pages, items

    def _search_keyboard(self, session_id: str, items: list[dict],
                         page: int, total_pages: int) -> dict | None:
        """结果操作 + 翻页。没有按钮可给时返回 None，让调用方去掉旧键盘。"""
        rows: list[list[dict]] = []
        for item in items:
            share_id = int(item.get("ID") or 0)
            if not share_id:
                continue
            rows.append([
                {"text": "⬇️ 秒传", "callback_data": f"res:tr:{share_id}"},
                {"text": "📋 复制", "callback_data": f"res:cp:{share_id}"},
            ])
        if total_pages > 1:
            def nav(text: str, target: str) -> dict:
                return {"text": text, "callback_data": f"search:{session_id}:{target}"}

            rows.append([
                nav("⏮", "1"),
                nav("◀️", "prev"),
                nav(f"{page}/{total_pages}", str(page)),
                nav("▶️", "next"),
                nav("⏭", "last"),
            ])
        return {"inline_keyboard": rows} if rows else None

    def _new_search_session(self, user_id: int, keyword: str,
                            status: str = "") -> str:
        while len(self._search_sessions) >= SEARCH_SESSION_LIMIT:
            self._search_sessions.pop(next(iter(self._search_sessions)))
        session_id = secrets.token_urlsafe(8)
        self._search_sessions[session_id] = {
            "user_id": int(user_id),
            "keyword": keyword,
            "status": status,
            "page": 1,
        }
        return session_id

    def _command_reply(self, command: str, argument: str,
                       user_id: int) -> tuple[str, dict | None]:
        if command in ("start", "help"):
            return self._help_text(), None
        if command == "cancel":
            # 没有待办操作时给个明确回复，别掉进「未知命令」
            return "没有正在进行的操作。", None
        if command in ("tasks", "task"):
            return self._tasks_text(), None
        if command in ("cleanup", "clean"):
            counts = self._cleanup_status_counts()
            keyboard = self._cleanup_keyboard() if counts else None
            return self._cleanup_text(), keyboard
        if command == "system":
            return self._system_status_text(), None
        if command in ("status", "links"):
            return self._links_view()
        if command in ("accounts", "account"):
            keyboard = self._accounts_keyboard()
            if not keyboard.get("inline_keyboard"):
                keyboard = None
            return self._accounts_text(), keyboard
        if command in ("search", "s"):
            keyword = " ".join(argument.split())
            if not keyword:
                return "用法：<code>/search 关键词</code>", None
            session_id = self._new_search_session(user_id, keyword)
            text, page, total_pages, items = self._search_page(keyword, 1)
            return text, self._search_keyboard(session_id, items, page, total_pages)
        return f"未知命令：<code>/{html.escape(command)}</code>\n\n{self._help_text()}", None

    async def _api_post(self, client: httpx.AsyncClient, conf: dict,
                        method: str, payload: dict) -> dict:
        url = f"{TELEGRAM_API_BASE}/bot{conf['token']}/{method}"
        response = await client.post(url, json=payload)
        try:
            data = response.json()
        except ValueError as e:
            raise RuntimeError(
                f"Telegram Bot API 返回了非 JSON（HTTP {response.status_code}）"
            ) from e
        if not response.is_success or not data.get("ok"):
            detail = data.get("description") or response.text[:200]
            raise RuntimeError(f"Telegram Bot API 错误：{detail}")
        return data.get("result") or {}

    async def _send_message(self, client: httpx.AsyncClient, conf: dict,
                            chat_id: int, text: str,
                            reply_markup: dict | None = None,
                            parse_mode: str | None = None) -> int:
        text = text.strip()
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        payload = {
            "chat_id": int(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = await self._api_post(client, conf, "sendMessage", payload)
        return int(result.get("message_id") or 0)

    async def _edit_message(self, client: httpx.AsyncClient, conf: dict,
                            chat_id: int, message_id: int, text: str,
                            reply_markup: dict | None = None,
                            parse_mode: str | None = None) -> bool:
        text = text.strip()
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        payload = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            await self._api_post(client, conf, "editMessageText", payload)
            return True
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return True
            log.warning(
                "Telegram Bot 编辑消息失败：%s",
                str(e).replace(conf["token"], "***"),
            )
            return False

    async def _answer_callback(self, client: httpx.AsyncClient, conf: dict,
                               callback_id: str, text: str = "") -> None:
        if not callback_id:
            return
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self._api_post(client, conf, "answerCallbackQuery", payload)

    async def _handle_update(self, update: dict, conf: dict, merger: CodeMerger,
                             client: httpx.AsyncClient) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message, conf, merger, client)

        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback, conf, client)

    async def _handle_message(self, message: dict, conf: dict, merger: CodeMerger,
                              client: httpx.AsyncClient) -> None:

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return
        try:
            sender_id = int(sender.get("id") or 0)
        except (TypeError, ValueError):
            return
        if sender_id != int(conf["user_id"]):
            if sender_id and sender_id not in self._ignored_senders:
                self._ignored_senders.add(sender_id)
                log.warning("Bot 忽略非授权用户 %s 的私聊", sender_id)
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        self.store.set_state(BOT_LAST_SEEN_KEY, iso(utcnow()))
        pending_action = self._pending_account_actions.pop(sender_id, None)
        if pending_action:
            raw = text.strip()
            if raw.lower() in ("取消", "/cancel"):
                await self._send_message(client, conf, sender_id, "已取消添加群组。")
                return
            if raw.startswith("/"):
                self._pending_account_actions[sender_id] = pending_action
                await self._send_message(
                    client, conf, sender_id,
                    "当前正在添加群组，请发送群组链接，或发送「取消」退出。",
                )
                return
            await self._finish_add_group(
                pending_action, raw, sender_id, conf, client
            )
            return

        command, argument = self._parse_command(text)
        if command:
            log.debug("Bot 收到命令 user=%s command=/%s", sender_id, command)
            reply, reply_markup = self._command_reply(command, argument, sender_id)
            try:
                await self._send_message(
                    client, conf, sender_id, reply,
                    reply_markup=reply_markup, parse_mode="HTML",
                )
            except Exception as e:
                detail = str(e).replace(conf["token"], "***")
                log.warning("Bot 回复命令失败：%s", detail)
            return

        preview = " ".join(text.split())[:120]
        log.debug("Bot 收到文本 user=%s text=%s", sender_id, preview)

        raw_date = message.get("date")
        try:
            posted_at = datetime.fromtimestamp(int(raw_date), timezone.utc)
        except (TypeError, ValueError, OSError):
            posted_at = utcnow()

        has_link = bool(extract_links(text))
        if has_link and transfer_module.load_settings(self.store).get("check_used_links"):
            blocked = []
            for line in transfer_module.split_share_inputs(text):
                share_id, _ = transfer_module.parse_share_input(line)
                reason = transfer_module.used_link_reason(self.store, share_id)
                if reason:
                    blocked.append(reason)
            if blocked:
                await self._send_message(
                    client, conf, sender_id,
                    "该 PikPak 分享链接已使用，已拦截：\n" + "\n".join(blocked),
                )
                return

        result = process_message(
            self.store,
            merger,
            chat_id=int(chat.get("id") or sender_id),
            message_id=int(message.get("message_id") or 0),
            text=text,
            posted_at=iso(posted_at),
            chat_title=_sender_name(sender),
            sender_id=sender_id,
            sender_name=_sender_name(sender),
            include=self.cfg.collector.keywords_include,
            exclude=self.cfg.collector.keywords_exclude,
            ignore_senders=self.cfg.collector.ignore_senders,
        )
        if not result.get("saved") and not has_link:
            return

        if result.get("saved"):
            self._last_update_at = iso(utcnow())
            for saved in result["saved"]:
                if saved.get("update_only"):
                    log.debug("Bot 补码 %s -> %s", saved["link_id"], saved.get("pass_code"))
                elif saved.get("share_created"):
                    log.debug(
                        "Bot 新链接 %s 码=%s 备注=%s",
                        saved["link_id"],
                        saved.get("pass_code") or "无",
                        saved.get("remark") or "",
                    )

        if has_link:
            try:
                await self._enqueue_transfer_message(text, sender_id, conf, client)
            except Exception as e:
                detail = str(e).replace(conf["token"], "***")
                log.warning("Bot 创建秒传任务失败：%s", detail)
                with contextlib.suppress(Exception):
                    await self._send_message(
                        client, conf, sender_id, f"加入秒传队列失败：{detail}"
                    )

    async def _handle_callback(self, callback: dict, conf: dict,
                               client: httpx.AsyncClient) -> None:
        callback_id = str(callback.get("id") or "")
        sender = callback.get("from") or {}
        try:
            sender_id = int(sender.get("id") or 0)
        except (TypeError, ValueError):
            return
        if sender_id != int(conf["user_id"]):
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id, "无权操作")
            return

        self.store.set_state(BOT_LAST_SEEN_KEY, iso(utcnow()))
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or sender_id)
        message_id = int(message.get("message_id") or 0)
        if data.startswith("search:"):
            await self._handle_search_callback(
                callback_id, data, sender_id, chat_id, message_id, conf, client
            )
            return
        if data.startswith("cleanup:"):
            await self._handle_cleanup_callback(
                callback_id, data, chat_id, message_id, conf, client
            )
            return
        if data.startswith("acct:"):
            await self._handle_account_callback(
                callback_id, data, sender_id, chat_id, message_id, conf, client
            )
            return
        if data.startswith("res:"):
            await self._handle_result_callback(
                callback_id, data, sender_id, chat_id, conf, client
            )
            return
        if data.startswith("st:"):
            await self._handle_status_callback(
                callback_id, data, sender_id, chat_id, message_id, conf, client
            )
            return
        with contextlib.suppress(Exception):
            await self._answer_callback(client, conf, callback_id, "不支持的操作")

    async def _refresh_panel(self, client: httpx.AsyncClient, conf: dict,
                             chat_id: int, message_id: int,
                             text: str, keyboard: dict | None) -> None:
        """回调后刷新原消息；编辑失败（比如消息太旧）就补发一条。"""
        if message_id:
            edited = await self._edit_message(
                client, conf, chat_id, message_id, text,
                reply_markup=keyboard, parse_mode="HTML",
            )
            if edited:
                return
        await self._send_message(
            client, conf, chat_id, text, reply_markup=keyboard, parse_mode="HTML"
        )

    async def _handle_status_callback(self, callback_id: str, data: str,
                                      sender_id: int, chat_id: int,
                                      message_id: int, conf: dict,
                                      client: httpx.AsyncClient) -> None:
        """`/links` 里的分类按钮：按校验状态列出链接。"""
        status = data.split(":", 1)[1] if ":" in data else ""
        known = {code for code, _ in self.LINK_STATUS_ROWS}
        if status not in known:
            await self._answer_callback(client, conf, callback_id, "不支持的分类")
            return

        session_id = self._new_search_session(sender_id, "", status=status)
        text, page, total_pages, items = self._search_page("", 1, status)
        keyboard = self._search_keyboard(session_id, items, page, total_pages) or {
            "inline_keyboard": []
        }
        await self._refresh_panel(client, conf, chat_id, message_id, text, keyboard)
        with contextlib.suppress(Exception):
            await self._answer_callback(client, conf, callback_id, STATUS_LABELS.get(status, status))

    async def _handle_result_callback(self, callback_id: str, data: str,
                                      sender_id: int, chat_id: int,
                                      conf: dict, client: httpx.AsyncClient) -> None:
        """搜索结果上的按钮：直接秒传，或把链接+提取码发成可点复制的文本。"""
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        try:
            share_id = int(parts[2])
        except (IndexError, TypeError, ValueError):
            await self._answer_callback(client, conf, callback_id, "参数不对")
            return

        rows = self.store.shares_by_ids([share_id])
        if not rows:
            await self._answer_callback(
                client, conf, callback_id, "这条链接已经不在了（可能被清理过）"
            )
            return
        row = rows[0]
        code = str(row.get("pass_code") or "").strip()

        if action == "cp":
            # <code> 在 Telegram 里是「点一下复制」，比让用户手动划选好得多
            payload = row["url"] + (f"  提取码 {code}" if code else "")
            await self._send_message(
                client, conf, chat_id,
                f"<code>{html.escape(payload)}</code>",
                parse_mode="HTML",
            )
            await self._answer_callback(client, conf, callback_id, "已发送，点一下复制")
            return

        if action == "tr":
            line = str(row.get("url") or "")
            if code:
                # parse_share_input 靠「提取码」这个标签认码，别直接拼裸码
                line = f"{line} 提取码 {code}"
            await self._enqueue_transfer_message(line, sender_id, conf, client)
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id, "已加入秒传队列")
            return

        await self._answer_callback(client, conf, callback_id, "不支持的操作")

    async def _handle_search_callback(self, callback_id: str, data: str,
                                      sender_id: int, chat_id: int,
                                      message_id: int, conf: dict,
                                      client: httpx.AsyncClient) -> None:
        parts = data.split(":", 2)
        if len(parts) != 3:
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id, "不支持的操作")
            return

        session_id = parts[1]
        session = self._search_sessions.get(session_id)
        if not session or int(session.get("user_id") or 0) != sender_id:
            with contextlib.suppress(Exception):
                await self._answer_callback(
                    client, conf, callback_id, "分页已过期，请重新 /search"
                )
            return

        target = parts[2]
        current_page = int(session.get("page") or 1)
        if target == "prev":
            page = current_page - 1
        elif target == "next":
            page = current_page + 1
        elif target == "last":
            page = 10**9
        else:
            try:
                page = int(target)
            except ValueError:
                page = current_page

        text, page, total_pages, items = self._search_page(
            session["keyword"], page, session.get("status") or ""
        )
        session["page"] = page
        # 空键盘而不是 None：编辑消息时不传 reply_markup 会保留上一次的按钮，
        # 翻到没结果的一页就会留下点不动的旧键盘。
        reply_markup = self._search_keyboard(session_id, items, page, total_pages) or {
            "inline_keyboard": []
        }

        edited = True
        if message_id:
            edited = await self._edit_message(
                client, conf, chat_id, message_id, text, reply_markup,
                parse_mode="HTML",
            )
        with contextlib.suppress(Exception):
            await self._answer_callback(
                client,
                conf,
                callback_id,
                f"第 {page}/{total_pages} 页" if edited else "刷新失败，请重试",
            )
        log.debug(
            "Bot 搜索翻页 user=%s keyword=%s page=%s/%s",
            sender_id,
            session["keyword"],
            page,
            total_pages,
        )

    async def _handle_cleanup_callback(self, callback_id: str, data: str,
                                       chat_id: int, message_id: int,
                                       conf: dict, client: httpx.AsyncClient) -> None:
        action = data.split(":", 1)[1] if ":" in data else ""
        if action == "cancel":
            text = "已取消链接清理。"
            if message_id:
                await self._edit_message(
                    client, conf, chat_id, message_id, text, parse_mode="HTML"
                )
            else:
                await self._send_message(client, conf, chat_id, text)
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id, "已取消")
            return
        if action != "confirm":
            with contextlib.suppress(Exception):
                await self._answer_callback(client, conf, callback_id, "不支持的操作")
            return
        await self._answer_callback(client, conf, callback_id, "正在清理…")
        try:
            result = await asyncio.to_thread(self._cleanup_links_sync)
        except Exception as e:
            text = f"清理失败：{e}"
        else:
            text = self._cleanup_result_text(result)
        if message_id:
            await self._edit_message(
                client, conf, chat_id, message_id, text, parse_mode="HTML"
            )
        else:
            await self._send_message(client, conf, chat_id, text, parse_mode="HTML")

    def _queue_counts(self) -> tuple[int, int]:
        counts = self.store.transfer_job_counts()
        waiting = int(counts.get("queued") or 0) + int(counts.get("paused") or 0)
        running = int(counts.get("running") or 0)
        return waiting, running

    async def _enqueue_transfer_message(self, text: str, user_id: int,
                                        conf: dict,
                                        client: httpx.AsyncClient) -> None:
        if self.transfer_manager is None:
            await self._send_message(
                client, conf, user_id, "秒传服务还没有启动，暂时无法创建任务。"
            )
            return

        guangya = GuangyaAuth(self.store).status()
        if not guangya.get("connected"):
            await self._send_message(
                client, conf, user_id, "光鸭账号还没有登录，请先在设置页完成登录。"
            )
            return

        lines = transfer_module.split_share_inputs(text)
        jobs: list[tuple[dict, str]] = []
        for line in lines:
            share_id, pass_code = transfer_module.parse_share_input(line)
            if not share_id:
                continue
            job = self.transfer_manager.create_job(
                share_url=line,
                pass_code=pass_code,
                target_folder_id=str(guangya.get("root_folder_id") or ""),
                target_folder_path=str(guangya.get("root_folder_path") or "/"),
                source="telegram",
            )
            jobs.append((job, line))

        if not jobs:
            await self._send_message(
                client, conf, user_id, "没有识别到可加入秒传的 PikPak 分享链接。"
            )
            return

        job_ids = {int(job.get("id") or 0) for job, _ in jobs}
        self._suppressed_jobs.update(job_ids)
        try:
            for job, line in jobs:
                waiting, running = self._queue_counts()
                name = str(job.get("title") or "").strip()
                if not name:
                    name = " ".join(line.split())[:100]
                log.info(
                    "Bot 已加入秒传队列 job=%s waiting=%s running=%s",
                    job.get("id"),
                    waiting,
                    running,
                )
                await self._send_message(
                    client,
                    conf,
                    user_id,
                    f"已把「{name}」加入秒传队列，当前队列 {waiting + running} 个"
                    f"（待处理 {waiting} · 执行中 {running}）。",
                )
        finally:
            self._suppressed_jobs.difference_update(job_ids)

    @staticmethod
    def _notify_state(store: Store) -> dict:
        state = store.get_json(BOT_NOTIFY_STATE_KEY, {}) or {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("progress_messages"), dict):
            state["progress_messages"] = {}
        if not isinstance(state.get("summary_messages"), dict):
            state["summary_messages"] = {}
        if not isinstance(state.get("progress_state"), dict):
            state["progress_state"] = {}
        if not isinstance(state.get("completed"), list):
            state["completed"] = []
        state.pop("last_progress_at", None)
        return state

    def _queue_job_event(self, job_id: int, event: dict | None = None) -> None:
        if self._job_events is not None and not self._stop.is_set():
            self._job_events.put_nowait((int(job_id), dict(event or {})))

    def _prime_job_events(self) -> None:
        """首次配置跳过历史终态；之后重启补发漏掉的完成，并刷新进行中任务。"""
        state = self._notify_state(self.store)
        completed = {int(x) for x in state.get("completed") or [] if str(x).isdigit()}
        terminal_to_send: list[int] = []
        active_to_send: list[int] = []
        for job in self.store.list_transfer_jobs(limit=200):
            job_id = int(job.get("id") or 0)
            if not job_id:
                continue
            status = str(job.get("status") or "")
            if str(job.get("source") or "") != "telegram":
                continue
            if status in ACTIVE_JOB_STATUSES:
                completed.discard(job_id)
                active_to_send.append(job_id)
            elif status in TERMINAL_JOB_STATUSES and job_id not in completed:
                terminal_to_send.append(job_id)

        if not state.get("initialized"):
            completed.update(terminal_to_send)
            terminal_to_send = []
            state["initialized"] = True
        state["completed"] = list(completed)[-500:]
        self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
        for job_id in [*active_to_send, *terminal_to_send]:
            self._queue_job_event(job_id, {"type": "snapshot"})

    async def _job_event_loop(self) -> None:
        pending_progress: dict[int, dict] = {}
        while not self._stop.is_set():
            conf = resolve_bot_settings(self.store)
            if not conf["token"] or conf["user_id"] <= 0:
                return
            timeout = httpx.Timeout(
                35.0, connect=15.0, read=35.0, write=15.0, pool=15.0
            )
            try:
                async with httpx.AsyncClient(
                    proxy=self._proxy_url(), timeout=timeout
                ) as client:
                    while not self._stop.is_set():
                        current = resolve_bot_settings(self.store)
                        if current != conf:
                            break
                        queue = self._job_events
                        if queue is None:
                            return
                        try:
                            job_id, event = await asyncio.wait_for(
                                queue.get(), timeout=PROGRESS_TICK_SECONDS
                            )
                        except asyncio.TimeoutError:
                            await self._flush_due_progress(
                                pending_progress, conf, client
                            )
                            continue

                        event_type = str(event.get("type") or "snapshot")
                        retry_event = event_type != "progress"
                        try:
                            while job_id in self._suppressed_jobs and not self._stop.is_set():
                                await asyncio.sleep(0.2)
                            if event_type == "progress":
                                pending_progress[job_id] = event
                            elif event_type == "parsed":
                                pending_progress.pop(job_id, None)
                                await self._handle_parsed_event(job_id, conf, client)
                            elif event_type == "final":
                                pending_progress.pop(job_id, None)
                                await self._handle_final_event(job_id, conf, client)
                            elif event_type == "reset":
                                pending_progress.pop(job_id, None)
                                await self._handle_reset_event(job_id)
                            else:
                                progress_event = await self._handle_snapshot_event(
                                    job_id, conf, client
                                )
                                if progress_event:
                                    pending_progress[job_id] = progress_event
                            await self._flush_due_progress(
                                pending_progress, conf, client
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            detail = str(e).replace(conf["token"], "***")
                            self._last_error = detail
                            log.warning("Telegram Bot 秒传推送失败：%s", detail)
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(self._stop.wait(), timeout=5)
                            if (
                                retry_event
                                and not self._stop.is_set()
                                and queue is not None
                            ):
                                queue.put_nowait((job_id, event))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                detail = str(e).replace(conf["token"], "***")
                self._last_error = detail
                log.warning("Telegram Bot 秒传推送连接失败：%s", detail)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5)

    @staticmethod
    def _event_counts(event: dict, job: dict) -> tuple[int, int, int, int]:
        completed = int(event.get("completed_files", job.get("completed_files")) or 0)
        failed = int(event.get("failed_files", job.get("failed_files")) or 0)
        skipped = int(event.get("skipped_files", job.get("skipped_files")) or 0)
        completed_bytes = int(
            event.get("completed_bytes", job.get("completed_bytes")) or 0
        )
        return completed, failed, skipped, completed_bytes

    async def _handle_parsed_event(self, job_id: int, conf: dict,
                                   client: httpx.AsyncClient) -> None:
        job = self.store.get_transfer_job(job_id)
        if (
            not job
            or str(job.get("source") or "") != "telegram"
            or int(job.get("total_files") or 0) <= 0
        ):
            return
        state = self._notify_state(self.store)
        key = str(job_id)
        await self._upsert_task_message(
            "summary_messages", job_id, self._summary_text(job),
            conf, client, state,
        )
        progress_state: dict = state["progress_state"]
        if not isinstance(progress_state.get(key), dict):
            progress_state[key] = {"done": 0, "at": time.time()}
        self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
        log.info("Bot 已发送解析完成摘要 job=%s", job_id)

    async def _handle_snapshot_event(self, job_id: int, conf: dict,
                                     client: httpx.AsyncClient) -> dict | None:
        job = self.store.get_transfer_job(job_id)
        if not job or str(job.get("source") or "") != "telegram":
            return None
        status = str(job.get("status") or "")
        if status in TERMINAL_JOB_STATUSES:
            await self._handle_final_event(job_id, conf, client)
            return None
        if status not in ACTIVE_JOB_STATUSES or int(job.get("total_files") or 0) <= 0:
            return None

        await self._handle_parsed_event(job_id, conf, client)
        state = self._notify_state(self.store)
        state["progress_state"][str(job_id)] = {"done": 0, "at": 0}
        self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
        completed, failed, skipped, completed_bytes = self._event_counts({}, job)
        return {
            "type": "progress",
            "status": status,
            "completed_files": completed,
            "failed_files": failed,
            "skipped_files": skipped,
            "completed_bytes": completed_bytes,
        }

    async def _handle_reset_event(self, job_id: int) -> None:
        state = self._notify_state(self.store)
        completed = {int(x) for x in state.get("completed") or [] if str(x).isdigit()}
        completed.discard(job_id)
        state["completed"] = list(completed)[-500:]
        state["progress_state"].pop(str(job_id), None)
        self.store.set_json(BOT_NOTIFY_STATE_KEY, state)

    async def _handle_final_event(self, job_id: int, conf: dict,
                                  client: httpx.AsyncClient) -> None:
        job = self.store.get_transfer_job(job_id)
        if not job or str(job.get("source") or "") != "telegram":
            return
        status = str(job.get("status") or "")
        if status not in TERMINAL_JOB_STATUSES:
            return
        state = self._notify_state(self.store)
        completed = {int(x) for x in state.get("completed") or [] if str(x).isdigit()}
        if job_id in completed:
            state["progress_messages"].pop(str(job_id), None)
            state["progress_state"].pop(str(job_id), None)
            self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
            return
        await self._upsert_task_message(
            "progress_messages", job_id, self._completion_text(job),
            conf, client, state,
        )
        status_label = TRANSFER_STATUS_LABELS.get(status, status or "未知")
        await self._upsert_task_message(
            "summary_messages", job_id, self._summary_text(job, status_label),
            conf, client, state,
        )
        state["progress_messages"].pop(str(job_id), None)
        state["progress_state"].pop(str(job_id), None)
        completed.add(job_id)
        state["completed"] = list(completed)[-500:]
        self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
        log.info("Bot 已发送完成通知 job=%s status=%s", job_id, status)

    async def _flush_due_progress(self, pending: dict[int, dict], conf: dict,
                                  client: httpx.AsyncClient) -> None:
        if not pending:
            return
        now = time.time()
        state = self._notify_state(self.store)
        progress_state: dict = state["progress_state"]
        for job_id, event in list(pending.items()):
            job = self.store.get_transfer_job(job_id)
            if not job:
                pending.pop(job_id, None)
                progress_state.pop(str(job_id), None)
                continue
            if str(job.get("source") or "") != "telegram":
                pending.pop(job_id, None)
                continue
            status = str(job.get("status") or "")
            if status not in ACTIVE_JOB_STATUSES:
                pending.pop(job_id, None)
                continue

            key = str(job_id)
            completed, failed, skipped, completed_bytes = self._event_counts(event, job)
            done = completed + failed + skipped
            meta = progress_state.get(key)
            if not isinstance(meta, dict):
                meta = {"done": 0, "at": 0}
            try:
                last_done = int(meta.get("done") or 0)
                last_at = float(meta.get("at") or 0)
            except (TypeError, ValueError):
                last_done, last_at = 0, 0
            count_due = done - last_done >= PROGRESS_BATCH_SIZE
            time_due = bool(last_at) and now - last_at >= PROGRESS_MAX_SECONDS
            if last_at and not count_due and not time_due:
                continue

            progress_event = {
                **event,
                "completed_files": completed,
                "failed_files": failed,
                "skipped_files": skipped,
                "completed_bytes": completed_bytes,
            }
            text = self._progress_text(job, progress_event)
            try:
                await self._upsert_task_message(
                    "progress_messages", job_id, text, conf, client, state
                )
            except Exception as e:
                detail = str(e).replace(conf["token"], "***")
                log.warning("Telegram Bot 推送任务进度失败：%s", detail)
                continue
            progress_state[key] = {"done": done, "at": now}
            pending.pop(job_id, None)
            self.store.set_json(BOT_NOTIFY_STATE_KEY, state)
            log.debug("Bot 已合并推送任务进度 job=%s done=%s", job_id, done)

    async def _upsert_task_message(self, state_key: str, job_id: int, text: str,
                                   conf: dict, client: httpx.AsyncClient,
                                   state: dict) -> int:
        key = str(job_id)
        messages: dict = state[state_key]
        message_id = int(messages.get(key) or 0)
        if message_id:
            edited = await self._edit_message(
                client, conf, int(conf["user_id"]), message_id, text,
                parse_mode="HTML",
            )
            if edited:
                return message_id
        message_id = await self._send_message(
            client, conf, int(conf["user_id"]), text, parse_mode="HTML"
        )
        if message_id:
            messages[key] = message_id
        return message_id

    @staticmethod
    def _summary_text(job: dict, state_text: str = "正在秒传") -> str:
        job_id = int(job.get("id") or 0)
        name = " ".join(
            str(job.get("title") or job.get("share_url") or f"任务 #{job_id}").split()
        )[:120]
        return "\n".join([
            "📥 <b>PikPak 秒传任务已解析</b>",
            "━━━━━━━━━━━━━━",
            f"📄 标题：<b>{html.escape(name)}</b>",
            f"📦 文件个数：<b>{int(job.get('total_files') or 0)}</b> 个",
            f"💾 文件大小：<b>{_fmt_bytes(job.get('total_bytes'))}</b>",
            f"📂 目标目录：<code>{html.escape(_target_folder(job))}</code>",
            f"🆔 任务编号：<code>#{job_id}</code>",
            f"🔄 状态：<b>{html.escape(state_text)}</b>",
        ])

    @staticmethod
    def _completion_text(job: dict) -> str:
        job_id = int(job.get("id") or 0)
        name = " ".join(
            str(job.get("title") or job.get("share_url") or f"任务 #{job_id}").split()
        )[:120]
        name = html.escape(name)
        target = html.escape(_target_folder(job))
        status = str(job.get("status") or "")
        if status != "finished":
            label = TRANSFER_STATUS_LABELS.get(status, status or "未知")
            return (
                f"❌ <b>PikPak秒传光鸭 任务未完成：{name}</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"状态：{html.escape(label)}\n"
                f"✅ 成功 {int(job.get('completed_files') or 0)} 个，"
                f"❌ 失败 {int(job.get('failed_files') or 0)} 个\n"
                f"📦 总体积：{_fmt_bytes(job.get('total_bytes'))}\n"
                f"📦 成功体积：{_fmt_bytes(job.get('completed_bytes'))}\n"
                f"📂 目标目录 ID：{target}"
            )
        return (
            f"✅ <b>PikPak秒传光鸭 秒传完成：{name}</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"✅ 成功 {int(job.get('completed_files') or 0)} 个，"
            f"❌ 失败 {int(job.get('failed_files') or 0)} 个\n"
            f"📦 总体积：{_fmt_bytes(job.get('total_bytes'))}\n"
            f"📦 成功体积：{_fmt_bytes(job.get('completed_bytes'))}\n"
            f"📂 目标目录 ID：{target}"
        )

    @staticmethod
    def _progress_text(job: dict, event: dict | None = None) -> str:
        job_id = int(job.get("id") or 0)
        name = " ".join(
            str(job.get("title") or job.get("share_url") or f"任务 #{job_id}").split()
        )[:120]
        event = event or {}
        status = str(event.get("status") or job.get("status") or "")
        label = TRANSFER_STATUS_LABELS.get(status, status or "未知")
        total = int(job.get("total_files") or 0)
        completed_files, failed_files, skipped_files, completed_bytes = (
            TelegramBotRunner._event_counts(event, job)
        )
        done = completed_files + failed_files + skipped_files
        progress = (done / total * 100) if total else 0.0
        if status == "paused":
            header = "⏸ <b>PikPak秒传光鸭 已暂停</b>"
        elif status == "queued":
            header = "🕒 <b>PikPak秒传光鸭 排队中</b>"
        else:
            header = "⏳ <b>PikPak秒传光鸭 正在秒传</b>"
        lines = [
            header,
            "━━━━━━━━━━━━━━",
            f"📄 <b>{html.escape(name)}</b>",
            f"🔄 状态：{html.escape(label)}",
            f"📊 已处理：<b>{done}/{total}</b>（{progress:.1f}%）",
            f"✅ 成功 {completed_files}　❌ 失败 {failed_files}　⏭ 跳过 {skipped_files}",
            f"💾 成功体积：{_fmt_bytes(completed_bytes)}",
            f"📂 目标目录：{html.escape(_target_folder(job))}",
        ]
        return "\n".join(lines)
