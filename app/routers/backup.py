"""备份与还原接口：一键下载归档、上传归档还原。

上传走原始请求体而不是 multipart —— 一个文件而已，为此多装一个
python-multipart 不划算。前端 `fetch(url, {method:'POST', body: file})` 就是。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app import backup
from app.routers.context import WebContext

log = logging.getLogger("backup")

# 上传上限。备份正常就是「库 + 缩略图」，几十到几百 MB；
# 留到 8G 只是为了不让一个畸形请求把磁盘写满。
MAX_UPLOAD_BYTES = 8 * 1024 ** 3


def build_router(ctx: WebContext) -> APIRouter:
    cfg = ctx.cfg
    store = ctx.store
    router = APIRouter()

    @router.get("/api/backup")
    def api_backup_info() -> dict:
        """设置页要的清单：装什么、多大、日志被排除在外。"""
        try:
            return backup.describe(cfg)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"统计备份内容失败：{e}") from e

    @router.get("/api/backup/download")
    async def api_backup_download() -> FileResponse:
        # 打包是纯 CPU + 磁盘，放线程里跑，别卡住事件循环（几十 MB 的库
        # 要压缩好几秒，期间页面轮询全会排队）
        workdir = Path(tempfile.mkdtemp(prefix="pikpak-backup-"))
        target = workdir / backup.default_name()
        try:
            manifest = await asyncio.to_thread(backup.create_archive, cfg, target)
        except backup.BackupError as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except OSError as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"打包失败：{e}") from e
        log.info("已生成备份 %s（%s 字节）", target.name, manifest.get("bytes"))
        # 临时目录跟着响应一起删掉：归档只是过一下手，不用留在服务器上
        return FileResponse(
            target,
            media_type="application/gzip",
            filename=target.name,
            background=BackgroundTask(shutil.rmtree, workdir, True),
        )

    @router.post("/api/backup/restore")
    async def api_backup_restore(request: Request) -> dict:
        name = (request.query_params.get("name") or "").strip()
        handle, tmp_name = tempfile.mkstemp(prefix="pikpak-upload-", suffix=".tar.gz")
        os.close(handle)
        tmp = Path(tmp_name)
        size = 0
        try:
            with tmp.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="归档超过 8G，请把文件放到服务器上再用 python -m tools.restore",
                        )
                    out.write(chunk)
            if not size:
                raise HTTPException(status_code=400, detail="没有收到文件内容")
            result = await asyncio.to_thread(
                backup.restore_archive, cfg, tmp, store=store
            )
        except backup.BackupError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        finally:
            tmp.unlink(missing_ok=True)
        log.info("已还原上传的备份 %s：%s", name or tmp.name, "、".join(result["applied"]))
        return {"status": 200, "name": name, **result}

    return router
