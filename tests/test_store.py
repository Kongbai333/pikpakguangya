"""存储层测试：去重、备注聚合、搜索分页、校验结果。"""

from __future__ import annotations

import pytest

from app.store import Store, chat_id_variants, iso, utcnow

SID_A = "AAAAAAAAAAAAAAAAAAAAAA"
SID_B = "BBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def add(store, sid, *, msg_id=1, chat_id=1, remark="", code=None, chat_title="群一", when=None):
    return store.add_message(
        chat_id=chat_id,
        chat_title=chat_title,
        message_id=msg_id,
        posted_at=when or iso(utcnow()),
        raw_text=remark or f"https://mypikpak.com/s/{sid}",
        link_id=sid,
        url=f"https://mypikpak.com/s/{sid}",
        pass_code=code,
        remark=remark,
    )


def test_share_dedupe_and_remark_aggregation(store):
    add(store, SID_A, msg_id=1, remark="XP")
    add(store, SID_A, msg_id=2, remark="11")
    add(store, SID_A, msg_id=3, remark="XP")  # 重复备注只留一条

    stats = store.stats()
    assert stats["shares"] == 1
    assert stats["messages"] == 3

    item = store.query()["items"][0]
    assert item["MSG"] == "XP / 11"


def test_same_message_twice_does_not_double_count(store):
    add(store, SID_A, msg_id=1, remark="XP")
    add(store, SID_A, msg_id=1, remark="XP")  # 回溯与实时监听重叠
    assert store.stats()["messages"] == 1


def test_edit_adds_pass_code_without_double_count(store):
    add(store, SID_A, msg_id=1, remark="XP")
    assert store.query()["items"][0]["CODE"] == "无"
    add(store, SID_A, msg_id=1, remark="XP", code="f766")  # 消息被编辑补上了码
    item = store.query()["items"][0]
    assert item["CODE"] == "f766"


def test_update_pass_code_only_fills_empty(store):
    add(store, SID_A, msg_id=1)
    assert store.update_pass_code(SID_A, "f766") is True
    assert store.update_pass_code(SID_A, "zzzz") is False   # 已有值不覆盖
    assert store.query()["items"][0]["CODE"] == "f766"
    assert store.update_pass_code("不存在的id", "f766") is False


def test_two_chats_same_link_merges_remarks(store):
    add(store, SID_A, msg_id=1, chat_id=1, chat_title="群一", remark="A")
    add(store, SID_A, msg_id=1, chat_id=2, chat_title="群二", remark="B")
    item = store.query()["items"][0]
    assert item["MSG"] == "A / B"
    assert item["CHAT"] == "群一 / 群二"


def test_search_across_fields(store):
    add(store, SID_A, msg_id=1, remark="示例合集", code="f766")
    add(store, SID_B, msg_id=2, remark="别的", code="1234", chat_title="群二")

    assert store.query(q="示例")["total"] == 1
    assert store.query(q="f766")["total"] == 1
    assert store.query(q="bbbb")["total"] == 1          # link_id 不区分大小写
    assert store.query(q="mypikpak")["total"] == 2      # url
    assert store.query(q="示例 f766")["total"] == 1     # 多关键词 AND
    assert store.query(q="示例 1234")["total"] == 0
    assert store.query(q="不存在")["total"] == 0


def test_search_escapes_like_wildcards(store):
    add(store, SID_A, msg_id=1, remark="100%纯天然")
    assert store.query(q="100%")["total"] == 1
    assert store.query(q="1_0%")["total"] == 0   # _ 不应被当通配符


def test_pagination_and_sort(store):
    for i in range(1, 26):
        add(store, f"ID{i:022d}", msg_id=i, remark=f"备注{i}")
    page1 = store.query(page=1, size=10)
    assert page1["total"] == 25
    assert len(page1["items"]) == 10
    assert [it["ID"] for it in page1["items"]] == sorted([it["ID"] for it in page1["items"]], reverse=True)

    page3 = store.query(page=3, size=10)
    assert len(page3["items"]) == 5

    asc = store.query(size=5, sort="id", direction="asc")
    assert asc["items"][0]["ID"] == 1


def test_sort_whitelist_ignores_injection(store):
    add(store, SID_A, msg_id=1)
    # 非白名单的 sort 退回默认列，不报错也不拼进 SQL
    assert store.query(sort="id; DROP TABLE shares")["total"] == 1
    assert store.stats()["shares"] == 1


