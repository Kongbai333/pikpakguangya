"""代理配置：类型校验、粘贴解析、Telethon 参数和连通性测试。

两种代理，用途不一样：

  http / https / socks5   标准代理。既能给 Telegram 用，也能给 httpx 用
                          （PikPak 校验、分享详情接口、秒传、Bot API）。
  mtproto                 Telegram 的 MTProto 代理，就是 `tg://proxy?...` 那种。
                          **只能给 Telegram 连接用** —— 它说的是 MTProto 协议，
                          代理不了普通的 HTTP 请求，所以不能当系统代理。

背景（踩过的坑）：Telethon 的 proxy 参数在没装 python-socks 时**只打一条 warning
然后静默忽略**，看起来像「代理没生效但也不报错」。所以 requirements.txt 里必须
有 python-socks，并且本模块会显式检查。

不再使用订阅、节点或 sing-box —— 只保存能直接连上的代理（含 Telegram 的 MTProto 代理）。
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

log = logging.getLogger("proxy")

# 标准代理：Telethon 和 httpx 都能用
STANDARD_TYPES = ("http", "https", "socks5")
# MTProto 代理：只有 Telethon 能用
MT_PROXY_TYPE = "mtproto"
SUPPORTED_TYPES = (*STANDARD_TYPES, MT_PROXY_TYPE)
DEFAULT_PORTS = {"http": 8080, "https": 8080, "socks5": 1080, "mtproto": 443}

# MTProto secret：32 位十六进制，或带 dd/ee 前缀的 34 位
SECRET_HEX_RE = re.compile(r"^(?:dd|ee)?[0-9a-fA-F]{32}$")
HOST_PORT_RE = re.compile(r"^(?P<host>[^:\s]+):(?P<port>\d{1,5})$")

def python_socks_installed() -> bool:
    try:
        import python_socks  # noqa: F401
    except ImportError:
        return False
    return True


def secret_warning(secret: str) -> str:
    """MTProto secret 的可用性提醒；没问题就返回空串。

    `ee` 开头的是 fake-TLS（域名伪装）代理：secret 后面还跟着一个十六进制的域名，
    客户端要在 TLS ClientHello 里用这个域名做伪装。Telethon 没实现这一段 ——
    它把域名直接丢掉（tcpmtproxy.py 里那句 "until domain support is added"），
    所以这类代理基本连不上。`dd`（随机填充）和裸 secret 是正常的。
    """
    text = (secret or "").strip().lower()
    if not text:
        return ""
    if text.startswith("ee"):
        return (
            "这是 fake-TLS（域名伪装）的 MTProto 代理，Telethon 不支持域名伪装，"
            "大概率连不上。换 dd 开头的，或者用 SOCKS5 / HTTP 代理。"
        )
    return ""


@dataclass
class ProxyConfig:
    enabled: bool = False
    proxy_type: str = "socks5"
    addr: str = "127.0.0.1"
    port: int = 1080
    username: str = ""
    password: str = ""
    # 只有 mtproto 用：tg://proxy 链接里的 secret
    secret: str = ""
    rdns: bool = True         # 由代理端做 DNS 解析（socks5 建议开）
    name: str = ""            # 备注，多条代理时好认

    def normalized(self) -> "ProxyConfig":
        proxy_type = (self.proxy_type or "socks5").strip().lower()
        return ProxyConfig(
            enabled=bool(self.enabled),
            proxy_type=proxy_type,
            addr=(self.addr or "").strip(),
            port=int(self.port or 0) or DEFAULT_PORTS.get(proxy_type, 1080),
            username=(self.username or "").strip(),
            password=(self.password or "").strip(),
            secret=(self.secret or "").strip(),
            rdns=bool(self.rdns),
            name=(self.name or "").strip(),
        )

    def validate(self) -> None:
        cfg = self.normalized()
        if not cfg.enabled:
            return
        if cfg.proxy_type not in SUPPORTED_TYPES:
            raise ValueError(
                f"不支持的代理类型 {cfg.proxy_type!r}，只支持 {'/'.join(SUPPORTED_TYPES)}"
            )
        if not cfg.addr:
            raise ValueError("代理地址不能为空")
        if not (0 < cfg.port < 65536):
            raise ValueError(f"代理端口不合法：{cfg.port}")
        if cfg.proxy_type == MT_PROXY_TYPE:
            if not cfg.secret:
                raise ValueError("MTProto 代理要填 secret（tg://proxy 链接里的那串）")
            if not SECRET_HEX_RE.match(cfg.secret):
                raise ValueError(
                    "MTProto secret 应该是 32 位十六进制（可以带 dd / ee 前缀）"
                )
            return
        if not python_socks_installed():
            raise ValueError("缺少 python-socks，代理会被 Telethon 静默忽略：pip install python-socks")

    def supports_http(self) -> bool:
        """能不能代理普通 HTTP 请求（httpx 用的那条路）。

        MTProto 代理不行：它只认 MTProto 协议，PikPak 的 HTTPS 请求过不去。
        """
        cfg = self.normalized()
        return cfg.enabled and cfg.proxy_type in STANDARD_TYPES

    def masked(self) -> str:
        cfg = self.normalized()
        auth = f"{cfg.username}:***@" if cfg.username else ""
        return f"{cfg.proxy_type}://{auth}{cfg.addr}:{cfg.port}"

    def to_url(self) -> str:
        """转成 httpx 能用的代理 URL。MTProto 用不了，返回空串。"""
        cfg = self.normalized()
        if not cfg.supports_http():
            return ""
        auth = ""
        if cfg.username:
            auth = quote(cfg.username, safe="")
            if cfg.password:
                auth += ":" + quote(cfg.password, safe="")
            auth += "@"
        return f"{cfg.proxy_type}://{auth}{cfg.addr}:{cfg.port}"

    def to_dict(self) -> dict:
        return asdict(self.normalized())

    def telethon_kwargs(self) -> dict:
        """给 TelegramClient(**kwargs) 用的参数。未启用时返回空 dict。"""
        cfg = self.normalized()
        if not cfg.enabled:
            return {}
        if cfg.proxy_type == MT_PROXY_TYPE:
            # MTProto 走的是另一条路：换 connection 类，proxy 传 (host, port, secret)。
            # RandomizedIntermediate 是现在通用的那个 —— 代理端按首字节自己认协议，
            # dd（随机填充）的 secret 也走它。
            from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

            return {
                "connection": ConnectionTcpMTProxyRandomizedIntermediate,
                "proxy": (cfg.addr, int(cfg.port), cfg.secret),
            }
        proxy_type = "http" if cfg.proxy_type == "https" else cfg.proxy_type
        proxy: dict[str, Any] = {
            "proxy_type": proxy_type,
            "addr": cfg.addr,
            "port": int(cfg.port),
            "rdns": cfg.rdns,
        }
        if cfg.username:
            proxy["username"] = cfg.username
        if cfg.password:
            proxy["password"] = cfg.password
        return {"proxy": proxy}

    def httpx_proxy(self) -> str | None:
        """给 httpx 用的代理 URL。MTProto 返回 None。"""
        url = self.to_url()
        return url or None


def from_proxy_row(row: dict | None) -> ProxyConfig | None:
    """数据库 proxies 行 → ProxyConfig。"""
    if not row:
        return None
    return ProxyConfig(
        enabled=True,
        proxy_type=row.get("proxy_type") or "http",
        addr=row.get("addr") or "",
        port=int(row.get("port") or 0),
        username=row.get("username") or "",
        password=row.get("password") or "",
        secret=row.get("secret") or "",
        name=row.get("name") or "",
    ).normalized()


def proxy_from_account(store, account: dict | None) -> ProxyConfig | None:
    """账号直接绑定的代理；没绑定返回 None（直连）。"""
    proxy_id = int((account or {}).get("proxy_id") or 0)
    if not proxy_id:
        return None
    return from_proxy_row(store.get_proxy(proxy_id))


# ---------- 系统代理 ----------
# 全局出口：校验、PikPak 详情接口、秒传、Bot API 都走它。
# 和「代理池」是两件事：池子里的代理是绑给具体 TG 账号的（每个号可以走不同出口），
# 系统代理是那些不属于任何账号的请求共用的一个。存的是 proxies 表里的 id，
# 0 或没有 = 直连。
SYSTEM_PROXY_KEY = "proxy.system_id"


def system_proxy_id(store) -> int:
    try:
        return int(store.get_state(SYSTEM_PROXY_KEY, "0") or 0)
    except (TypeError, ValueError):
        return 0


def system_proxy(store) -> ProxyConfig | None:
    """系统代理。没配、或者配的那条已经删了，都返回 None（直连）。"""
    proxy_id = system_proxy_id(store)
    if not proxy_id:
        return None
    cfg = from_proxy_row(store.get_proxy(proxy_id))
    if cfg is None:
        return None
    if not cfg.supports_http():
        # MTProto 代理只能连 Telegram，代理不了 PikPak 的 HTTPS。
        # 真按它走，所有请求会静默失败 —— 不如直接退回直连并在日志里说清楚。
        log.warning(
            "系统代理 #%s 是 MTProto 代理，代理不了普通 HTTP 请求，本次按直连处理", proxy_id
        )
        return None
    return cfg


def set_system_proxy(store, proxy_id: int) -> int:
    """设置系统代理。0 = 直连。返回实际存下去的值。"""
    proxy_id = int(proxy_id or 0)
    if proxy_id and not store.get_proxy(proxy_id):
        raise ValueError("没有这个代理")
    store.set_state(SYSTEM_PROXY_KEY, str(proxy_id))
    return proxy_id


def adopt_system_proxy(store) -> bool:
    """老库升级：把「当前使用」账号绑的代理认成系统代理。

    以前全局出口就是「当前使用那个账号绑定的代理」，现在拆成了独立设置。
    不迁的话，升级完校验、秒传、Bot 会突然全变直连（国内直接不通），
    用户只会觉得「怎么突然坏了」。只认一次 —— 之后哪怕用户手动改成直连，
    这里也不会再覆盖（键存在但值是 "0"）。
    """
    if store.get_state(SYSTEM_PROXY_KEY) is not None:
        return False
    proxy_id = int((store.active_account() or {}).get("proxy_id") or 0)
    if not proxy_id or not store.get_proxy(proxy_id):
        return False
    store.set_state(SYSTEM_PROXY_KEY, str(proxy_id))
    return True


# ---------- 粘贴解析 ----------
class ProxyParseError(ValueError):
    """粘贴的内容认不出来，message 直接给用户看。"""


def _clean(value: Any) -> str:
    return unquote(str(value or "")).strip()


def _split_auth(raw: str) -> tuple[str, str]:
    """`user:pass` → (user, pass)。没有冒号时整体当用户名。"""
    if not raw:
        return "", ""
    if ":" in raw:
        user, _, pwd = raw.partition(":")
        return _clean(user), _clean(pwd)
    return _clean(raw), ""


def _finish(proxy_type: str, host: str, port: Any, username: str = "",
            password: str = "", secret: str = "") -> dict:
    host = _clean(host)
    if not host:
        raise ProxyParseError("没解析出代理地址")
    try:
        port_num = int(str(port or "").strip() or 0)
    except ValueError as e:
        raise ProxyParseError(f"端口不是数字：{port}") from e
    if not port_num:
        port_num = DEFAULT_PORTS.get(proxy_type, 1080)
    if not (0 < port_num < 65536):
        raise ProxyParseError(f"端口不合法：{port_num}")
    return {
        "name": "",
        "proxy_type": proxy_type,
        "addr": host,
        "port": port_num,
        "username": username,
        "password": password,
        "secret": secret,
    }


def _parse_tg_link(query: str, kind: str) -> dict:
    """tg://proxy?... / tg://socks?... 以及网页版 https://t.me/... 的查询串。"""
    params = parse_qs(query or "", keep_blank_values=True)

    def pick(*names: str) -> str:
        for name in names:
            values = params.get(name)
            if values and values[0]:
                return values[0]
        return ""

    if kind == "proxy":
        # MTProto 代理：secret 是必须的
        secret = pick("secret")
        if not secret:
            raise ProxyParseError("tg://proxy 链接里没有 secret，不是有效的 MTProto 代理")
        return _finish(
            MT_PROXY_TYPE, pick("server", "host", "addr"), pick("port"), secret=secret
        )
    # tg://socks 是 Telegram 客户端里的「SOCKS5 代理」，就是普通 socks5
    user, pwd = _clean(pick("user")), _clean(pick("pass", "password"))
    return _finish("socks5", pick("server", "host", "addr"), pick("port"), user, pwd)


