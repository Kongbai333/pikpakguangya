"""API 进程里的 Telegram 会话管理：登录、群列表、加群/退群。

多账号：一个账号一个 session 文件（见 app/store.py 的 accounts 表），
文件都放在 config.toml 里 telegram.session 所在的目录下。采集用「当前使用」的那个。
指定 account=None 时跟随库里的「当前使用」，老库还没账号记录就退回 config.toml 那个。

代理和 api_id / api_hash 从数据库设置里读（管理页可改），config.toml 只作为初始默认值。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from app.proxy import ProxyConfig, proxy_from_account

log = logging.getLogger("tg")

INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:joinchat/)?(\+?[\w\-]+)", re.I)

CREDENTIALS_KEY = "telegram.credentials"
API_HASH_RE = re.compile(r"[0-9a-fA-F]{32}")


def session_file(path: Path) -> Path:
    """Telethon 的 SQLiteSession 会在文件名后面补 `.session`，这里跟上，免得对不上。

    config.toml 里写 `session/collector`，实际落盘是 `session/collector.session`。
    以前只有一处用到（cfg.session_path()），现在多了「按文件名找账号的 session」，
    不统一就会出现「文件明明在，页面却说没有登录态」。
    """
    return path if str(path).endswith(".session") else Path(f"{path}.session")


class LoginError(Exception):
    """带用户可读信息的错误，API 层直接转 400 返回给页面。"""


PHONE_RE = re.compile(r"^\+?\d{5,20}$")


def _clean_phone(value: Any) -> str:
    """get_me() 回来的 phone 不一定是手机号。

    Telegram 有时往这个字段塞一串内部串（实测拿到过 `gld1_36_...`），
    直接存进账号表会让人以为「手机号变成乱码了」，所以只认标准格式。
    """
    s = str(value or "").strip()
    return s if PHONE_RE.fullmatch(s) else ""


def _me_dict(user: Any) -> dict:
    if user is None:
        return {}
    return {
        "id": getattr(user, "id", None),
        "first_name": getattr(user, "first_name", None) or "",
        "last_name": getattr(user, "last_name", None) or "",
        "username": getattr(user, "username", None) or "",
        "phone": _clean_phone(getattr(user, "phone", None)),
    }

SESSION_LOCKED_HINT = (
    "登录态文件（session）正被另一个进程占用 —— 多半是采集进程在跑。"
    "采集和这个后台不能同时连 Telegram（Telethon 的 session 是 SQLite，会抢锁），"
    "先在「群组 → 采集」里停掉采集再操作。"
)


def _make_session(path):
    """建 Telethon 的 session，并加长 SQLite 的锁等待。

    Telethon 的 SQLiteSession 里写死了 sqlite3.connect(path, check_same_thread=False)，
    没传 timeout，所以用的是 sqlite3 默认的 5 秒。两个进程共用一个 session 文件时
    5 秒很容易撞上 database is locked，这里改成 30 秒让它自己等过去。

    （用 _conn 私有属性是因为 SQLiteSession 没留任何口子传 pragma。
    它的 __init__ 里已经建好连接了，只能在事后设。）
    """
    from telethon.sessions import SQLiteSession

    sess = SQLiteSession(str(path))
    with contextlib.suppress(Exception):
        sess._conn.execute("PRAGMA busy_timeout = 30000")
    return sess


def resolve_credentials(cfg, store) -> dict:
    """凭据来源：管理页填的（数据库）优先，没填过才回退 config.toml 的 [telegram]。

    单独抽成函数是因为监听器和 tools/login.py 也要用同一套规则，
    不然会出现「管理页填了、监听器却说没凭据」。
    """
    data = store.get_json(CREDENTIALS_KEY) or {}
    t = cfg.telegram
    api_id = int(data.get("api_id") or 0) or int(t.api_id or 0)
    api_hash = (data.get("api_hash") or "").strip() or (t.api_hash or "").strip()
    phone = (data.get("phone") or "").strip() or (t.phone or "").strip()
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "source": "管理页" if data.get("api_id") else ("config.toml" if t.api_id else ""),
    }


def missing_credentials(cred: dict) -> list[str]:
    """返回缺哪些字段。api_id / api_hash 缺一个都连不上，phone 只有命令行登录才必需。"""
    return [k for k in ("api_id", "api_hash") if not cred.get(k)]


def _friendly(e: Exception) -> str:
    """把 Telethon 的异常翻译成用户能看懂的话。"""
    from telethon import errors as E

    if isinstance(e, E.FloodWaitError):
        return f"Telegram 限流：请等 {getattr(e, 'seconds', '?')} 秒再试"
    if isinstance(e, E.PhoneNumberInvalidError):
        return "手机号格式不对，要带国家码，例如 +8613800000000"
    if isinstance(e, E.PhoneNumberBannedError):
        return "这个手机号已被 Telegram 封禁"
    if isinstance(e, E.PhoneCodeInvalidError):
        return "验证码不对"
    if isinstance(e, E.PhoneCodeExpiredError):
        return "验证码已过期，请重新点「发送验证码」"
    if isinstance(e, E.PasswordHashInvalidError):
        return "二级密码不对"
    if isinstance(e, E.SessionPasswordNeededError):
        return "这个账号开了二级密码验证，需要输入密码"
    if isinstance(e, E.UsernameNotOccupiedError):
        return "这个用户名不存在"
    if isinstance(e, E.UsernameInvalidError):
        return "用户名格式不对"
    if isinstance(e, E.ChannelPrivateError):
        return "这是私有群/频道，需要邀请链接（t.me/+xxx）才能加入"
    if isinstance(e, (E.InviteHashExpiredError, E.InviteHashInvalidError)):
        return "邀请链接已失效或无效"
    if isinstance(e, E.ChatAdminRequiredError):
        return "这个操作需要管理员权限"
    if isinstance(e, E.AuthKeyUnregisteredError):
        return "登录态已失效，需要重新登录"
    return f"{type(e).__name__}: {e}"


class TelegramSession:
    """懒连接：第一次用到才连，代理改了要重建连接。"""

    def __init__(self, cfg, store, connect_timeout: float = 20.0,
                 account: dict | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.connect_timeout = connect_timeout
        # 指定用哪个账号；None = 跟随库里的「当前使用」
        self._account = account
        # 由 Web 层注入：按账号决定走哪个出口。
        # 账号绑了直接代理 → 直接使用；没绑则直连。
        self.proxy_resolver = None
        self._client = None
        self._login: dict | None = None
        self._lock = asyncio.Lock()

    # ---------- 应用凭据（api_id / api_hash / phone） ----------
    def credentials(self) -> dict:
        """优先用管理页填的；没填过才回退 config.toml 的 [telegram]。"""
        return resolve_credentials(self.cfg, self.store)

    async def save_credentials(self, api_id: int, api_hash: str, phone: str) -> dict:
        """存进数据库并断开连接——凭据变了必须重建客户端。"""
        api_id = int(api_id or 0)
        api_hash = (api_hash or "").strip()
        phone = (phone or "").strip()
        if api_id and not (0 < api_id < 2**31):
            raise LoginError("api_id 不合法，应该是一串数字")
        if api_hash and not API_HASH_RE.fullmatch(api_hash):
            raise LoginError("api_hash 应该是 32 位十六进制字符")
        if phone and not phone.startswith("+"):
            phone = "+" + phone.lstrip("+")
        self.store.set_json(
            CREDENTIALS_KEY, {"api_id": api_id, "api_hash": api_hash, "phone": phone}
        )
        await self.disconnect()
        return self.credentials()

    # ---------- 账号 / session 文件 ----------
    def sessions_dir(self) -> Path:
        """所有账号的 session 文件都放这里（config.toml 里 telegram.session 的同级目录）。"""
        return self.cfg.session_path().parent

    def session_path(self) -> Path:
        """当前该用哪个 session 文件。"""
        account = self._account or self.store.active_account()
        name = (account or {}).get("session") or ""
        if not name:
            # 还没有任何账号记录：退回 config.toml 那个，老行为不变
            return session_file(self.cfg.session_path())
        return self.account_session_path(name)

    def account_session_path(self, session_name: str) -> Path | None:
        """账号的 session 文件路径。表里存的是文件名（`collector` 或 `acct-xxx.session`）。"""
        if not session_name:
            return None
        p = Path(session_name)
        return session_file(p if p.is_absolute() else self.sessions_dir() / p)

    def account(self) -> dict | None:
        """当前用的是哪个账号。按 id 回库里取最新一行（名字、手机号可能刚补上）。"""
        if self._account and self._account.get("id"):
            return self.store.get_account(self._account["id"]) or self._account
        return self.store.active_account()

    async def use_account(self, account: dict | None) -> None:
        """切换账号。必须先断开，否则连接还挂在上一个账号的 session 上。"""
        self._account = account
        await self.disconnect()

    # ---------- 代理 ----------
    def proxy_config(self) -> ProxyConfig:
        """账号绑哪条直接代理就走哪条；没绑则直连。"""
        account = self.account()
        if self.proxy_resolver is not None:
            try:
                resolved = self.proxy_resolver(account)
            except Exception:      # 解析失败不该让整个连接流程挂掉
                resolved = None
            if resolved is not None:
                return resolved.normalized()
            if int((account or {}).get("proxy_id") or 0) > 0:
                raise LoginError("账号绑定的代理已经不存在，请在账号监控页重新选择")
            return ProxyConfig()
        return proxy_from_account(self.store, account) or ProxyConfig()

    async def apply_proxy_change(self) -> None:
        """代理改了必须重连，否则还在用旧链路。"""
        await self.disconnect()

    # ---------- 连接 ----------
    def _build_client(self):
        from telethon import TelegramClient

        cred = self.credentials()
        if not cred["api_id"] or not cred["api_hash"]:
            raise LoginError(
                "还没填 api_id / api_hash。在管理页「账号」页签填，"
                "或写进 config.toml 的 [telegram] 段"
            )
        proxy = self.proxy_config()
        proxy.validate()
        session = self.session_path()
        session.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            _make_session(session), cred["api_id"], cred["api_hash"],
            **proxy.telethon_kwargs(),
        )

    async def get_client(self):
        async with self._lock:
            if self._client is not None:
                return self._client
            client = self._build_client()
            try:
                await asyncio.wait_for(client.connect(), timeout=self.connect_timeout)
            except asyncio.TimeoutError as e:
                proxy = self.proxy_config()
                raise LoginError(
                    f"连接 Telegram 超时（{self.connect_timeout:.0f} 秒）。"
                    + (f"当前代理 {proxy.masked()}，先在「代理」页测试连通性。" if proxy.enabled else "没配代理，国内直连 Telegram 通常不通。")
                ) from e
            except Exception as e:
                if "database is locked" in str(e).lower():
                    raise LoginError(SESSION_LOCKED_HINT) from e
                proxy = self.proxy_config()
                hint = f"（走代理 {proxy.masked()}）" if proxy.enabled else "（直连）"
                raise LoginError(f"连接 Telegram 失败{hint}：{type(e).__name__}: {e}") from e
            self._client = client
            return client

    async def disconnect(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.debug("断开连接时出错", exc_info=True)

    async def status(self) -> dict:
        cred = self.credentials()
        proxy_error = ""
        try:
            proxy = self.proxy_config()
        except LoginError as e:
            proxy = ProxyConfig(enabled=False, name="账号代理不可用")
            proxy_error = str(e)
        info = {
            "proxy": proxy.to_dict(),
            "proxy_masked": (
                proxy.masked() if proxy.enabled else ("账号代理不可用" if proxy_error else "直连")
            ),
            "proxy_enabled": proxy.enabled,
            "session_file": str(self.session_path()),
            "api_id_set": bool(cred["api_id"] and cred["api_hash"]),
            "api_id": cred["api_id"],
            "api_hash_set": bool(cred["api_hash"]),
            "phone": cred["phone"],
            "credentials_source": cred["source"],
            "account": self.account(),
            "connected": False,
            "authorized": False,
            "me": None,
            "error": proxy_error,
        }
        if proxy_error:
            return info
        try:
            client = await self.get_client()
        except LoginError as e:
            info["error"] = str(e)
            return info
        info["connected"] = client.is_connected()
        try:
            info["authorized"] = await client.is_user_authorized()
            if info["authorized"]:
                info["me"] = _me_dict(await client.get_me())
        except Exception as e:
            info["error"] = _friendly(e)
        return info

    async def require_authorized(self):
        client = await self.get_client()
        if not await client.is_user_authorized():
            raise LoginError("账号还没登录，请先在「账号」页登录")
        return client

    # ---------- 登录三步 ----------
    async def send_code(self, phone: str) -> dict:
        phone = (phone or "").strip()
        if not phone.startswith("+"):
            raise LoginError("手机号要带国家码，例如 +8613800000000")
        client = await self.get_client()
        if await client.is_user_authorized():
            return {"already_logged_in": True, "me": _me_dict(await client.get_me())}
        try:
            sent = await client.send_code_request(phone)
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        self._login = {"phone": phone, "hash": getattr(sent, "phone_code_hash", None)}
        log.info("已向 %s 发送登录码", phone)
        return {"sent": True, "phone": phone}

    async def submit_code(self, code: str) -> dict:
        if not self._login:
            raise LoginError("还没请求验证码，请先点「发送验证码」")
        code = (code or "").strip()
        if not code:
            raise LoginError("请填验证码")
        client = await self.get_client()
        from telethon.errors import SessionPasswordNeededError

        try:
            await client.sign_in(
                phone=self._login["phone"], code=code, phone_code_hash=self._login["hash"],
            )
        except SessionPasswordNeededError:
            return {"need_password": True}
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        self._login = None
        me = _me_dict(await client.get_me())
        log.info("登录成功：%s", me.get("username") or me.get("id"))
        return {"ok": True, "me": me}

    async def submit_password(self, password: str) -> dict:
        if not (password or "").strip():
            raise LoginError("请填二级密码")
        client = await self.get_client()
        try:
            await client.sign_in(password=password.strip())
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        self._login = None
        me = _me_dict(await client.get_me())
        log.info("二级密码通过，登录成功：%s", me.get("username") or me.get("id"))
        return {"ok": True, "me": me}

    async def logout(self) -> dict:
        client = await self.get_client()
        try:
            await client.log_out()
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        await self.disconnect()
        self._login = None
        return {"ok": True}

    # ---------- 群列表 / 加群 / 退群 ----------
    async def list_dialogs(self) -> list[dict]:
        client = await self.require_authorized()
        out: list[dict] = []
        try:
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_channel):
                    continue
                ent = dialog.entity
                if getattr(ent, "broadcast", False):
                    kind = "频道"
                elif getattr(ent, "megagroup", False):
                    kind = "超级群"
                else:
                    kind = "群"
                out.append({
                    "chat_id": int(dialog.id),
                    "title": dialog.name or "",
                    "username": getattr(ent, "username", None) or "",
                    "kind": kind,
                    "participants": getattr(ent, "participants_count", None),
                    "is_member": True,
                })
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        return out

    async def join(self, target: str) -> dict:
        """支持 @username / t.me/xxx / t.me/+邀请码 / 数字 id。"""
        client = await self.require_authorized()
        raw = (target or "").strip()
        if not raw:
            raise LoginError("请填 @username、t.me 链接或 t.me/+邀请链接")

        from telethon.errors import (
            InviteRequestSentError,
            UserAlreadyParticipantError,
        )
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest

        m = INVITE_RE.search(raw)
        token = m.group(1) if m else raw
        is_invite = token.startswith("+") or bool(m and "joinchat/" in raw.lower())

        try:
            if is_invite:
                res = await client(ImportChatInviteRequest(token.lstrip("+")))
                chat = res.chats[0] if getattr(res, "chats", None) else None
                return {
                    "joined": True,
                    "chat_id": int(getattr(chat, "id", 0)) or None,
                    "title": getattr(chat, "title", "") or "",
                    "username": getattr(chat, "username", None) or "",
                }
            entity = await self._resolve(client, token)
            try:
                await client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                return {
                    "joined": True, "already": True,
                    "chat_id": int(getattr(entity, "id", 0)) or None,
                    "title": getattr(entity, "title", "") or "",
                    "username": getattr(entity, "username", None) or "",
                }
            return {
                "joined": True,
                "chat_id": int(getattr(entity, "id", 0)) or None,
                "title": getattr(entity, "title", "") or "",
                "username": getattr(entity, "username", None) or "",
            }
        except InviteRequestSentError:
            return {"joined": False, "pending_approval": True,
                    "message": "已提交入群申请，等管理员批准"}
        except LoginError:
            raise
        except Exception as e:
            raise LoginError(_friendly(e)) from e

    async def leave(self, chat_id: int) -> dict:
        client = await self.require_authorized()
        from telethon.tl.functions.channels import LeaveChannelRequest

        try:
            entity = await client.get_entity(int(chat_id))
            await client(LeaveChannelRequest(entity))
        except Exception as e:
            raise LoginError(_friendly(e)) from e
        return {"left": True, "chat_id": int(chat_id)}

    @staticmethod
    async def _resolve(client, token: str):
        """@username / username / t.me/username / 数字 id -> entity。"""
        token = token.strip()
        if token.lstrip("-").isdigit():
            return await client.get_entity(int(token))
        if not token.startswith("@"):
            token = "@" + token
        return await client.get_entity(token)
