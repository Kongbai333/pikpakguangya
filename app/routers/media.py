"""Telegram 媒体瀑布流与缩略图。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import media as media_module
from app.routers.context import WebContext


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    router = APIRouter()

    @router.get("/api/media")
    def api_media(
        page: int = 1,
        size: int = 60,
        media_type: str = "",
        chat_id: int | None = None,
        account_id: int | None = None,
    ) -> dict:
        result = store.list_media(
            page=page, size=size, media_type=media_type,
            chat_id=chat_id, account_id=account_id,
        )
        for item in result["items"]:
            item["thumb_url"] = (
                f"/api/media/{item['id']}/thumb"
                if item.get("thumb_status") == "ok" and item.get("thumb_path") else ""
            )
            item["telegram_url"] = media_module.telegram_post_url(item)
        return {"status": 200, **result}

    @router.get("/api/media/{media_id}/thumb")
    def api_media_thumb(media_id: int) -> FileResponse:
        item = store.get_media(media_id)
        path = media_module.relative_thumb(item or {})
        if not path:
            raise HTTPException(status_code=404, detail="缩略图不存在")
        return FileResponse(path, media_type="image/jpeg")

    return router