def parse_proxy_input(text: str) -> dict:
    """把粘贴进来的一行代理信息拆成字段。

    支持这些写法（前三种是 Telegram 分享出来的）：

        tg://proxy?server=1.2.3.4&port=443&secret=dd0123...   MTProto 代理
        tg://socks?server=1.2.3.4&port=1080&user=u&pass=p     Telegram 的 SOCKS5
        https://t.me/proxy?... / https://t.me/socks?...       网页版，同上
        socks5://user:pass@1.2.3.4:1080                       标准 URL
        http://1.2.3.4:8080
        1.2.3.4:1080                                          只有地址端口
        1.2.3.4:1080:user:pass                                冒号分隔（代理商常这么发）
        user:pass@1.2.3.4:1080
    """
    raw = (text or "").strip()
    if not raw:
        raise ProxyParseError("先粘贴一条代理链接或地址")
    # 有些分享文本会带说明文字，取第一个看起来像链接/地址的片段
    if "\n" in raw:
        raw = raw.strip().splitlines()[0].strip()

    lowered = raw.lower()
    if lowered.startswith("tg:") or "t.me/proxy" in lowered or "t.me/socks" in lowered:
        parsed = urlparse(raw)
        query = parsed.query
        if not query and "?" in raw:          # tg://proxy?server=... 这种 urlparse 认不全
            query = raw.split("?", 1)[1]
        # 类型只看 tg:// 后面那一段（proxy / socks）。
        # 不能拿整条链接去搜关键字 —— 服务器域名叫 socks.example.com 就会被误判。
        kind_src = f"{parsed.scheme}//{parsed.netloc}{parsed.path}".lower()
        return _parse_tg_link(query, "socks" if "socks" in kind_src else "proxy")

    if "://" in raw:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in STANDARD_TYPES:
            raise ProxyParseError(
                f"不认识的代理类型 {scheme!r}，支持 http / https / socks5 / mtproto"
            )
        user, pwd = _split_auth(parsed.username and (
            f"{_clean(parsed.username)}:{_clean(parsed.password or '')}"
        ) or "")
        try:
            port = parsed.port
        except ValueError as e:
            raise ProxyParseError(f"端口不合法：{e}") from e
        return _finish(scheme, parsed.hostname or "", port, user, pwd)

    # host:port:user:pass —— 代理商列表最常见的格式
    parts = raw.split(":")
    if len(parts) == 4 and parts[1].strip().isdigit():
        return _finish("http", parts[0], parts[1], _clean(parts[2]), _clean(parts[3]))

    # user:pass@host:port
    if "@" in raw:
        auth, _, hostpart = raw.rpartition("@")
        user, pwd = _split_auth(auth)
        matched = HOST_PORT_RE.match(hostpart.strip())
        if not matched:
            raise ProxyParseError(f"「{hostpart}」不是 地址:端口 的写法")
        return _finish("http", matched.group("host"), matched.group("port"), user, pwd)

    matched = HOST_PORT_RE.match(raw)
    if matched:
        return _finish("http", matched.group("host"), matched.group("port"))

    raise ProxyParseError(
        "认不出这条代理信息。可以粘 tg://proxy?... / tg://socks?... 链接，"
        "或者 地址:端口、地址:端口:用户名:密码 这样的写法。"
    )


