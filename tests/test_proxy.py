"""代理配置：粘贴解析、类型能力、系统代理。

不碰网络 —— 连通性测试单独测，这里只测纯逻辑。
"""

from __future__ import annotations

import pytest

from app import proxy as proxy_module
from app.proxy import ProxyConfig, ProxyParseError, parse_proxy_input
from app.store import Store

# 32 位十六进制的合法 secret
SECRET = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


# ---------- 粘贴解析：Telegram 那几种 ----------
def test_parse_tg_mtproto_link():
    """tg://proxy?...&secret=... 是 MTProto 代理，secret 是必须的。"""
    got = parse_proxy_input(f"tg://proxy?server=1.2.3.4&port=443&secret=dd{SECRET}")
    assert got["proxy_type"] == "mtproto"
    assert got["addr"] == "1.2.3.4"
    assert got["port"] == 443
    assert got["secret"] == f"dd{SECRET}"


def test_parse_tg_proxy_without_secret_is_rejected():
    with pytest.raises(ProxyParseError) as e:
        parse_proxy_input("tg://proxy?server=1.2.3.4&port=443")
    assert "secret" in str(e.value)


def test_parse_tg_socks_link():
    """tg://socks 是 Telegram 客户端里的「SOCKS5 代理」，就是普通 socks5。"""
    got = parse_proxy_input("tg://socks?server=1.2.3.4&port=1080&user=u&pass=p")
    assert got["proxy_type"] == "socks5"
    assert (got["addr"], got["port"]) == ("1.2.3.4", 1080)
    assert (got["username"], got["password"]) == ("u", "p")


def test_parse_tme_web_links():
    """网页版分享出来的 https://t.me/proxy?... 和 tg:// 是同一件事。"""
    mt = parse_proxy_input(f"https://t.me/proxy?server=5.6.7.8&port=8443&secret={SECRET}")
    assert (mt["proxy_type"], mt["addr"], mt["port"]) == ("mtproto", "5.6.7.8", 8443)

    socks = parse_proxy_input("https://t.me/socks?server=5.6.7.8&port=1080")
    assert (socks["proxy_type"], socks["addr"]) == ("socks5", "5.6.7.8")


def test_parse_tg_proxy_with_socks_in_hostname():
    """服务器域名里带 socks 不能被误判成 tg://socks。

    类型只能看 tg:// 后面那一段，不能拿整条链接去搜关键字。
    """
    got = parse_proxy_input(f"tg://proxy?server=socks.example.com&port=443&secret={SECRET}")
    assert got["proxy_type"] == "mtproto"
    assert got["addr"] == "socks.example.com"


# ---------- 粘贴解析：通用写法 ----------
@pytest.mark.parametrize("raw,expected", [
    ("socks5://user:p%40ss@1.2.3.4:1080", ("socks5", "1.2.3.4", 1080, "user", "p@ss")),
    ("http://1.2.3.4:8080", ("http", "1.2.3.4", 8080, "", "")),
    ("1.2.3.4:1080", ("http", "1.2.3.4", 1080, "", "")),
    ("1.2.3.4:1080:bob:secret", ("http", "1.2.3.4", 1080, "bob", "secret")),
    ("bob:secret@1.2.3.4:1080", ("http", "1.2.3.4", 1080, "bob", "secret")),
])
def test_parse_common_forms(raw, expected):
    got = parse_proxy_input(raw)
    assert (
        got["proxy_type"], got["addr"], got["port"], got["username"], got["password"]
    ) == expected


@pytest.mark.parametrize("raw", ["", "   ", "今天天气不错", "ftp://1.2.3.4:21", "1.2.3.4"])
def test_parse_rejects_garbage(raw):
    with pytest.raises(ProxyParseError):
        parse_proxy_input(raw)


# ---------- 类型能力 ----------
def test_mtproto_cannot_proxy_http():
    """MTProto 代理只代理 Telegram，不能当系统代理。"""
    cfg = ProxyConfig(
        enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=443, secret=SECRET
    )
    assert cfg.supports_http() is False
    assert cfg.to_url() == ""
    assert cfg.httpx_proxy() is None


def test_mtproto_telethon_kwargs_uses_mtproxy_connection():
    cfg = ProxyConfig(
        enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=443, secret=SECRET
    )
    kwargs = cfg.telethon_kwargs()
    assert kwargs["proxy"] == ("1.2.3.4", 443, SECRET)
    assert kwargs["connection"].__name__ == "ConnectionTcpMTProxyRandomizedIntermediate"