def test_only_valid_and_with_code_filters(store):
    add(store, SID_A, msg_id=1, code="f766")
    add(store, SID_B, msg_id=2)
    store.save_check(store.query()["items"][0]["ID"], "OK", title="文件A")

    assert store.query(only_valid=True)["total"] == 1
    assert store.query(with_code=True)["total"] == 1
    assert store.query(with_code=False)["total"] == 1


def test_save_check_and_stats(store):
    add(store, SID_A, msg_id=1)
    add(store, SID_B, msg_id=2)
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "NOT_FOUND")
    store.save_check(ids[1], "PASS_CODE_EMPTY")

    stats = store.stats()
    assert stats["gone"] == 1
    assert stats["need_code"] == 1
    assert stats["unchecked"] == 0
    assert stats["last_checked_at"]


def test_error_rows_retry_sooner(store):
    """上次是 ERROR 的链接（限流/网络抖动）应比 24 小时更早重试。"""
    add(store, SID_A, msg_id=1)
    add(store, SID_B, msg_id=2)
    rows = store.shares_to_check(limit=10)
    store.save_check(rows[0]["id"], "ERROR")

    # 默认 30 分钟内不重试
    assert [r["id"] for r in store.shares_to_check(limit=10)] == [rows[1]["id"]]
    # error_retry_minutes=0 时立刻重试，且排在「从未校验」的后面
    ids = [r["id"] for r in store.shares_to_check(limit=10, error_retry_minutes=0)]
    assert ids == [rows[1]["id"], rows[0]["id"]]


def test_shares_to_check_ordering(store):
    add(store, SID_A, msg_id=1)
    add(store, SID_B, msg_id=2)
    rows = store.shares_to_check(limit=10, recheck_after_hours=24)
    assert len(rows) == 2

    store.save_check(rows[0]["id"], "OK")
    # 刚校验过的不会再排在最前面
    again = store.shares_to_check(limit=10, recheck_after_hours=24)
    assert [r["id"] for r in again] == [rows[1]["id"]]
    # recheck_after_hours=0 时，刚校验的也算过期
    assert len(store.shares_to_check(limit=10, recheck_after_hours=0)) == 2


def test_state_kv(store):
    assert store.get_state("k") is None
    store.set_state("k", "1")
    assert store.get_state("k") == "1"
    store.set_state("k", "2")
    assert store.get_state("k") == "2"


def test_export_items_shape(store):
    add(store, SID_A, msg_id=1, remark="示例", code="f766")
    store.save_check(store.query()["items"][0]["ID"], "OK", title="文件名", file_num=3)
    items = store.export_items()
    assert len(items) == 1
    it = items[0]
    for key in ("ID", "LINK", "CODE", "MSG"):
        assert key in it
    assert it["CODE"] == "f766"
    assert it["MSG"] == "示例"
    assert it["STATUS"] == "OK"
    assert it["TITLE"] == "文件名"


def test_persist_across_instances(tmp_path):
    path = tmp_path / "p.db"
    s1 = Store(path)
    s1.init()
    add(s1, SID_A, msg_id=1)
    s2 = Store(path)
    s2.init()
    assert s2.query()["total"] == 1


# ---------- 监听白名单 ----------
def test_watched_add_list_toggle(store):
    assert store.list_watched() == []
    store.add_watched(-100123, "某群", "somegroup", "超级群")
    store.add_watched(-100456, "另一个群", "", "群")

    rows = store.list_watched()
    # added_at 精度到秒，同一批写入时实际按 chat_id 兜底排序，所以只断言集合
    assert {r["chat_id"] for r in rows} == {-100123, -100456}
    assert all(r["enabled"] is True for r in rows)
    assert set(store.watched_ids()) == {-100123, -100456}

    assert store.set_watched(-100456, False) is True
    assert store.watched_ids() == [-100123]

    assert len(store.list_watched()) == 2


def test_watched_upsert_keeps_metadata(store):
    store.add_watched(-100123, "某群", "somegroup", "群")
    # 再存一次不带元数据（比如从勾选接口过来），标题不该被清空
    store.add_watched(-100123)
    row = store.list_watched()[0]
    assert row["title"] == "某群"
    assert row["username"] == "somegroup"


# ---------- JSON 设置 ----------
def test_json_settings_roundtrip(store):
    assert store.get_json("demo.config") is None
    assert store.get_json("demo.config", {"enabled": False}) == {"enabled": False}

    store.set_json("demo.config", {"enabled": True, "port": 7890})
    assert store.get_json("demo.config")["port"] == 7890

    store.set_state("坏数据", "{不是 json")
    assert store.get_json("坏数据", "兜底") == "兜底"


