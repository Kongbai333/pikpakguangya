"""校验器测试：用真实抓取的分享页做夹具，离线可跑。"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import pikpak
from app.proxy import ProxyConfig

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _html(nuxt_raw: str = "", title: str = "") -> str:
    parts = ["<html><head>"]
    if title:
        parts.append(f"<title>{title}</title>")
    parts.append("</head><body>")
    if nuxt_raw:
        parts.append(
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{nuxt_raw}</script>'
        )
    parts.append("</body></html>")
    return "".join(parts)


# ---------- 真实夹具（需提取码的分享页） ----------
def test_real_fixture_need_code():
    data = json.loads((FIXTURES / "need_code.json").read_text(encoding="utf-8"))
    parsed = pikpak.parse_share_html(_html(data["nuxt_raw"], data["html_title"]))
    assert parsed["status"] == pikpak.STATUS_NEED_CODE
    assert parsed["source"] == "nuxt"
    assert data["html_title"] == "Shared by 8**8 | PikPak"


def test_real_fixture_title_fallback_without_payload():
    """就算 __NUXT_DATA__ 被前端改版删掉，<title> 兜底也要给出同样结论。"""
    data = json.loads((FIXTURES / "need_code.json").read_text(encoding="utf-8"))
    parsed = pikpak.parse_share_html(_html("", data["html_title"]))
    assert parsed["status"] == pikpak.STATUS_NEED_CODE
    assert parsed["source"] == "title"


# ---------- <title> 兜底：三种形态都来自真实抓取 ----------
@pytest.mark.parametrize("title,expected,expected_name", [
    ("示例合集 Shared by 就**是 | PikPak", pikpak.STATUS_OK, "示例合集"),
    ("Shared by 8**8 | PikPak", pikpak.STATUS_NEED_CODE, ""),
    ("Shared by  | PikPak", pikpak.STATUS_GONE, ""),
])
def test_title_fallback(title, expected, expected_name):
    parsed = pikpak.parse_share_html(_html("", title))
    assert parsed["status"] == expected
    assert parsed["title"] == expected_name


# ---------- devalue 解引用 ----------
def test_resolve_flat_payload():
    # [root, {"pinia": 2}, {"share": 3}, {"data": 4}, {"share_status": 5, "file_num": 6, "title": 7}, "OK", "2", "文件名"]
    arr = [
        {"pinia": 1},
        {"share": 2},
        {"data": 3},
        {"share_status": 4, "file_num": 5, "title": 6, "expiration_left_seconds": 7},
        "OK", "2", "文件名", "-1",
    ]
    parsed = pikpak.parse_share_html(_html(json.dumps(arr, ensure_ascii=False)))
    assert parsed["status"] == "OK"
    assert parsed["title"] == "文件名"
    assert parsed["file_num"] == 2
    assert parsed["expire_seconds"] == -1
    assert parsed["source"] == "nuxt"


def test_resolve_handles_reactive_wrapper():
    arr = [
        ["ShallowReactive", 1],
        {"pinia": 2},
        ["ShallowReactive", 3],
        {"share": 4},
        {"data": 5},
        {"share_status": 6},
        "NOT_FOUND",
    ]
    parsed = pikpak.parse_share_html(_html(json.dumps(arr)))
    assert parsed["status"] == "NOT_FOUND"


def test_broken_payload_falls_back_to_title():
    parsed = pikpak.parse_share_html(_html("{不是合法 JSON", "文件A Shared by 张三 | PikPak"))
    assert parsed["status"] == pikpak.STATUS_OK
    assert parsed["title"] == "文件A"
    assert parsed["source"] == "title"


def test_empty_html_is_gone():
    assert pikpak.parse_share_html("")["status"] == pikpak.STATUS_GONE


# ---------- check_share ----------
def test_check_share_parses_response(monkeypatch):
    html = _html("", "文件A Shared by 张三 | PikPak")
    monkeypatch.setattr(pikpak, "fetch_share_html", lambda url, timeout=25, proxy=None: html)
    res = pikpak.check_share("https://mypikpak.com/s/ABCD1234EFGH")
    assert res.status == pikpak.STATUS_OK
    assert res.title == "文件A"
    assert res.link_id == "ABCD1234EFGH"
    assert res.is_valid is True


def test_check_share_passes_proxy_through(monkeypatch):
    seen = {}

    def fake_fetch(url, timeout=25, proxy=None):
        seen["url"] = url
        seen["proxy"] = proxy
        return _html("", "文件A Shared by 张三 | PikPak")

    monkeypatch.setattr(pikpak, "fetch_share_html", fake_fetch)
    proxy = ProxyConfig(enabled=True, proxy_type="socks5", addr="127.0.0.1", port=7890)
    pikpak.check_share("https://mypikpak.com/s/ABCD1234EFGH", proxy=proxy)
    assert seen["url"] == "https://mypikpak.com/s/ABCD1234EFGH"
    assert seen["proxy"] is proxy


def test_check_share_network_error_is_not_marked_gone(monkeypatch):
    def boom(url, timeout=25, proxy=None):
        raise OSError("connection reset")

    monkeypatch.setattr(pikpak, "fetch_share_html", boom)
    res = pikpak.check_share("https://mypikpak.com/s/ABCD1234EFGH")
    assert res.status == pikpak.STATUS_ERROR
    assert res.is_valid is False
    assert "OSError" in res.error


@pytest.mark.parametrize("raw,expected", [
    ("https://mypikpak.com/s/ABCD1234", "ABCD1234"),
    ("mypikpak.com/s/ABCD1234?pass_code=x", "ABCD1234"),
    ("ABCD1234", "ABCD1234"),
])
def test_link_id_of(raw, expected):
    assert pikpak.link_id_of(raw) == expected


# ---------- 需要真实网络（默认跳过，--run-network 开启） ----------
@pytest.mark.network
def test_live_not_found_is_stable():
    res = pikpak.check_share("ZZnotexist_zzz_123")
    assert res.status == pikpak.STATUS_GONE


@pytest.mark.network
def test_live_known_links_return_known_status():
    # 这里刻意用不存在的 id：断言只要求「不崩、落到四种状态之一」，
    # 而真实分享 id 是别人的资源，不该写进公开仓库。
    # 想验证自己手上的链接，把 id 换成你的再跑。
    for sid in ("EXAMPLEShareId000000000001", "ZZnotexist_zzz_123"):
        res = pikpak.check_share(sid)
        assert res.status in (pikpak.STATUS_OK, pikpak.STATUS_NEED_CODE, pikpak.STATUS_GONE,
                              pikpak.STATUS_ERROR)
