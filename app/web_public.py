"""前台浏览页总开关。

关掉之后 `/` 不再返回浏览页，`/api/items`、`/api/stats` 和那几个静态数据文件
也一并拒绝 —— 只藏页面不拦接口等于没关，数据照样能直接拉走。

管理后台（`/admin`）不受影响：这是给库主自己看的，不是「前台」。
开关存在 state 表里，默认开，所以老库升级后行为不变。
"""

from __future__ import annotations

STATE_KEY = "web.public.enabled"
# 关掉前台后要拦掉的公开接口
PUBLIC_PATHS = ("/api/items", "/api/stats")
# 关掉前台后不再放行的静态文件（data.js / shuju.json 是导出的链接快照）
BLOCKED_FILES = ("index.html", "data.js", "shuju.json")


def is_enabled(store) -> bool:
    return store.get_state(STATE_KEY, "1") != "0"


def set_enabled(store, enabled: bool) -> bool:
    store.set_state(STATE_KEY, "1" if enabled else "0")
    return bool(enabled)


# 关掉之后 / 上显示的东西。自带样式、不引外部资源 —— 前台已经关了，
# 再让浏览器去拉一堆静态文件没有意义。
#
# 里面那个「进入管理后台」必须指向 `/admin#/admin`，不能写 `/#/admin`：
# 前端是 **hash 路由**，服务器只看得见 `/`，写 `/#/admin` 等于再请求一次 `/`，
# 又拿到这张关闭页 —— 点了像是没反应，库主反而被锁在门外。
# `/admin` 这个路径是会吐前端入口的（见 routers/static.py）。
CLOSED_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>浏览页已关闭</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    padding: 24px; background: #fafafa; color: #18181b;
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .card {
    width: 100%; max-width: 420px; padding: 32px 30px;
    border: 1px solid #e4e4e7; border-radius: 18px; background: #fff;
    box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 12px 32px -12px rgba(0,0,0,.12);
    text-align: center;
  }
  .mark {
    width: 46px; height: 46px; margin: 0 auto 16px; border-radius: 13px;
    display: grid; place-items: center; background: #18181b; color: #fff;
    font-weight: 700; letter-spacing: -.5px;
  }
  h1 { margin: 0 0 8px; font-size: 19px; font-weight: 650; letter-spacing: -.2px; }
  p { margin: 0; color: #71717a; font-size: 13.5px; }
  a {
    display: inline-block; margin-top: 20px; padding: 9px 18px;
    border-radius: 10px; background: #18181b; color: #fff;
    font-size: 13.5px; font-weight: 500; text-decoration: none;
  }
  a:hover { background: #3f3f46; }
  @media (prefers-color-scheme: dark) {
    body { background: #111113; color: #e7e7ea; }
    .card { background: #1d1d20; border-color: #2c2c31; }
    .mark { background: #e7e7ea; color: #18181b; }
    p { color: #a1a1aa; }
    a { background: #e7e7ea; color: #18181b; }
    a:hover { background: #fff; }
  }
</style>
</head>
<body>
  <div class="card">
    <div class="mark">PK</div>
    <h1>浏览页已关闭</h1>
    <p>库主关掉了前台浏览。数据还在，只是不对外展示。</p>
    <a href="/admin#/admin">进入管理后台</a>
  </div>
</body>
</html>
"""
