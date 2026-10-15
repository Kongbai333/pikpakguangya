"""静态页托管：Vite 产物优先，回退旧的单文件页。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import config as config_module
from app import web_public

ROOT = config_module.ROOT
# 允许通过 HTTP 访问的静态文件白名单
STATIC_WHITELIST = web_public.BLOCKED_FILES
WEB_DIST = ROOT / "web" / "dist"
SPA_INDEX = WEB_DIST / "index.html"


class _HashedAssets(StaticFiles):
    """给 Vite 产物加长缓存。

    Starlette 的 StaticFiles 只发 ETag / Last-Modified，浏览器每次都要回来问一遍
    「变了没」。文件名本来就带内容哈希，内容一变文件名就变，所以直接标成
    immutable；index.html 不走这里，保持每次校验。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_assets(app: FastAPI) -> None:
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets), name="assets")


def build_router(store=None) -> APIRouter:
    router = APIRouter()

    def closed() -> HTMLResponse:
        return HTMLResponse(web_public.CLOSED_HTML, status_code=404)

    def spa() -> FileResponse:
        """前端入口。Vue 产物优先，没有就退回旧的单文件页。"""
        target = SPA_INDEX if SPA_INDEX.exists() else ROOT / "index.html"
        return FileResponse(target, media_type="text/html; charset=utf-8")

    @router.get("/", include_in_schema=False)
    def index():
        # 前台关了就不给浏览页；管理后台走 /admin，不受影响
        if store is not None and not web_public.is_enabled(store):
            return closed()
        return spa()

    # hash 路由下正常不会用到，但直接敲地址栏 /admin 时得给同一个入口
    @router.get("/admin", include_in_schema=False)
    def admin_page() -> FileResponse:
        return spa()

    @router.get("/favicon.ico", include_in_schema=False)
    def favicon() -> None:
        raise HTTPException(status_code=404, detail="no favicon")

    @router.get("/{filename}", include_in_schema=False)
    def static_file(filename: str):
        """白名单静态文件（data.js / shuju.json / 旧版页面）。

        不做 StaticFiles(directory=项目根)，否则 config.toml（含 api_hash）
        和 pikpak.db 会被直接下载。前台关掉后这几个文件也一并拒绝 ——
        data.js 就是链接库的静态快照，放行等于没关。
        """
        if filename not in STATIC_WHITELIST:
            raise HTTPException(status_code=404, detail="not found")
        if store is not None and not web_public.is_enabled(store):
            return closed()
        path = ROOT / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{filename} 不存在")
        media = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(Path(filename).suffix, "application/octet-stream")
        return FileResponse(path, media_type=media)

    return router
