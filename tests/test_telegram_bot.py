"""Telegram Bot 的命令与回调。

只测「拿 store 就能算出结果」的那部分（文本、按钮、分页），
不发真实 Telegram 请求 —— 网络那一层靠 _api_post 收口，这里不碰。
"""

from __future__ import annotations

import pytest

from app import config as config_module
from app.store import Store, iso, utcnow
from app.telegram_bot import TelegramBotRunner

SID_A = "AAAAAAAAAAAAAAAAAAAAAA"
SID_B = "BBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "bot.db")
    s.init()
    return s


@pytest.fixture()
def bot(store):
    cfg = config_module.Config()
    return TelegramBotRunner(store, cfg)


def add(store, sid, *, msg_id=1, chat_id=1, remark="", code=None, title=""):
    store.add_message(
        chat_id=chat_id, chat_title="测试群", message_id=msg_id,
        posted_at=iso(utcnow()), raw_text=remark or f"https://mypikpak.com/s/{sid}",
        link_id=sid, url=f"https://mypikpak.com/s/{sid}", pass_code=code, remark=remark,
    )
    if title:
        row = store.query(size=50)["items"]
        target = next((it for it in row if it["LINK"].endswith(sid)), None)
        if target:
            store.save_check(target["ID"], "OK", title=title)


# ---------- 命令解析 ----------
def test_parse_command_handles_bot_suffix_and_args():
    parse = TelegramBotRunner._parse_command
    assert parse("/tasks") == ("tasks", "")
    assert parse("/tasks@my_bot") == ("tasks", "")
    assert parse("/search 示例合集") == ("search", "示例合集")
    assert parse("  /HELP  ") == ("help", "")
    assert parse("普通文本") == ("", "")


def test_every_registered_command_has_a_reply(bot):
    """注册到 Telegram 菜单里的命令必须真的有实现，不能点了回「未知命令」。"""
    from app.telegram_bot import BOT_COMMANDS

    for item in BOT_COMMANDS:
        text, _ = bot._command_reply(item["command"], "", 1)
        assert "未知命令" not in text, f"/{item['command']} 没有实现"


def test_cancel_is_not_an_unknown_command(bot):
    text, _ = bot._command_reply("cancel", "", 1)
    assert "未知命令" not in text


# ---------- 任务列表 ----------
def _make_job(store, status: str) -> int:
    job = store.create_transfer_job(
        share_url=f"https://mypikpak.com/s/{SID_A}", pass_code="",
        target_folder_id="", target_folder_path="/",
    )
    job_id = int(job["id"])
    store.update_transfer_job(job_id, status=status)
    return job_id


def test_tasks_text_lists_jobs_without_buttons(bot, store):
    """任务列表只列出来，不带操作按钮 —— 按钮会把消息撑得很长。"""
    running = _make_job(store, "running")
    finished = _make_job(store, "finished")

    text, keyboard = bot._command_reply("tasks", "", 1)
    assert keyboard is None
    assert f"#{running}" in text and f"#{finished}" in text
    # 状态和进度都要能看到
    assert "运行中" in text and "已完成" in text


def test_tasks_text_empty(bot):
    text, keyboard = bot._command_reply("tasks", "", 1)
    assert keyboard is None
    assert "暂无秒传任务" in text


# ---------- 搜索与分页 ----------
def test_search_page_returns_items_for_action_buttons(bot, store):
    add(store, SID_A, msg_id=1, remark="示例合集", title="文件A")
    text, page, total_pages, items = bot._search_page("示例合集")
    assert page == 1 and total_pages == 1
    assert len(items) == 1
    assert items[0]["ID"]  # 有 ID 才能生成按钮
    assert "示例合集" in text

    keyboard = bot._search_keyboard("sid", items, page, total_pages)
    assert keyboard is not None
    flat = [b for row in keyboard["inline_keyboard"] for b in row]
    assert {b["callback_data"].split(":")[1] for b in flat} == {"tr", "cp"}


def test_search_keyboard_has_no_pagination_on_single_page(bot, store):
    add(store, SID_A, msg_id=1, remark="唯一一条")
    _, page, total_pages, items = bot._search_page("唯一")
    keyboard = bot._search_keyboard("sid", items, page, total_pages)
    flat = [b for row in keyboard["inline_keyboard"] for b in row]
    assert all(not b["callback_data"].startswith("search:") for b in flat)


def test_search_keyboard_none_when_nothing_to_offer(bot):
    assert bot._search_keyboard("sid", [], 1, 1) is None


def test_status_filter_lists_without_keyword(bot, store):
    add(store, SID_A, msg_id=1)
    add(store, SID_B, msg_id=2)
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "NOT_FOUND")

    text, page, total_pages, items = bot._search_page("", 1, "NOT_FOUND")
    assert len(items) == 1
    assert "已失效" in text


def test_search_page_without_keyword_or_status_shows_usage(bot):
    text, _, _, items = bot._search_page("")
    assert "用法" in text
    assert items == []


# ---------- 链接统计的分类按钮 ----------
def test_links_view_buttons_match_counts(bot, store):
    """按钮上写的条数必须等于点进去看到的条数 —— 两处用同一份计数。"""
    add(store, SID_A, msg_id=1)
    add(store, SID_B, msg_id=2)
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "NOT_FOUND")

    text, keyboard = bot._links_view()
    flat = [b for row in keyboard["inline_keyboard"] for b in row]
    gone = next(b for b in flat if b["callback_data"] == "st:NOT_FOUND")
    assert gone["text"].endswith("1")

    _, _, _, items = bot._search_page("", 1, "NOT_FOUND")
    assert len(items) == 1


def test_links_view_without_data_has_no_keyboard(bot):
    text, keyboard = bot._links_view()
    assert keyboard is None
    assert "总链接" in text


def test_link_status_rows_are_all_queryable(bot, store):
    """分类表里的每个状态都要能被 query_admin 认出来。"""
    for status, _ in TelegramBotRunner.LINK_STATUS_ROWS:
        result = store.query_admin(status=status)
        assert "total" in result, f"{status} 查不动"
