"""链接与统计：前台列表、后台管理、校验。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import pikpak
from app.routers.context import WebContext
from app.store import STATUS_LABELS


class LinkIdsIn(BaseModel):
    ids: list[int] = []
    hidden: bool = True


class LinkSourceIn(BaseModel):
    ids: list[int] = []
    source: str = ""


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    cfg = ctx.cfg
    router = APIRouter()

    @router.get("/api/items")
    def api_items(
        q: str = "",
        page: int = 1,
        size: int = 50,
        sort: str = "id",
        dir: str = "desc",  # noqa: A002 - 对外参数名
        only_valid: bool = False,
        with_code: bool | None = None,
    ) -> dict:
        result = store.query(
            q=q, page=page, size=size, sort=sort, direction=dir,
            only_valid=only_valid, with_code=with_code,
        )
        # 前台不给来源：source 是采集到这条链接的群名，属于库主的隐私，
        # 前台页面上也不展示。管理页走 /api/admin/links，那边照旧有。
        for item in result.get("items") or []:
            item.pop("SOURCE", None)
        return {"status": 200, **result, "stats": store.stats()}

    @router.get("/api/stats")
    def api_stats() -> dict:
        return {"status": 200, "stats": store.stats()}

    @router.get("/api/stats/daily")
    def api_stats_daily(days: int = Query(default=30, ge=2, le=365)) -> dict:
        """按天汇总，给仪表盘的走势图用。日期是 UTC。"""
        return {"status": 200, **store.daily_stats(days)}

    @router.post("/api/check")
    async def api_check(limit: int = Query(default=50, ge=1, le=1000)) -> dict:
        """手动触发一批校验（后台跑，立即返回）。"""
        if not cfg.checker.enabled:
            raise HTTPException(status_code=400, detail="checker.enabled = false，校验已关闭")
        asyncio.create_task(
            pikpak.check_pending(store, cfg.checker, limit=limit,
                                 api_proxy_resolver=ctx.system_proxy)
        )
        return {"status": 200, "queued": limit}

    @router.get("/api/admin/links")
    def api_admin_links(
        q: str = "",
        page: int = 1,
        size: int = 20,
        sort: str = "id",
        dir: str = "desc",  # noqa: A002
        source: str = "",
        hidden: bool | None = None,
        status: str = "",
        with_code: bool | None = None,
    ) -> dict:
        """管理页的链接列表：包含已隐藏的，可按来源 / 显示状态 / 校验状态筛。"""
        result = store.query_admin(
            q=q, page=page, size=size, sort=sort, direction=dir,
            source=source, hidden=hidden, status=status, with_code=with_code,
        )
        return {
            "status": 200,
            **result,
            "sources": store.source_stats(),
            "status_counts": store.check_stats(),
            "status_labels": STATUS_LABELS,
            "stats": store.stats(),
        }

    @router.post("/api/admin/links/check")
    async def api_admin_links_check(payload: LinkIdsIn) -> dict:
        """重新检测指定的链接（同步等结果，前端点「重新检测」用）。

        不做熔断：用户明确点了这几条，就该给出结果。
        """
        ids = [int(i) for i in payload.ids]
        if not ids:
            raise HTTPException(status_code=400, detail="没有要检测的链接")
        if len(ids) > 100:
            raise HTTPException(
                status_code=400,
                detail=f"一次最多检测 100 条（这次选了 {len(ids)} 条）。"
                       "要批量刷的话用「检测待检测」（后台跑，不占着页面）。",
            )
        if not cfg.checker.enabled:
            raise HTTPException(status_code=400, detail="checker.enabled = false，校验已关闭")
        result = await pikpak.check_ids(store, cfg.checker, ids,
                                        api_proxy_resolver=ctx.system_proxy)
        return {
            "status": 200,
            **result,
            "status_counts": store.check_stats(),
            "stats": store.stats(),
        }

    @router.post("/api/admin/links/hide")
    def api_admin_links_hide(payload: LinkIdsIn) -> dict:
        """前台显示开关。ids 为空 = 对全部生效。"""
        updated = store.set_hidden(payload.ids, payload.hidden)
        return {
            "status": 200,
            "updated": updated,
            "hidden": payload.hidden,
            "sources": store.source_stats(),
        }

    @router.post("/api/admin/links/delete")
    def api_admin_links_delete(payload: LinkIdsIn) -> dict:
        if not payload.ids:
            raise HTTPException(status_code=400, detail="没有要删除的链接")
        deleted = store.delete_shares(payload.ids)
        return {
            "status": 200,
            "deleted": deleted,
            "sources": store.source_stats(),
            "stats": store.stats(),
        }

    @router.post("/api/admin/links/source")
    def api_admin_links_source(payload: LinkSourceIn) -> dict:
        """改来源（老数据没来源，可以手动补）。"""
        updated = store.set_source(payload.ids, payload.source)
        return {"status": 200, "updated": updated, "sources": store.source_stats()}

    return router