# ---------- 连通性测试 ----------
def _socks_connect(cfg: ProxyConfig, host: str, port: int, timeout: float):
    from python_socks import ProxyType
    from python_socks.sync import Proxy as SocksProxy

    ptype = {
        "socks5": ProxyType.SOCKS5,
        "http": ProxyType.HTTP,
        "https": ProxyType.HTTP,
    }[cfg.proxy_type]
    return SocksProxy(
        proxy_type=ptype, host=cfg.addr, port=int(cfg.port),
        username=cfg.username or None, password=cfg.password or None,
    ).connect(dest_host=host, dest_port=port, timeout=timeout)


def _probe(host: str, port: int, timeout: float, proxy: ProxyConfig | None, tls: bool) -> dict:
    """连一次并（可选）做 TLS 握手。返回 {ok, ms, detail} 或 {ok:False, error}。"""
    started = time.perf_counter()
    sock = None
    try:
        if proxy is None or not proxy.enabled:
            sock = socket.create_connection((host, port), timeout=timeout)
        else:
            sock = _socks_connect(proxy.normalized(), host, port, timeout)
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        ms = round((time.perf_counter() - started) * 1000)
        return {"ok": True, "ms": ms, "detail": f"{'TLS' if tls else 'TCP'} 握手成功 {host}:{port}"}
    except Exception as e:
        return {
            "ok": False,
            "ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _probe_mtproto(cfg: ProxyConfig, timeout: float) -> dict:
    """MTProto 代理只能测到「TCP 通不通」。

    它的握手是 MTProto 自己的一套（要用 secret 加密），拿 SOCKS5 那套去连
    只会得到一个看不懂的报错。所以这里退一步：只确认端口能连上，
    并明确告诉用户「协议级测试做不了，实际能不能用要看采集能不能起来」。
    """
    started = time.perf_counter()
    try:
        with socket.create_connection((cfg.addr, int(cfg.port)), timeout=timeout):
            pass
    except Exception as e:
        return {
            "ok": False,
            "ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "ok": True,
        "ms": round((time.perf_counter() - started) * 1000),
        "detail": "端口可达（MTProto 没法在这里做协议级测试，以实际采集为准）",
    }


# 测试目标：Telegram 的 DC（代理最该打通的地方）和 PikPak 分享站
PROBE_TARGETS = (
    ("Telegram DC", "149.154.167.51", 443, True),
    ("api.telegram.org", "api.telegram.org", 443, True),
    ("mypikpak.com", "mypikpak.com", 443, True),
)


def test_connectivity(cfg: ProxyConfig | None, timeout: float = 8.0) -> dict:
    """同步测试代理连通性。cfg 为 None 或未启用时测的是直连（做对照）。"""
    proxy = cfg.normalized() if cfg else None
    using = bool(proxy and proxy.enabled)
    if using and proxy.proxy_type == MT_PROXY_TYPE:
        result = _probe_mtproto(proxy, timeout)
        result["target"] = f"MTProto 代理端口 ({proxy.addr}:{proxy.port})"
        return {
            "using_proxy": True,
            "proxy": proxy.masked(),
            "proxy_type": proxy.proxy_type,
            "all_ok": bool(result["ok"]),
            "results": [result],
            "note": (
                "MTProto 代理只代理 Telegram 连接，代理不了 PikPak 的 HTTP 请求，"
                "所以不能当系统代理。"
                + (secret_warning(proxy.secret) and f" {secret_warning(proxy.secret)}" or "")
            ),
        }
    results = []
    for label, host, port, tls in PROBE_TARGETS:
        r = _probe(host, port, timeout, proxy if using else None, tls)
        r["target"] = f"{label} ({host}:{port})"
        results.append(r)
    return {
        "using_proxy": using,
        "proxy": proxy.masked() if using else "直连",
        "proxy_type": proxy.proxy_type if using else "",
        "all_ok": all(r["ok"] for r in results),
        "results": results,
    }