# ---------- 群维度：采集统计与链接列表 ----------
def test_chat_stats_counts_per_chat(store):
    add(store, SID_A, msg_id=1, chat_id=-100111)
    add(store, SID_B, msg_id=2, chat_id=-100111)
    add(store, SID_A, msg_id=3, chat_id=-100222)          # 同一链接出现在另一个群
    add(store, SID_A, msg_id=4, chat_id=-100111)          # 同群同链接又转了一次

    st = store.chat_stats()
    assert st["-100111"]["messages"] == 3
    assert st["-100111"]["links"] == 2                    # 链接去重，消息不去重
    assert st["-100222"]["links"] == 1


def test_query_by_chat_dedupes_and_puts_newest_first(store):
    add(store, SID_A, msg_id=1, chat_id=-100111)
    add(store, SID_B, msg_id=2, chat_id=-100111)
    add(store, SID_A, msg_id=3, chat_id=-100111, remark="后面又转了一次")

    out = store.query_by_chat(-100111)
    assert out["total"] == 2                              # 去重后是 2 条
    assert out["items"][0]["LINK"].endswith(SID_A)        # 最近出现的排前面
    assert out["items"][0]["MSG"] == "后面又转了一次"
    assert out["items"][1]["LINK"].endswith(SID_B)


def test_query_by_chat_only_returns_that_chat(store):
    add(store, SID_A, msg_id=1, chat_id=-100111)
    add(store, SID_B, msg_id=2, chat_id=-100222)
    out = store.query_by_chat(-100111)
    assert out["total"] == 1
    assert out["items"][0]["LINK"].endswith(SID_A)


def test_query_by_chat_accepts_both_id_forms(store):
    """带 -100 前缀和不带前缀的 id 都能查到同一个群。"""
    add(store, SID_A, msg_id=1, chat_id=-1003739604004)
    assert store.query_by_chat(-1003739604004)["total"] == 1
    assert store.query_by_chat(3739604004)["total"] == 1


def test_query_by_chat_paginates_without_overlap(store):
    for i in range(1, 8):
        add(store, f"SID{i:020d}", msg_id=i, chat_id=-100111)
    p1 = store.query_by_chat(-100111, page=1, size=3)
    p2 = store.query_by_chat(-100111, page=2, size=3)
    assert p1["total"] == 7
    assert len(p1["items"]) == 3
    assert len(p2["items"]) == 3
    assert not ({it["ID"] for it in p1["items"]} & {it["ID"] for it in p2["items"]})


def test_query_by_chat_empty(store):
    out = store.query_by_chat(-100999)
    assert out == {"total": 0, "page": 1, "size": 50, "items": []}


def test_chat_id_variants():
    assert chat_id_variants(-1001234567890) == [-1001234567890, 1234567890]
    assert chat_id_variants(1234567890) == [1234567890, -1001234567890]
    assert chat_id_variants(-987654321) == [-987654321]   # 普通负 id 只有一种写法


# ---------- 聚合缓存：必须「写后即读得到」 ----------
def test_stats_reflects_write_immediately(store):
    """stats 有 8 秒 TTL 缓存，但写操作必须立刻让它失效。

    仪表盘是「删一批链接 → 立刻重新拉列表」的用法，如果缓存不失效，
    用户会看到刚删掉的条数还在。
    """
    add(store, SID_A, msg_id=1)
    assert store.stats()["shares"] == 1        # 先把缓存填上

    add(store, SID_B, msg_id=2)
    assert store.stats()["shares"] == 2        # 缓存必须已经被冲掉

    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.delete_shares(ids[:1])
    assert store.stats()["shares"] == 1


def test_stats_aggregates_every_status(store):
    """合并成单条 SQL 之后，各状态计数仍要和逐个 COUNT 的结果一致。"""
    add(store, "S" * 22, msg_id=1)
    add(store, "T" * 22, msg_id=2)
    add(store, "U" * 22, msg_id=3, code="f766")
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "OK")
    store.save_check(ids[1], "DELETED")        # GONE_STATUSES 里的一种

    stats = store.stats()
    assert stats["shares"] == 3
    assert stats["ok"] == 1
    assert stats["gone"] == 1
    assert stats["unchecked"] == 1
    assert stats["with_code"] == 1
    assert stats["need_code"] == 0
    assert stats["error"] == 0


