"""消息解析与提取码合并的测试。用例来自真实群里见过的形态。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.parser import (
    CodeMerger,
    build_remark,
    extract_links,
    find_pass_code,
    is_bare_code,
    keep_message,
    parse_message,
)

SID = "EXAMPLEShareId000000000001"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------- 链接抽取 ----------
@pytest.mark.parametrize("text,expected", [
    (f"https://mypikpak.com/s/{SID}", SID),
    (f"http://mypikpak.com/s/{SID}", SID),
    (f"mypikpak.com/s/{SID}", SID),
    (f"www.mypikpak.com/s/{SID}", SID),
    (f"https://m.mypikpak.com/s/{SID}", SID),
    (f"https://pikpak.com/s/{SID}", SID),
    (f"看看这个 https://mypikpak.com/s/{SID}?pass_code=1234 很好", SID),
    (f"【分享】https://mypikpak.com/s/{SID}\n提取码：f766", SID),
])
def test_extract_links(text, expected):
    hits = extract_links(text)
    assert len(hits) == 1
    assert hits[0].link_id == expected
    assert hits[0].url.startswith("https://")


def test_extract_links_normalizes_host_and_dedupes():
    hits = extract_links(f"https://www.mypikpak.com/s/{SID} 和 pikpak.com/s/{SID}")
    assert len(hits) == 1
    assert hits[0].url == f"https://mypikpak.com/s/{SID}"


def test_extract_links_multi_keeps_order():
    hits = extract_links("https://mypikpak.com/s/AAAA1111 https://mypikpak.com/s/BBBB2222")
    assert [h.link_id for h in hits] == ["AAAA1111", "BBBB2222"]


@pytest.mark.parametrize("text", [
    "https://example.com/s/EXAMPLEShareId000000000001",
    "https://mypikpak.com/share/xxxx",
    "mypikpak.com/s/ab",  # 太短，不是有效 share id
    "今天天气不错",
])
def test_extract_links_negative(text):
    assert extract_links(text) == []


# ---------- 提取码 ----------
@pytest.mark.parametrize("text,expected", [
    ("提取码：f766", "f766"),
    ("提取码: f766", "f766"),
    ("提取码 f766", "f766"),
    ("密码：1234", "1234"),
    ("访问码 ABCD", "ABCD"),
    ("code: 9z8y", "9z8y"),
    ("f766", "f766"),                      # 整条消息就是一个码
    ("提取码：无", None),
    ("密码：不需要", None),
    ("no pass code", None),
])
def test_find_pass_code(text, expected):
    assert find_pass_code(text) == expected


def test_pass_code_from_url_wins():
    text = f"https://mypikpak.com/s/{SID}?pass_code=ab12 提取码：zzzz"
    assert find_pass_code(text) == "ab12"


def test_is_bare_code_rejects_link_message():
    assert is_bare_code("f766") == "f766"
    assert is_bare_code("f766 ") == "f766"
    assert is_bare_code(f"https://mypikpak.com/s/{SID}") is None
    assert is_bare_code("f766 看看") is None
    assert is_bare_code("无") is None


# ---------- 备注 ----------
@pytest.mark.parametrize("text,expected", [
    (f"示例合集 https://mypikpak.com/s/{SID} 提取码：f766", "示例合集"),
    (f"https://mypikpak.com/s/{SID}\nEXAMPLE-0001-C\n提取码：f766", "EXAMPLE-0001-C"),
    (f"[示例合集](https://mypikpak.com/s/{SID})", "示例合集"),
    (f"@someone 看看 https://mypikpak.com/s/{SID}", "看看"),
    (f"https://mypikpak.com/s/{SID}", ""),
    (f"https://mypikpak.com/s/{SID}\nf766", ""),
])
def test_build_remark(text, expected):
    assert build_remark(text) == expected


def test_build_remark_truncates():
    long = "字" * 500
    assert len(build_remark(f"{long} https://mypikpak.com/s/{SID}")) == 200


# ---------- parse_message ----------
def test_parse_message_full():
    shares = parse_message(f"示例合集 https://mypikpak.com/s/{SID} 提取码：f766")
    assert len(shares) == 1
    s = shares[0]
    assert s.link_id == SID
    assert s.pass_code == "f766"
    assert s.remark == "示例合集"
    assert s.update_only is False


def test_parse_message_multi_link_shares_one_code():
    shares = parse_message("https://mypikpak.com/s/AAAA1111 https://mypikpak.com/s/BBBB2222 提取码：f766")
    assert [s.pass_code for s in shares] == ["f766", "f766"]


def test_parse_message_without_link():
    assert parse_message("今天群里没人发东西") == []


# ---------- 关键词过滤 ----------
def test_keep_message():
    assert keep_message("示例 分享", include=["示例"], exclude=[]) is True
    assert keep_message("别的", include=["示例"], exclude=[]) is False
    assert keep_message("示例 广告", include=[], exclude=["广告"]) is False
    assert keep_message("随便", include=[], exclude=[]) is True


# ---------- 跨消息合并提取码 ----------
def test_merger_attaches_later_code():
    m = CodeMerger(window_seconds=90)
    first = m.observe(1, 100, f"https://mypikpak.com/s/{SID}", NOW)
    assert first[0].pass_code is None

    second = m.observe(1, 100, "f766", NOW + timedelta(seconds=20))
    assert len(second) == 1
    assert second[0].link_id == SID
    assert second[0].pass_code == "f766"
    assert second[0].update_only is True


def test_merger_respects_window():
    m = CodeMerger(window_seconds=30)
    m.observe(1, 100, f"https://mypikpak.com/s/{SID}", NOW)
    assert m.observe(1, 100, "f766", NOW + timedelta(seconds=31)) == []


def test_merger_isolates_chat_and_sender():
    m = CodeMerger(window_seconds=90)
    m.observe(1, 100, f"https://mypikpak.com/s/{SID}", NOW)
    assert m.observe(2, 100, "f766", NOW) == []      # 别的群
    assert m.observe(1, 999, "f766", NOW) == []      # 别的人
    assert len(m.observe(1, 100, "f766", NOW)) == 1


def test_merger_ignores_code_when_link_already_has_one():
    m = CodeMerger(window_seconds=90)
    m.observe(1, 100, f"https://mypikpak.com/s/{SID} 提取码：aaaa", NOW)
    assert m.observe(1, 100, "bbbb", NOW + timedelta(seconds=5)) == []
