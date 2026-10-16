"""API 测试（FastAPI TestClient，不启动真实服务）。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import config as config_module
from app.api import create_app
from app.store import Store, iso, utcnow


@pytest.fixture()
def client(tmp_path):
    cfg = config_module.Config(
        storage=config_module.StorageConfig(db=str(tmp_path / "api.db")),
        checker=config_module.CheckerConfig(enabled=False),  # 测试里不起后台校验
        admin=config_module.AdminConfig(password="test-admin"),
    )
    app = create_app(cfg)
    store: Store = app.state.store
    _seed(store)
    with TestClient(app) as c:
        c.post("/api/web-auth/login", json={"password": "test-admin"})
        yield c


def _seed(store: Store) -> None:
    now = iso(utcnow())
    for i, (sid, code, remark) in enumerate([
        ("AAAAAAAAAAAAAAAAAAAAAA", "f766", "示例合集"),
        ("BBBBBBBBBBBBBBBBBBBBBB", "1234", "EXAMPLE-0001-C"),
        ("CCCCCCCCCCCCCCCCCCCCCC", None, "嘤嘤嘤"),
    ], start=1):
        store.add_message(
            chat_id=-100123, chat_title="测试群", message_id=i, posted_at=now,
            raw_text=remark, link_id=sid, url=f"https://mypikpak.com/s/{sid}",
            pass_code=code, remark=remark,
        )
    ids = [it["ID"] for it in store.query(size=10)["items"]]
    store.save_check(ids[0], "OK", title="文件名A", file_num=2, expire_seconds=-1)


def test_items_default_id_desc(client):
    body = client.get("/api/items").json()
    assert body["status"] == 200
    assert body["total"] == 3
    assert body["page"] == 1
    ids = [it["ID"] for it in body["items"]]
    assert ids == sorted(ids, reverse=True)          # 默认 ID 倒序
    assert set(body["stats"]) >= {"shares", "ok", "unchecked"}


def test_items_search_and_pagination(client):
    assert client.get("/api/items", params={"q": "示例"}).json()["total"] == 1
    assert client.get("/api/items", params={"q": "f766"}).json()["total"] == 1
    assert client.get("/api/items", params={"q": "示例 f766"}).json()["total"] == 1
    assert client.get("/api/items", params={"q": "没有这个"}).json()["total"] == 0

    page1 = client.get("/api/items", params={"size": 2, "page": 1}).json()
    page2 = client.get("/api/items", params={"size": 2, "page": 2}).json()
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    assert {it["ID"] for it in page1["items"]}.isdisjoint({it["ID"] for it in page2["items"]})


def test_items_sort_and_filters(client):
    asc = client.get("/api/items", params={"sort": "id", "dir": "asc"}).json()
    assert [it["ID"] for it in asc["items"]] == sorted(it["ID"] for it in asc["items"])

    assert client.get("/api/items", params={"only_valid": True}).json()["total"] == 1
    assert client.get("/api/items", params={"with_code": True}).json()["total"] == 2


def test_item_fields_contract(client):
    """页面依赖这些字段名，缺了页面就渲染不出来。"""
    item = client.get("/api/items").json()["items"][0]
    for key in ("ID", "LINK", "CODE", "MSG", "STATUS", "TITLE", "CHAT"):
        assert key in item
    assert item["CODE"] in ("无", "f766", "1234")


def test_public_items_hide_source(client):
    """前台不返回来源：它是采集到这条链接的群名，属于库主的隐私。

    后端直接不给这个字段，而不是前端不渲染 —— 不然打开 devtools 照样看得到。
    管理页走 /api/admin/links，那边要保留。
    """
    items = client.get("/api/items").json()["items"]
    assert items and all("SOURCE" not in it for it in items)
    admin = client.get("/api/admin/links").json()["items"]
    assert any(it.get("SOURCE") for it in admin)


# ---------- 前台浏览页总开关 ----------
def test_public_web_toggle_closes_front_and_api(client):
    assert client.get("/api/web-public").json()["enabled"] is True
    assert client.get("/api/items").status_code == 200

    assert client.put("/api/web-public", json={"enabled": False}).json()["enabled"] is False

    # 页面：根地址换成「已关闭」提示，状态码 404（别让搜索引擎当正常页收录）
    res = client.get("/")
    assert res.status_code == 404
    assert "浏览页已关闭" in res.text
    # 公开接口一起拒 —— 只藏页面不拦接口等于没关
    assert client.get("/api/items").status_code == 403
    assert client.get("/api/stats").status_code == 403
    # 导出的静态快照也要停掉
    assert client.get("/data.js").status_code == 404

    # 管理后台和登录不受影响
    assert client.get("/admin").status_code == 200
    assert client.get("/api/admin/links").status_code == 200
    assert client.get("/api/web-auth/session").status_code == 200

    assert client.put("/api/web-public", json={"enabled": True}).json()["enabled"] is True
    assert client.get("/api/items").status_code == 200
    assert client.get("/").status_code == 200


def test_closed_page_has_no_admin_entry(client):
    """关闭页是给外人看的，上面不能出现进后台的入口 —— 等于告诉访客后台在哪儿。

    库主自己走 `/admin`（另见 test_admin_entry_serves_spa_when_front_closed）。
    """
    client.put("/api/web-public", json={"enabled": False})

    page = client.get("/")
    assert page.status_code == 404
    assert "浏览页已关闭" in page.text
    assert not re.search(r"<a\s", page.text), "关闭页上不该有链接"
    assert "admin" not in page.text.lower(), "关闭页不该透露后台地址"


def test_admin_entry_serves_spa_when_front_closed(client):
    """前台关掉后，`/admin` 仍然要吐前端入口，否则库主自己也被挡在外面。"""
    client.put("/api/web-public", json={"enabled": False})

    entry = client.get("/admin")
    assert entry.status_code == 200
    assert "浏览页已关闭" not in entry.text


@pytest.mark.parametrize("enabled", [True, False])
def test_admin_stats_survives_front_toggle(client, enabled):
    """管理后台的统计走自己的接口，不受前台总开关影响。

    以前两边共用 `/api/stats`：前台一关，仪表盘的数据跟着一起 403，库主自己
    反而看不到。现在前台 `/api/stats` 关掉就拒，后台 `/api/admin/stats` 照常。
    """
    client.put("/api/web-public", json={"enabled": enabled})

    admin = client.get("/api/admin/stats")
    assert admin.status_code == 200, admin.text
    assert admin.json()["stats"]["shares"] == 3

    assert client.get("/api/stats").status_code == (200 if enabled else 403)


def test_public_web_toggle_requires_admin(client):
    """开关本身是管理接口，没登录不能改。"""
    client.post("/api/web-auth/logout")
    assert client.put("/api/web-public", json={"enabled": False}).status_code == 401
    assert client.get("/api/web-public").status_code == 401


# ---------- 代理：粘贴解析 + 系统代理 ----------
SECRET = "0" * 32


def test_proxy_parse_endpoint(client):
    """粘一条链接要能拆成字段，给添加表单做预览。"""
    body = client.post(
        "/api/proxies/parse",
        json={"raw": f"tg://proxy?server=1.2.3.4&port=443&secret=dd{SECRET}"},
    ).json()
    assert body["proxy_type"] == "mtproto"
    assert (body["addr"], body["port"]) == ("1.2.3.4", 443)
    assert body["secret"] == f"dd{SECRET}"

    socks = client.post(
        "/api/proxies/parse", json={"raw": "tg://socks?server=5.6.7.8&port=1080&user=u"}
    ).json()
    assert (socks["proxy_type"], socks["addr"], socks["username"]) == ("socks5", "5.6.7.8", "u")

    assert client.post("/api/proxies/parse", json={"raw": "看不懂的东西"}).status_code == 400
    assert client.post("/api/proxies/parse", json={"raw": ""}).status_code == 400


def test_proxy_add_accepts_pasted_link(client):
    """添加接口带 raw 时直接按链接解析，不用前端先拆好。"""
    p = client.post("/api/proxies", json={"raw": "socks5://u:p@1.2.3.4:1080"}).json()["proxy"]
    assert (p["proxy_type"], p["addr"], p["port"], p["username"], p["password"]) == (
        "socks5", "1.2.3.4", 1080, "u", "p",
    )


def test_proxy_mtproto_requires_secret(client):
    bad = client.post(
        "/api/proxies", json={"proxy_type": "mtproto", "addr": "1.2.3.4", "port": 443}
    )
    assert bad.status_code == 400
    assert "secret" in bad.json()["detail"]

    # 长度不对的 secret 也要拦
    assert client.post("/api/proxies", json={
        "proxy_type": "mtproto", "addr": "1.2.3.4", "port": 443, "secret": "abc",
    }).status_code == 400

    ok = client.post("/api/proxies", json={
        "proxy_type": "mtproto", "addr": "1.2.3.4", "port": 443, "secret": SECRET,
    })
    assert ok.status_code == 200
    assert ok.json()["proxy"]["secret"] == SECRET


def test_unknown_proxy_type_rejected(client):
    assert client.post("/api/proxies", json={
        "proxy_type": "ssr", "addr": "1.2.3.4", "port": 1080,
    }).status_code == 400


def test_system_proxy_endpoint(client):
    assert client.get("/api/proxies/system").json()["proxy_id"] == 0

    p = client.post("/api/proxies", json={
        "name": "sys", "proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080,
    }).json()["proxy"]
    body = client.put("/api/proxies/system", json={"proxy_id": p["id"]}).json()
    assert body["proxy_id"] == p["id"]
    assert body["usable"] is True
    assert body["masked"].startswith("socks5://")

    # MTProto 代理代理不了 HTTP 请求，设成系统代理时 usable=False（实际按直连走）
    mt = client.post("/api/proxies", json={
        "proxy_type": "mtproto", "addr": "5.6.7.8", "port": 443, "secret": SECRET,
    }).json()["proxy"]
    assert client.put(
        "/api/proxies/system", json={"proxy_id": mt["id"]}
    ).json()["usable"] is False

    # 回到直连；不存在的 id 要 404
    assert client.put("/api/proxies/system", json={"proxy_id": 0}).json()["proxy_id"] == 0
    assert client.put("/api/proxies/system", json={"proxy_id": 999}).status_code == 404


def test_stats_and_health(client):
    stats = client.get("/api/stats").json()["stats"]
    assert stats["shares"] == 3
    assert stats["ok"] == 1
    assert stats["unchecked"] == 2

    health = client.get("/api/health").json()
    assert health["status"] == 200
    assert health["version"]


def test_check_disabled_returns_400(client):
    assert client.post("/api/check").status_code == 400


def test_static_whitelist_blocks_secrets(client):
    """config.toml / pikpak.db 绝不能通过 HTTP 下载。"""
    assert client.get("/config.toml").status_code == 404
    assert client.get("/pikpak.db").status_code == 404
    assert client.get("/app/store.py").status_code == 404
    assert client.get("/../config.toml").status_code == 404


def test_index_page_served(client):
    """首页返回前端入口。

    构建过 web/dist 时是 Vue 的入口页（<div id="app">），没构建时回退到
    根目录那个旧的单文件页（标题「数据浏览」）—— 两种都算通过。
    以前只断言旧标题，前端构建好之后这条就一直是红的。
    """
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert 'id="app"' in res.text or "数据浏览" in res.text


def _iter_api_routes(router, depth: int = 0):
    """展开 FastAPI 的嵌套路由，产出真正的 APIRoute。

    FastAPI 0.143 起 `include_router` 不再把子路由拍平进 `app.routes`，
    而是塞一个 `_IncludedRouter` 包装对象（它只有 `original_router`，
    没有 `.path`）。直接遍历 `app.routes` 一个接口都拿不到。
    """
    if depth > 8:
        return
    for route in list(getattr(router, "routes", None) or []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_api_routes(inner, depth + 1)
        else:
            yield route
    for route in list(getattr(router, "_low_priority_routes", None) or []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_api_routes(inner, depth + 1)
        else:
            yield route


def test_no_duplicate_route_paths(client):
    """同一个「方法 + 路径」只能注册一条。

    FastAPI 按注册顺序匹配，重复注册时后一条会被**静默遮蔽**，没有任何报错。
    /api/logs 就这么被遮过一次：设置页的日志面板一直显示空白，
    因为拿到的是日志页那个「日志内容」接口的返回。
    """
    seen: dict[tuple[str, str], str] = {}
    for route in _iter_api_routes(client.app):
        path = getattr(route, "path", None)
        if not path or not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, path)
            assert key not in seen, f"{method} {path} 被注册了两次，后一条会被遮蔽"
            seen[key] = path
    # 扫不到路由说明展开逻辑和 FastAPI 版本对不上了，得先修测试本身
    assert len(seen) > 20, f"只扫到 {len(seen)} 条 /api 路由，展开逻辑可能失效了"


def test_logs_content_and_log_files_are_different_endpoints(client):
    """/api/logs 是日志内容（日志页），/api/logs/files 是文件清单（设置页）。"""
    content = client.get("/api/logs").json()
    assert "sources" in content, "/api/logs 应该是日志内容接口"

    files = client.get("/api/logs/files").json()
    assert "files" in files and "retention_days" in files