def test_stats_on_empty_db_is_all_zero(store):
    """空库时 SUM(...) 返回 NULL，不能漏成 None。"""
    stats = store.stats()
    assert stats["shares"] == 0
    assert stats["messages"] == 0
    assert stats["ok"] == 0
    assert stats["gone"] == 0
    assert stats["with_code"] == 0
    assert stats["last_checked_at"] == ""
    assert stats["last_message_at"] == ""


def test_chat_stats_reflects_write_immediately(store):
    add(store, SID_A, msg_id=1, chat_id=-100111)
    assert store.chat_stats()["-100111"]["messages"] == 1

    add(store, SID_A, msg_id=2, chat_id=-100111)
    assert store.chat_stats()["-100111"]["messages"] == 2


# ---------- 一次性迁移只跑一遍 ----------
def test_shares_backfill_runs_once(tmp_path):
    """回填会重写 shares 全表，不能在每次启动时都跑。

    用「跑完把 first_seen_at 改坏，再 init 一次」来验证：如果第二次 init
    又回填了，值会被改回去。
    """
    path = tmp_path / "t.db"
    s = Store(path)
    s.init()
    add(s, SID_A, msg_id=1)

    # 手动改坏，模拟「回填过一次之后数据被别的地方改过」
    with s._write() as c:  # noqa: SLF001 - 测试直接构造场景
        c.execute("UPDATE shares SET first_seen_at = '1999-01-01T00:00:00+00:00'")

    s2 = Store(path)
    s2.init()
    item = s2.query()["items"][0]
    assert item["FIRST_SEEN"] == "1999-01-01T00:00:00+00:00"


# ---------- 秒传任务查重 ----------
def test_find_transfer_job_by_link_id_is_exact(store):
    """share_url 只能 LIKE 粗筛，但 id 是子串时不能算命中。"""
    store.create_transfer_job(
        share_url=f"https://mypikpak.com/s/{SID_A}",
        pass_code="",
        target_folder_id="",
        target_folder_path="/",
    )
    assert store.find_transfer_job_by_link_id(SID_A) is not None
    # SID_A 的后半段是另一个分享的完整 id 时，不该被判成「已提交过」
    assert store.find_transfer_job_by_link_id(SID_A[6:]) is None
    assert store.find_transfer_job_by_link_id("") is None


# ---------- 聚合缓存 ----------
def test_every_cached_aggregate_runs(store):
    """把每个走缓存的聚合方法都调两遍（第一遍 miss、第二遍 hit）。

    这类方法踩过一次坑：store.py 里 `from datetime import time` 把
    `import time` 模块遮蔽了，_cached() 里的 time.monotonic() 直接抛
    AttributeError —— 仪表盘整个打不开。跑一遍就能立刻发现。
    """
    add(store, SID_A, msg_id=1, chat_id=-100111, code="f766")
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "OK")

    for _ in range(2):
        assert store.stats()["shares"] == 1
        assert store.chat_stats()["-100111"]["messages"] == 1
        assert store.check_stats().get("OK") == 1
        assert store.source_stats()
        assert store.activity_stats(15)["series"]
        assert store.daily_stats(7)["series"]


def test_cache_hits_and_invalidates(store):
    """命中时不该重算；失效后要重算。"""
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"n": calls["n"]}

    first = store._cached("probe", loader)    # noqa: SLF001 - 直接测缓存本身
    second = store._cached("probe", loader)   # noqa: SLF001
    assert first is second
    assert calls["n"] == 1

    store._invalidate(("probe",))             # noqa: SLF001
    third = store._cached("probe", loader)    # noqa: SLF001
    assert calls["n"] == 2
    assert third["n"] == 2


def test_cache_not_poisoned_by_concurrent_write(store):
    """算结果的过程中有写提交，这份结果就不能进缓存。

    否则会拿旧数据当新的缓存 8 秒 —— 删完链接再拉列表，条数还是旧的。
    """
    state = {"n": 0}

    def loader():
        state["n"] += 1
        # 模拟「算到一半有人写了」
        if state["n"] == 1:
            store._invalidate()               # noqa: SLF001
        return {"n": state["n"]}

    store._cached("probe2", loader)           # noqa: SLF001 - 这次结果不该被缓存
    result = store._cached("probe2", loader)  # noqa: SLF001 - 所以这里会重算
    assert state["n"] == 2
    assert result["n"] == 2


