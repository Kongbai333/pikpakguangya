"""Telegram 媒体元数据与缩略图采集。

只保存 Telegram 自带缩略图和媒体元数据，不下载原图 / 视频。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.store import iso, utcnow

log = logging.getLogger("media")

MEDIA_DIR = ROOT / "media"
THUMB_DIR = MEDIA_DIR / "thumbs"
_DOWNLOAD_LOCK = asyncio.Semaphore(1)
_MIN_INTERVAL = 0.35
_last_download = 0.0


def _attr(message: Any, name: str, default: Any = None) -> Any:
    return getattr(message, name, default)


def _video_meta(document: Any) -> tuple[int | None, int | None, int | None]:
    for attr in getattr(document, "attributes", None) or []:
        width = getattr(attr, "w", None)
        height = getattr(attr, "h", None)
        duration = getattr(attr, "duration", None)
        if width or height or duration:
            return (
                int(width) if width else None,
                int(height) if height else None,
                int(duration) if duration else None,
            )
    return None, None, None


def _photo_meta(photo: Any) -> tuple[int | None, int | None, int | None]:
    candidates = []
    for size in getattr(photo, "sizes", None) or []:
        width = getattr(size, "w", None)
        height = getattr(size, "h", None)
        if width and height:
            candidates.append((int(width) * int(height), int(width), int(height)))
    if not candidates:
        return None, None, None
    _, width, height = max(candidates)
    return width, height, None


def detect_media(message: Any) -> list[dict]:
    """识别一条 Telegram 消息里的图片或视频。"""
    photo = _attr(message, "photo")
    video = _attr(message, "video")
    document = _attr(message, "document")

    if photo is not None:
        width, height, duration = _photo_meta(photo)
        return [{
            "item_index": 0,
            "media_type": "photo",
            "document_id": getattr(photo, "id", None),
            "access_hash": getattr(photo, "access_hash", None),
            "file_name": "",
            "mime_type": "image/jpeg",
            "size_bytes": None,
            "width": width,
            "height": height,
            "duration_sec": duration,
        }]

    if video is not None or document is not None:
        doc = video or document
        mime = str(getattr(doc, "mime_type", "") or "")
        is_video = video is not None or mime.startswith("video/")
        is_image = mime.startswith("image/")
        if not (is_video or is_image):
            return []
        width, height, duration = _video_meta(doc) if is_video else (None, None, None)
        names = [
            getattr(attr, "file_name", "")
            for attr in (getattr(doc, "attributes", None) or [])
            if getattr(attr, "file_name", "")
        ]
        return [{
            "item_index": 0,
            "media_type": "video" if is_video else "photo",
            "document_id": getattr(doc, "id", None),
            "access_hash": getattr(doc, "access_hash", None),
            "file_name": names[0] if names else "",
            "mime_type": mime,
            "size_bytes": getattr(doc, "size", None),
            "width": width,
            "height": height,
            "duration_sec": duration,
        }]
    return []


async def _download_thumb(message: Any, media_id: int, path: Path, store: Any) -> None:
    global _last_download
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _DOWNLOAD_LOCK:
        try:
            now = asyncio.get_running_loop().time()
            delay = max(0.0, _MIN_INTERVAL - (now - _last_download))
            if delay:
                await asyncio.sleep(delay)
            result = None
            for thumb in (0, -1):
                result = await message.download_media(file=str(path), thumb=thumb)
                if result:
                    break
            _last_download = asyncio.get_running_loop().time()
            if not result:
                raise ValueError("Telegram 没有返回可用缩略图")
            actual = Path(str(result))
            if not actual.is_absolute():
                actual = ROOT / actual
            rel = str(actual.relative_to(ROOT))
            store.set_media_thumb(media_id, rel, "ok", "")
        except Exception as e:
            store.set_media_thumb(media_id, "", "error", f"{type(e).__name__}: {e}")
            log.warning("媒体缩略图下载失败 media=%s: %s", media_id, e)


async def collect(message: Any, chat: Any, store: Any, account_id: int,
                  chat_title: str) -> list[int]:
    """把消息里的媒体元数据落库，并异步下载缩略图。"""
    items = detect_media(message)
    if not items:
        return []
    posted = _attr(message, "date")
    posted_at = iso(posted if posted and posted.tzinfo else
                    (posted.replace(tzinfo=utcnow().tzinfo) if posted else utcnow()))
    chat_id = int(getattr(chat, "id", 0) or getattr(message, "chat_id", 0) or 0)
    username = str(getattr(chat, "username", "") or "")
    grouped_id = getattr(message, "grouped_id", None)
    created = iso(utcnow())
    ids = []
    for info in items:
        media_id = store.upsert_media_item({
            "account_id": int(account_id),
            "chat_id": chat_id,
            "chat_title": chat_title or "",
            "chat_username": username,
            "message_id": int(getattr(message, "id", 0) or 0),
            "grouped_id": grouped_id,
            "posted_at": posted_at,
            "created_at": created,
            **info,
        })
        current = store.get_media(media_id) or {}
        if current.get("thumb_status") == "ok" and current.get("thumb_path"):
            ids.append(media_id)
            continue
        path = THUMB_DIR / str(account_id) / str(chat_id) / (
            f"{int(getattr(message, 'id', 0) or 0)}-{info['item_index']}.jpg"
        )
        asyncio.create_task(_download_thumb(message, media_id, path, store))
        ids.append(media_id)
    return ids


def relative_thumb(media: dict) -> Path | None:
    path = (media or {}).get("thumb_path") or ""
    if not path:
        return None
    full = (ROOT / path).resolve()
    with contextlib.suppress(ValueError):
        full.relative_to(MEDIA_DIR.resolve())
        return full if full.is_file() else None
    return None


def telegram_post_url(media: dict) -> str:
    username = (media or {}).get("chat_username") or ""
    message_id = int((media or {}).get("message_id") or 0)
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = int((media or {}).get("chat_id") or 0)
    if str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return ""
