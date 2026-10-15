"""秒传任务接口：创建任务、任务列表、单任务明细。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app import transfer
from app.guangya_login import GuangyaAuth
from app.routers.context import WebContext


class TransferJobsIn(BaseModel):
    """粘贴的多行文本，一行一个链接；inputs/share_url 只为兼容旧调用保留。"""
    text: str = ""
    inputs: list[str] = []
    share_url: str = ""
    pass_code: str = ""


class TransferSettingsIn(BaseModel):
    scan_workers: int = 15
    transfer_workers: int = 10
    cid_chunk_limit: int = 30
    check_used_links: bool | None = None


class TransferActionIn(BaseModel):
    action: str = ""


class TransferSelectIn(BaseModel):
    """选择性秒传：勾选 / 取消勾选要传的文件。

    ids 为空表示「批量操作全部待处理条目」；给了 max_size / min_size
    就是「只勾选小于 10MB 的」这类筛选。
    """
    ids: list[int] = []
    selected: bool = True
    max_size: int | None = None
    min_size: int | None = None


MAX_JOBS_PER_SUBMIT = 50


def build_router(ctx: WebContext) -> APIRouter:
    store = ctx.store
    router = APIRouter()

    @router.get("/api/transfers/jobs")
    def list_jobs(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        history = store.list_finished_transfer_jobs(page=page, size=size)
        return {
            "status": 200,
            "jobs": history["items"],
            "active_jobs": store.list_active_transfer_jobs(),
            "total": history["total"],
            "page": history["page"],
            "size": history["size"],
            "pages": history["pages"],
        }

    @router.get("/api/transfers/settings")
    def get_settings() -> dict:
        return {"status": 200, "settings": transfer.load_settings(store)}

    @router.put("/api/transfers/settings")
    def put_settings(payload: TransferSettingsIn) -> dict:
        return {
            "status": 200,
            "settings": transfer.save_settings(store, payload.model_dump()),
        }

    @router.get("/api/transfers/jobs/{job_id}")
    def get_job(
        job_id: int,
        items_limit: int = Query(default=300, ge=1, le=5000),
    ) -> dict:
        job = store.get_transfer_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {
            "status": 200,
            "job": job,
            "items": store.list_transfer_items(job_id, limit=items_limit),
            "selection": store.transfer_item_selection(job_id),
        }

    @router.post("/api/transfers/jobs/{job_id}/items/select")
    def select_items(job_id: int, payload: TransferSelectIn) -> dict:
        """勾选 / 取消勾选要秒传的文件。只影响还没处理的条目。"""
        if not store.get_transfer_job(job_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        try:
            updated = store.select_transfer_items(
                job_id,
                selected=payload.selected,
                ids=payload.ids or None,
                max_size=payload.max_size,
                min_size=payload.min_size,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "status": 200,
            "updated": updated,
            "selection": store.transfer_item_selection(job_id),
        }

    @router.get("/api/transfers/jobs/{job_id}/export")
    def export_job(job_id: int) -> Response:
        """把解析结果导成 JSON 附件：path/size/gcid/每个文件的结果都能看到。"""
        job = store.get_transfer_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        payload = {
            "job_id": job_id,
            "title": job.get("title") or "",
            "share_url": job.get("share_url") or "",
            "pass_code": job.get("pass_code") or "",
            "target_folder_id": job.get("target_folder_id") or "",
            "target_folder_path": job.get("target_folder_path") or "/",
            "status": job.get("status") or "",
            "message": job.get("message") or "",
            "total_bytes": int(job.get("total_bytes") or 0),
            "completed_bytes": int(job.get("completed_bytes") or 0),
            "created_at": job.get("created_at") or "",
            "finished_at": job.get("finished_at") or "",
            "files": [
                {
                    "path": item.get("path") or "",
                    "name": item.get("name") or "",
                    "size": int(item.get("size_bytes") or 0),
                    "gcid": item.get("gcid") or "",
                    "cid": item.get("cid") or "",
                    "file_id": item.get("file_id") or "",
                    "status": item.get("status") or "",
                    "selected": bool(item.get("selected", 1)),
                    "error": item.get("error") or "",
                }
                for item in store.list_transfer_items(job_id, limit=None)
            ],
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="pikpak-transfer-{job_id}.json"'
                ),
            },
        )

    @router.delete("/api/transfers/jobs/{job_id}")
    def delete_job(job_id: int) -> dict:
        job = store.get_transfer_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.get("status") in ("running", "queued", "paused"):
            raise HTTPException(status_code=400, detail="任务还在队列里，先终止再删除")
        store.delete_transfer_job(job_id)
        return {"status": 200}

    @router.post("/api/transfers/jobs/{job_id}/action")
    def job_action(job_id: int, payload: TransferActionIn) -> dict:
        manager = ctx.app.state.transfers
        handlers = {
            "pause": manager.pause,
            "resume": manager.resume,
            "cancel": manager.cancel,
            "retry": manager.retry,
        }
        handler = handlers.get((payload.action or "").strip().lower())
        if handler is None:
            raise HTTPException(status_code=400, detail="不支持的操作")
        job = handler(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"status": 200, "job": job}

    @router.post("/api/transfers/jobs")
    def create_jobs(payload: TransferJobsIn) -> dict:
        if (payload.text or "").strip():
            lines = transfer.split_share_inputs(payload.text)
        else:
            lines = [line.strip() for line in payload.inputs if line.strip()]
        legacy_code = (payload.pass_code or "").strip()
        if not lines and (payload.share_url or "").strip():
            # 旧版调用：一个链接 + 单独的提取码
            lines = [payload.share_url.strip()]
        if not lines:
            raise HTTPException(status_code=400, detail="请粘贴至少一个 PikPak 分享链接")
        if len(lines) > MAX_JOBS_PER_SUBMIT:
            raise HTTPException(
                status_code=400,
                detail=f"一次最多提交 {MAX_JOBS_PER_SUBMIT} 条链接",
            )
        guangya = GuangyaAuth(store).status()
        if not guangya.get("connected"):
            raise HTTPException(status_code=400, detail="请先在设置里登录光鸭账号")
        check_used = bool(transfer.load_settings(store).get("check_used_links"))
        # 目标目录固定用设置页选好的那个，秒传页不再问
        target_folder_id = str(guangya.get("root_folder_id") or "")
        target_folder_path = str(guangya.get("root_folder_path") or "/")
        jobs: list[dict] = []
        errors: list[str] = []
        for line in lines:
            share_id, pass_code = transfer.parse_share_input(line)
            if not share_id:
                errors.append(f"{line[:60]}：没找到分享链接")
                continue
            if check_used:
                reason = transfer.used_link_reason(store, share_id)
                if reason:
                    errors.append(f"{line[:60]}：{reason}")
                    continue
            job = ctx.app.state.transfers.create_job(
                share_url=line,
                pass_code=pass_code or legacy_code,
                target_folder_id=target_folder_id,
                target_folder_path=target_folder_path or "/",
            )
            jobs.append(job)
        if not jobs:
            raise HTTPException(
                status_code=400,
                detail="；".join(errors[:3]) or "没有可提交的链接",
            )
        return {"status": 200, "jobs": jobs, "errors": errors}

    return router