# ---------- 时区分桶 ----------
def test_daily_stats_uses_configured_timezone(tmp_path):
    """同一条消息，按 UTC 和按 UTC+8 分桶会落到不同日期。

    时间戳从「昨天 16:30 UTC」推 —— 它在 +8 已经是第二天，而且两个日期
    都落在最近 7 天里，所以不用管跑测试时正好几点。

    （之前的写法拿「CST 的今天」去查 UTC 的序列，跨过 UTC 午夜那几小时
    就会 KeyError —— 时区测试自己栽在时区上。）
    """
    from datetime import datetime as dt, timedelta, timezone as dt_timezone

    cst = dt_timezone(timedelta(hours=8))
    stamp_dt = (dt.now(dt_timezone.utc) - timedelta(days=1)).replace(
        hour=16, minute=30, second=0, microsecond=0
    )
    stamp = iso(stamp_dt)
    utc_day = stamp_dt.date().isoformat()
    cst_day = stamp_dt.astimezone(cst).date().isoformat()
    assert utc_day != cst_day, "16:30 UTC 在 +8 应该已经是第二天"

    def seed(store):
        store.init()
        with store._write() as c:  # noqa: SLF001 - 直接构造指定时间的数据
            c.execute(
                "INSERT INTO messages (chat_id, chat_title, message_id, posted_at,"
                " raw_text, link_id, created_at) VALUES (1,'群',1,?,?,?,?)",
                (stamp, "x", SID_A, stamp),
            )
            c.execute(
                "INSERT INTO shares (link_id, url, first_seen_at, last_seen_at)"
                " VALUES (?,?,?,?)",
                (SID_A, f"https://mypikpak.com/s/{SID_A}", stamp, stamp),
            )

    utc_store = Store(tmp_path / "tz.db", tz=dt_timezone.utc)
    seed(utc_store)
    utc_series = {b["date"]: b for b in utc_store.daily_stats(7)["series"]}
    assert utc_series[utc_day]["messages"] == 1
    assert utc_series[cst_day]["messages"] == 0

    cst_store = Store(tmp_path / "tz8.db", tz=cst)
    seed(cst_store)
    cst_stats = cst_store.daily_stats(7)
    cst_series = {b["date"]: b for b in cst_stats["series"]}
    assert cst_series[cst_day]["messages"] == 1
    assert cst_series[utc_day]["messages"] == 0
    assert cst_stats["timezone"].startswith("UTC+")


def test_activity_stats_returns_utc_instants(store):
    """活动条的每条桶给 UTC 时刻，由前端按浏览器时区显示。"""
    add(store, SID_A, msg_id=1)
    data = store.activity_stats(15)
    assert len(data["series"]) == 15
    assert data["messages"] == 1
    assert data["series"][-1]["at"].endswith("+00:00")


# ---------- 选择性秒传 ----------
def _seed_job(store, sizes=(1000, 2000, 3000)) -> int:
    job = store.create_transfer_job(
        share_url=f"https://mypikpak.com/s/{SID_A}", pass_code="",
        target_folder_id="", target_folder_path="/",
    )
    job_id = int(job["id"])
    store.add_transfer_items(job_id, [
        {"path": f"f{i}.mkv", "name": f"f{i}.mkv", "size": size}
        for i, size in enumerate(sizes)
    ])
    return job_id


def test_transfer_items_default_to_selected(store):
    """老库升级后行为不能变：默认全选，照旧全传。"""
    job_id = _seed_job(store)
    assert store.transfer_item_selection(job_id)["selected"] == 3
    assert len(store.list_pending_transfer_items(job_id)) == 3


def test_select_transfer_items_by_size(store):
    job_id = _seed_job(store)

    store.select_transfer_items(job_id, selected=False)
    assert store.transfer_item_selection(job_id)["selected"] == 0
    assert store.list_pending_transfer_items(job_id) == []

    # 「只传小于 2.5KB 的」
    changed = store.select_transfer_items(job_id, selected=True, max_size=2500)
    assert changed == 2
    selection = store.transfer_item_selection(job_id)
    assert selection["selected"] == 2
    assert selection["selected_bytes"] == 3000

    pending = {item["path"] for item in store.list_pending_transfer_items(job_id)}
    assert pending == {"f0.mkv", "f1.mkv"}


def test_select_transfer_items_by_ids(store):
    job_id = _seed_job(store)
    store.select_transfer_items(job_id, selected=False)
    ids = [item["id"] for item in store.list_transfer_items(job_id)]
    assert store.select_transfer_items(job_id, selected=True, ids=ids[:1]) == 1
    assert store.transfer_item_selection(job_id)["selected"] == 1