def test_standard_proxy_telethon_kwargs_unchanged():
    cfg = ProxyConfig(enabled=True, proxy_type="socks5", addr="1.2.3.4", port=1080)
    kwargs = cfg.telethon_kwargs()
    assert kwargs["proxy"]["proxy_type"] == "socks5"
    assert "connection" not in kwargs          # 标准代理不走 MTProxy 那条路
    assert cfg.supports_http() is True


def test_mtproto_validate_requires_valid_secret():
    with pytest.raises(ValueError) as e:
        ProxyConfig(enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=443).validate()
    assert "secret" in str(e.value)

    with pytest.raises(ValueError):
        ProxyConfig(
            enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=443, secret="abc"
        ).validate()

    # 32 位十六进制（可带 dd / ee 前缀）才是合法的
    for secret in (SECRET, f"dd{SECRET}", f"ee{SECRET}"):
        ProxyConfig(
            enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=443, secret=secret
        ).validate()


def test_ee_secret_is_flagged():
    """ee 开头是 fake-TLS，Telethon 不做域名伪装，得提醒用户。"""
    assert proxy_module.secret_warning(f"ee{SECRET}") != ""
    assert proxy_module.secret_warning(f"dd{SECRET}") == ""
    assert proxy_module.secret_warning(SECRET) == ""
    assert proxy_module.secret_warning("") == ""


def test_mtproto_default_port_is_443():
    """tg://proxy 链接经常不带 port，443 是通用默认值。"""
    cfg = ProxyConfig(enabled=True, proxy_type="mtproto", addr="1.2.3.4", port=0)
    assert cfg.normalized().port == 443


# ---------- 系统代理 ----------
def test_system_proxy_defaults_to_direct(store):
    assert proxy_module.system_proxy_id(store) == 0
    assert proxy_module.system_proxy(store) is None


def test_system_proxy_round_trip(store):
    row = store.add_proxy(name="系统", proxy_type="socks5", addr="1.2.3.4", port=1080)
    assert proxy_module.set_system_proxy(store, row["id"]) == row["id"]
    cfg = proxy_module.system_proxy(store)
    assert cfg is not None and cfg.addr == "1.2.3.4"

    proxy_module.set_system_proxy(store, 0)
    assert proxy_module.system_proxy(store) is None


def test_system_proxy_rejects_unknown_id(store):
    with pytest.raises(ValueError):
        proxy_module.set_system_proxy(store, 999)


def test_system_proxy_ignores_mtproto(store):
    """MTProto 代理代理不了 HTTP，配成系统代理等于让所有请求静默失败。"""
    row = store.add_proxy(
        name="mt", proxy_type="mtproto", addr="1.2.3.4", port=443, secret=SECRET
    )
    proxy_module.set_system_proxy(store, row["id"])
    assert proxy_module.system_proxy(store) is None


def test_proxy_secret_survives_round_trip(store):
    row = store.add_proxy(
        name="mt", proxy_type="mtproto", addr="1.2.3.4", port=443, secret=SECRET
    )
    assert store.get_proxy(row["id"])["secret"] == SECRET
    store.update_proxy(
        row["id"], name="mt2", proxy_type="mtproto", addr="1.2.3.4", port=8443,
        secret=f"dd{SECRET}",
    )
    got = store.get_proxy(row["id"])
    assert got["secret"] == f"dd{SECRET}"
    assert got["port"] == 8443


# ---------- 老库升级：把当前账号的代理认成系统代理 ----------
def test_adopt_system_proxy_takes_active_account_proxy(store):
    """以前全局出口就是「当前使用」账号的代理，别让它升级后突然变直连。"""
    proxy = store.add_proxy(name="老代理", proxy_type="socks5", addr="1.2.3.4", port=1080)
    # 第一个账号自动是「当前使用」，不用额外设置
    store.add_account(phone="+8613800000000", session="a.session", proxy_id=proxy["id"])

    assert proxy_module.adopt_system_proxy(store) is True
    assert proxy_module.system_proxy_id(store) == proxy["id"]
    # 只认一次
    assert proxy_module.adopt_system_proxy(store) is False


def test_adopt_system_proxy_does_not_override_explicit_direct(store):
    """用户明确选了直连（存的是 "0"）之后，不能再被自动改回去。"""
    proxy = store.add_proxy(name="老代理", proxy_type="socks5", addr="1.2.3.4", port=1080)
    store.add_account(phone="+8613800000000", session="a.session", proxy_id=proxy["id"])
    proxy_module.set_system_proxy(store, 0)

    assert proxy_module.adopt_system_proxy(store) is False
    assert proxy_module.system_proxy_id(store) == 0


def test_adopt_system_proxy_noop_without_accounts(store):
    assert proxy_module.adopt_system_proxy(store) is False