def test_select_only_touches_pending_items(store):
    """已经传完的条目不该被改动 —— 勾不勾都没意义。"""
    job_id = _seed_job(store)
    ids = [item["id"] for item in store.list_transfer_items(job_id)]
    store.update_transfer_item(ids[0], status="completed")

    assert store.select_transfer_items(job_id, selected=False, ids=ids) == 2


def test_skip_unselected_items_balances_counts(store):
    """没勾选的条目在收尾时要标成跳过，否则任务结束了计数还对不上。"""
    job_id = _seed_job(store)
    # 明细插入时默认全选，所以得先全不选，再挑出小的那两条
    store.select_transfer_items(job_id, selected=False)
    store.select_transfer_items(job_id, selected=True, max_size=2500)
    assert store.transfer_item_selection(job_id)["selected"] == 2

    assert store.skip_unselected_items(job_id) == 1
    counts = store.transfer_item_counts(job_id)
    assert counts.get("skipped") == 1
    assert counts.get("pending") == 2
    # 再跑一次是空转
    assert store.skip_unselected_items(job_id) == 0


# ---------- 秒传链接入库（手动 / Bot 加的也要进链接库） ----------
def test_add_share_dedupes_by_link_id(store):
    """add_share 只登记链接、没有消息；同一个 link_id 只留一行。"""
    first = store.add_share(
        link_id=SID_A, url=f"https://mypikpak.com/s/{SID_A}",
        pass_code="f766", source="手动秒传",
    )
    assert first["share_created"] is True

    again = store.add_share(
        link_id=SID_A, url=f"https://mypikpak.com/s/{SID_A}",
        pass_code="", source="Bot 秒传",
    )
    assert again["share_created"] is False
    assert again["share_id"] == first["share_id"]

    assert store.stats()["shares"] == 1
    item = store.query(size=10)["items"][0]
    assert item["SOURCE"] == "手动秒传"        # 首次来源不被覆盖
    assert item["CODE"] == "f766"              # 空提取码不会把已有的冲掉
    assert item["MSG"] == ""                   # 没有消息，备注为空
    assert item["HIDDEN"] is False


def test_add_share_keeps_collected_source(store):
    """先采集到、后秒传：来源仍是那个群，不会变成「手动秒传」。"""
    add(store, SID_A, msg_id=1, chat_title="搬运家")
    store.add_share(
        link_id=SID_A, url=f"https://mypikpak.com/s/{SID_A}", source="手动秒传",
    )
    assert store.stats()["shares"] == 1
    assert store.query(size=10)["items"][0]["SOURCE"] == "搬运家"


def test_add_share_rejects_empty(store):
    assert store.add_share(link_id="", url="x")["share_created"] is False
    assert store.add_share(link_id=SID_A, url="")["share_created"] is False
    assert store.stats()["shares"] == 0


def test_transfer_job_records_share(store):
    """建秒传任务时顺手把链接登记进库 —— 手动和 Bot 两个入口都要。"""
    from app.transfer import TransferManager

    manager = TransferManager(store)
    manager.create_job(
        share_url=f"https://mypikpak.com/s/{SID_A}",
        pass_code="f766",
        target_folder_id="",
        target_folder_path="/",
        source="web",
    )
    items = store.query(size=10)["items"]
    assert len(items) == 1
    assert items[0]["LINK"] == f"https://mypikpak.com/s/{SID_A}"
    assert items[0]["CODE"] == "f766"
    assert items[0]["SOURCE"] == "手动秒传"

    # 同一个分享从 Bot 再来一次：不重复入库，来源也不被改
    manager.create_job(
        share_url=f"https://mypikpak.com/s/{SID_A}",
        pass_code="f766",
        target_folder_id="",
        target_folder_path="/",
        source="telegram",
    )
    assert store.stats()["shares"] == 1
    assert store.query(size=10)["items"][0]["SOURCE"] == "手动秒传"


def test_transfer_job_accepts_bare_share_id(store):
    """只粘一个分享 id 时，入库的要是能点的完整链接。"""
    from app.transfer import TransferManager

    TransferManager(store).create_job(
        share_url=SID_A, pass_code="", target_folder_id="",
        target_folder_path="/", source="web",
    )
    assert store.query(size=10)["items"][0]["LINK"] == f"https://mypikpak.com/s/{SID_A}"
