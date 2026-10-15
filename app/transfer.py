"""PikPak 分享链接 → 光鸭云盘秒传。

链路：解析分享链接 → 递归列出文件（带 GCID）→ 逐个调光鸭秒传接口。
命中直接入库；未命中记「未命中」，**不做普通上传兜底**（不占上传带宽）。
任务和每个文件的状态全部落库，页面轮询 transfer_jobs / transfer_items 看进度。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from typing import Any, Callable

import httpx

from app import pikpak, pikpak_api
from app.guangya_login import GuangyaAuth, GuangyaAuthError, GuangyaLoginError
from app.integrations import _pikpak_refresh_token
from app.store import iso, utcnow

log = logging.getLogger("transfer")

SHARE_DETAIL_URL = "https://api-drive.mypikpak.com/drive/v1/share/detail"
SHARE_DETAIL_ACTION = "GET:/drive/v1/share/detail"
SHARE_FILE_INFO_URL = "https://api-drive.mypikpak.com/drive/v1/share/file_info"
SHARE_FILE_INFO_ACTION = "GET:/drive/v1/share/file_info"
# 只要没进回收站的文件；PikPak 分享目录接口要求显式过滤
SHARE_DETAIL_FILTERS = '{"trashed":{"eq":false}}'
MAX_DEPTH = 8
# 单任务最多两万个文件；超过就先按目录拆成多个任务
SPLIT_FILES = 20000
# 自动拆分的安全上限，超过这个量说明分享过大，让用户手动拆分享
MAX_PARSE_FILES = 100000
# 光鸭秒传要的 CID = 文件前 20KB + 1/3 处 20KB + 末尾 20KB 拼起来的 SHA-1
CID_SAMPLE = 20 * 1024
INVALID_PATH_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_path_name(value: Any) -> str:
    """去掉光鸭不允许的控制/不可见字符，并清理路径分隔符和尾随空格。"""
    text = "".join(
        ch for ch in str(value or "")
        if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )
    text = INVALID_PATH_CHARS_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return text or "未命名"

# 并发设置（本机实测标定的默认值）：
#   scan_workers     PikPak 目录扫描，走代理，20 足够
#   transfer_workers 光鸭秒传并发：实测 10~15 吞吐最好，再高只是把请求排队
#   cid_chunk_limit  CID 采样（PikPak CDN 直连）能扛 60 并发，这里留 40 的余量
SETTINGS_KEY = "transfer.settings"
DEFAULT_SETTINGS = {
    "scan_workers": 20,
    "transfer_workers": 15,
    "cid_chunk_limit": 40,
    "check_used_links": False,
}
MAX_WORKERS = 64

# 秒传加进来的链接也要进链接库，来源按入口标一下（链接页的「来源」列、
# 仪表盘的来源构成都用它）。认不出来的入口就原样显示。
TRANSFER_SOURCE_LABELS = {
    "web": "手动秒传",
    "telegram": "Bot 秒传",
}

# 提取码写法很杂：`提取码: ab12`、`提取码 ab12`、`pwd=ab12`、`pass_code=ab12` 都认
PASS_CODE_RE = re.compile(
    r"(?:提取码|提取碼|密码|密碼|访问码|訪問碼|pwd|pass(?:word)?|pass[_-]?code)"
    r"(?:\s*[:：=]\s*|\s+)([A-Za-z0-9]{2,16})",
    re.I,
)
# 允许直接粘分享 ID；长度卡一下，别把「复制这段内容…」这种说明文字当分享
SHARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,}")


def parse_share_input(text: str) -> tuple[str, str]:
    """从粘贴的一行里拆出分享标识和提取码，链接、带码链接、纯 ID 都支持。"""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    matched = PASS_CODE_RE.search(raw)
    pass_code = matched.group(1) if matched else ""
    share_id = pikpak.link_id_of(raw)
    if share_id != raw:
        return share_id, pass_code
    # 没有 /s/ 链接时 link_id_of 原样返回，只有纯 ID 才认
    token = raw.split()[0] if (" " in raw or "\t" in raw) else raw
    return (token if SHARE_ID_RE.fullmatch(token) else ""), pass_code


def split_share_inputs(text: str) -> list[str]:
    """多行文本拆成一条一行。

    提取码单独占一行时并回上一条链接；「复制这段内容…」这类说明文字直接忽略。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        has_link = bool(pikpak.LINK_ID_RE.search(line))
        if out and not has_link and PASS_CODE_RE.search(line):
            out[-1] = f"{out[-1]} {line}"
            continue
        if not has_link:
            token = line.split()[0] if (" " in line or "\t" in line) else line
            if not SHARE_ID_RE.fullmatch(token):
                continue
        out.append(line)
    return out


def load_settings(store) -> dict:
    raw = store.get_json(SETTINGS_KEY, {}) or {}
    out = {}
    for key, default in DEFAULT_SETTINGS.items():
        if isinstance(default, bool):
            value = raw.get(key, default)
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            out[key] = bool(value)
            continue
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        out[key] = max(1, min(value, MAX_WORKERS))
    return out


def save_settings(store, values: dict) -> dict:
    merged = load_settings(store)
    for key in DEFAULT_SETTINGS:
        if values.get(key) is not None:
            merged[key] = values[key]
    for key, default in DEFAULT_SETTINGS.items():
        if isinstance(default, bool):
            merged[key] = bool(merged.get(key))
        else:
            merged[key] = max(1, min(int(merged.get(key) or default), MAX_WORKERS))
    store.set_json(SETTINGS_KEY, merged)
    return merged


class TransferError(RuntimeError):
    pass


def used_link_reason(store, share_id: str) -> str:
    """检查分享是否已经创建过秒传任务。"""
    if not share_id:
        return ""
    job = store.find_transfer_job_by_link_id(share_id)
    if job:
        return f"该分享已有秒传任务（#{job.get('id')}）"
    return ""


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _gcid_of(file_info: dict) -> str:
    """从 PikPak 文件信息里找 GCID：hash 对象优先，其次平铺字段。"""
    for key in ("gcid", "cid"):
        value = file_info.get(key)
        if value:
            return str(value)
    hashed = file_info.get("hash")
    if isinstance(hashed, dict):
        for key in ("gcid", "cid"):
            if hashed.get(key):
                return str(hashed[key])
    elif isinstance(hashed, str) and hashed:
        return hashed
    return ""


def _headers(store, device_id: str, captcha_token: str) -> dict[str, str]:
    """分享接口是公开的，不带 Authorization。

    带上登录态反而会被过期 token 连累：PikPak 会直接回「账号验证失败」，
    而这跟分享本身能不能读没有关系。
    """
    return pikpak_api._headers(device_id, captcha_token)


def _request(client: httpx.Client, url: str, params: dict, store, proxy,
             device_id: str, captcha_token: str, action: str) -> httpx.Response:
    """带一次兜底重试的分享请求：401 刷新登录态，captcha 失效重取 token。"""
    headers = _headers(store, device_id, captcha_token)
    response = client.get(url, params=params, headers=headers)

    if response.status_code == 401:
        try:
            access_token = _pikpak_refresh_token(store, proxy)
        except Exception:
            access_token = ""
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
            response = client.get(url, params=params, headers=headers)

    if response.status_code == 400 and "captcha" in response.text:
        pikpak_api._TOKEN_CACHE.pop(action, None)
        captcha_token = pikpak_api._get_token(client, device_id, action)
        headers["x-captcha-token"] = captcha_token
        response = client.get(url, params=params, headers=headers)
    return response


def _json(response: httpx.Response, what: str) -> dict:
    try:
        data = response.json()
    except ValueError as e:
        raise TransferError(f"{what}返回异常：{response.text[:200]}") from e
    if response.is_error:
        message = (
            data.get("error_description") or data.get("error")
            or data.get("message") or f"HTTP {response.status_code}"
        )
        raise TransferError(f"{what}失败：{message}")
    return data


def _list_dir(client: httpx.Client, store, proxy, device_id: str, share_id: str,
              pass_code_token: str, parent_id: str, *, first: bool,
              pass_code: str = "") -> tuple[list[dict], str]:
    """列一层目录（自动翻页），返回 (files, pass_code_token)。"""
    files: list[dict] = []
    page_token = ""
    title = ""
    for _ in range(50):
        if first:
            url = pikpak_api.SHARE_URL
            action = pikpak_api.SHARE_ACTION
            params: dict[str, Any] = {
                "share_id": share_id,
                "limit": 100,
                "thumbnail_size": "SIZE_LARGE",
                "order": 3,
            }
            if pass_code:
                params["pass_code"] = pass_code
        else:
            url = SHARE_DETAIL_URL
            action = SHARE_DETAIL_ACTION
            params = {
                "share_id": share_id,
                "parent_id": parent_id,
                "pass_code_token": pass_code_token,
                "limit": 100,
                "filters": SHARE_DETAIL_FILTERS,
                "with_audit": "true",
            }
        if page_token:
            params["page_token"] = page_token

        captcha_token = pikpak_api._get_token(client, device_id, action)
        data = _json(
            _request(client, url, params, store, proxy, device_id, captcha_token, action),
            "PikPak 分享目录接口",
        )
        if first:
            status = str(data.get("share_status") or "").strip()
            if status == "PASS_CODE_ERROR":
                raise TransferError("提取码错误，请核对后重试")
            if status == "PASS_CODE_EMPTY":
                raise TransferError("这个分享需要提取码，请填写提取码")
            if status and status != "OK":
                raise TransferError(
                    f"分享不可用：{data.get('share_status_text') or status}"
                )
            pass_code_token = str(data.get("pass_code_token") or "")
            title = str(data.get("title") or "").strip()
        files.extend(data.get("files") or [])
        page_token = str(data.get("next_page_token") or "")
        if not page_token:
            break
    return files, pass_code_token, title


def list_share_files(store, share_url: str, pass_code: str,
                     proxy=None, scan_workers: int = 15,
                     client: httpx.Client | None = None
                     ) -> tuple[list[dict], str, str]:
    """递归列出分享里的全部文件：{path, name, size, gcid, file_id}。

    目录逐层并发扫描（scan_workers 个线程），文件先全部解析完再进秒传阶段。
    传了 client 就复用调用方的连接（省掉一遍遍代理握手）。
    返回 (files, pass_code_token, title)。
    """
    share_id = pikpak.link_id_of(share_url)
    if not share_id:
        raise TransferError("无法从分享链接里提取 share_id")
    device_id = pikpak_api.device_id(store)
    out: list[dict] = []
    out_lock = threading.Lock()

    owns_client = client is None
    if client is None:
        client = pikpak_api._client(proxy, 30.0)
    try:
        root_files, pass_code_token, title = _list_dir(
            client, store, proxy, device_id, share_id, "", "",
            first=True, pass_code=pass_code or "",
        )
        with ThreadPoolExecutor(max_workers=max(1, int(scan_workers))) as pool:
            futures: set[Future] = set()

            def collect(files: list[dict], prefix: str, depth: int) -> None:
                child_dirs: list[tuple[str, str, int]] = []
                with out_lock:
                    for info in files:
                        if len(out) >= MAX_PARSE_FILES:
                            raise TransferError(
                                f"文件超过 {MAX_PARSE_FILES} 个，"
                                "自动拆分上限已到，请手动拆分享后重试"
                            )
                        name = sanitize_path_name(info.get("name"))
                        if str(info.get("kind") or "") == "drive#folder":
                            if depth < MAX_DEPTH:
                                child_dirs.append((
                                    str(info.get("id") or ""),
                                    f"{prefix}{name}/",
                                    depth + 1,
                                ))
                            continue
                        out.append({
                            "path": f"{prefix}{name}",
                            "name": name,
                            "size": _to_int(info.get("size")),
                            "gcid": _gcid_of(info),
                            "file_id": str(info.get("id") or ""),
                        })
                for parent_id, child_prefix, child_depth in child_dirs:
                    futures.add(pool.submit(scan_folder, parent_id, child_prefix, child_depth))

            def scan_folder(parent_id: str, prefix: str, depth: int) -> None:
                children, _, _ = _list_dir(
                    client, store, proxy, device_id, share_id, pass_code_token,
                    parent_id, first=False,
                )
                collect(children, prefix, depth)

            collect(root_files, "", 0)
            while futures:
                # 传副本：子目录线程可能正在往 futures 里追加下一层
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.discard(future)
                    future.result()   # 子目录扫失败就让整单失败，别静默漏文件
    finally:
        if owns_client:
            client.close()
    return out, pass_code_token, title


def derive_share_title(files: list[dict], share_title: str) -> str:
    """列表里显示的名字：优先用 PikPak 分享标题，没有就从文件路径推一个。"""
    title = str(share_title or "").strip()
    if title:
        return title[:120]
    if not files:
        return ""
    tops: list[str] = []
    for info in files[:500]:
        top = str(info.get("path") or "").split("/")[0]
        if top and top not in tops:
            tops.append(top)
    if len(tops) == 1:
        return tops[0][:120]
    if tops:
        return f"{tops[0][:90]} 等 {len(tops)} 项"
    return ""


def _chunk_items(items: list[dict], limit: int) -> list[list[dict]]:
    return [items[start:start + limit] for start in range(0, len(items), limit)]


def _directory_groups(items: list[dict], limit: int,
                      prefix: str) -> list[tuple[str, list[dict]]]:
    """按目录层级拆；单层仍超限就继续往下，最后一层再按顺序切块。"""
    if len(items) <= limit:
        return [(prefix, items)]
    by_child: dict[str, list[dict]] = {}
    direct: list[dict] = []
    for item in items:
        path = str(item.get("path") or "")
        rest = path[len(prefix):] if prefix and path.startswith(prefix) else path
        parts = rest.split("/", 1)
        if len(parts) == 1:
            direct.append(item)
        else:
            by_child.setdefault(parts[0], []).append(item)
    groups: list[tuple[str, list[dict]]] = []
    for chunk in _chunk_items(direct, limit):
        groups.append((prefix, chunk))
    for child, child_items in by_child.items():
        groups.extend(_directory_groups(child_items, limit, f"{prefix}{child}/"))
    if not groups:
        for chunk in _chunk_items(items, limit):
            groups.append((prefix, chunk))
    return groups


def _merge_directory_groups(groups: list[tuple[str, list[dict]]],
                            limit: int) -> list[tuple[list[str], list[dict]]]:
    """把相邻小目录合并到接近 limit，避免一个分享拆出几十个小任务。"""
    merged: list[tuple[list[str], list[dict]]] = []
    current_items: list[dict] = []
    current_prefixes: list[str] = []
    for prefix, items in groups:
        if current_items and len(current_items) + len(items) > limit:
            merged.append((current_prefixes, current_items))
            current_items = []
            current_prefixes = []
        current_items.extend(items)
        current_prefixes.append(prefix)
    if current_items:
        merged.append((current_prefixes, current_items))
    return merged


def _group_label(prefixes: list[str]) -> str:
    names: list[str] = []
    has_root = False
    for prefix in prefixes:
        name = prefix.rstrip("/")
        if not name:
            has_root = True
        elif name not in names:
            names.append(name)
    if len(names) == 1 and not has_root:
        label = names[0]
    elif names:
        label = f"{names[0]} 等 {len(names) + (1 if has_root else 0)} 个目录"
    elif has_root:
        label = "根目录文件"
    else:
        label = "未命名分组"
    return label


def plan_share_groups(files: list[dict], limit: int,
                      base_title: str) -> list[dict]:
    """返回 [{title, items}]；不超过 limit 时只有一组，不拆任务。"""
    if not files:
        return []
    if len(files) <= limit:
        return [{"title": base_title or "PikPak 分享", "items": files}]
    ordered = sorted(files, key=lambda item: str(item.get("path") or ""))
    directory_groups = _directory_groups(ordered, limit, "")
    merged = _merge_directory_groups(directory_groups, limit)
    total = len(merged)
    out: list[dict] = []
    for index, (prefixes, items) in enumerate(merged, 1):
        desc = _group_label(prefixes)
        group_part = f"第 {index}/{total} 组"
        title = (
            f"{group_part} · {desc} · {base_title}"
            if base_title else f"{group_part} · {desc}"
        )
        out.append({"title": title[:220], "items": items})
    return out


def _fetch_download_url(client: httpx.Client, store, proxy, device_id: str,
                        share_id: str, file_id: str, pass_code_token: str) -> str:
    """取分享里单个文件的下载直链（PikPak 的 share/file_info 接口）。"""
    captcha_token = pikpak_api._get_token(client, device_id, SHARE_FILE_INFO_ACTION)
    data = _json(
        _request(
            client, SHARE_FILE_INFO_URL,
            {
                "share_id": share_id,
                "file_id": file_id,
                "pass_code_token": pass_code_token,
            },
            store, proxy, device_id, captcha_token, SHARE_FILE_INFO_ACTION,
        ),
        "PikPak 文件下载接口",
    )
    info = data.get("file_info") or {}
    url = str(info.get("web_content_link") or "")
    if not url:
        medias = info.get("medias") or []
        if medias:
            url = str(((medias[0].get("link") or {}).get("url")) or "")
    return url


def compute_cid(size: int, read_range) -> str:
    """光鸭要的 CID：前 20KB + 1/3 处 20KB + 末尾 20KB 拼起来取 SHA-1。"""
    if size <= 0:
        return ""
    if size <= CID_SAMPLE:
        combined = read_range(0, size - 1) * 3
    elif size <= CID_SAMPLE * 2:
        mid_start = size // 3
        combined = (
            read_range(0, CID_SAMPLE - 1)
            + read_range(mid_start, min(mid_start + CID_SAMPLE, size) - 1)
            + read_range(max(0, size - CID_SAMPLE), size - 1)
        )
    else:
        mid_start = size // 3
        combined = (
            read_range(0, CID_SAMPLE - 1)
            + read_range(mid_start, mid_start + CID_SAMPLE - 1)
            + read_range(size - CID_SAMPLE, size - 1)
        )
    return hashlib.sha1(combined).hexdigest().upper()


def compute_item_cid(store, proxy, share_id: str, pass_code_token: str,
                     item: dict, api_client: httpx.Client | None = None,
                     cdn_client: httpx.Client | None = None) -> str:
    """给单个文件算 CID：只下载 3×20KB 样本，PikPak CDN 直连即可。"""
    size = _to_int(item.get("size_bytes"))
    file_id = str(item.get("file_id") or "")
    if size <= 0 or not file_id:
        return ""
    owns_api = api_client is None
    if api_client is None:
        api_client = pikpak_api._client(proxy, 30.0)
    try:
        device_id = pikpak_api.device_id(store)
        url = _fetch_download_url(
            api_client, store, proxy, device_id, share_id, file_id, pass_code_token,
        )
    finally:
        if owns_api:
            api_client.close()
    if not url:
        raise TransferError("PikPak 没返回下载直链，无法计算 CID")

    def read_with(client: httpx.Client) -> str:
        def read(start: int, end: int) -> bytes:
            response = client.get(url, headers={"Range": f"bytes={start}-{end}"})
            response.raise_for_status()
            data = response.content
            want = end - start + 1
            if len(data) > want:   # 服务器忽略 Range 返回整文件时自己截
                data = data[start:end + 1]
            return data
        return compute_cid(size, read)

    if cdn_client is not None:
        return read_with(cdn_client)
    try:
        with httpx.Client(timeout=40.0, follow_redirects=True, trust_env=False) as client:
            return read_with(client)
    except httpx.HTTPError:
        if proxy is None:
            raise
        # CDN 直连不通时退回账号代理再试一次
        with pikpak_api._client(proxy, 40.0) as client:
            return read_with(client)


class TransferManager:
    """队列调度：同一时间只跑一个秒传任务；下一单可提前解析，状态实时落库。"""

    def __init__(self, store, proxy_resolver=None):
        self.store = store
        self.proxy_resolver = proxy_resolver
        self._wake = threading.Event()
        self._parse_wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._parse_thread: threading.Thread | None = None
        # 每个任务一套暂停/终止开关；_active_job_id 表示当前正在跑哪个任务
        self._controls: dict[int, dict[str, threading.Event]] = {}
        self._control_lock = threading.Lock()
        self._active_job_id: int | None = None
        self._active_phase: str | None = None
        # 下一单的预解析状态；解析和秒传用不同网盘，可以重叠执行
        self._prefetching_job_id: int | None = None
        self._prefetch_checked_job_id: int | None = None
        self._prefetch_lock = threading.Lock()
        self._progress_callback: Callable[[int, dict], None] | None = None

    def set_progress_callback(self, callback: Callable[[int, dict], None] | None) -> None:
        """任务解析、单文件完成和最终结束时各通知一次。"""
        self._progress_callback = callback

    def _emit_progress(self, job_id: int, event: dict | None = None) -> None:
        callback = self._progress_callback
        if callback is None:
            return
        try:
            callback(int(job_id), event or {})
        except Exception:
            log.warning("秒传进度回调失败 job=%s", job_id, exc_info=True)

    def _control(self, job_id: int) -> dict[str, threading.Event]:
        with self._control_lock:
            ctrl = self._controls.get(job_id)
            if ctrl is None:
                ctrl = {"pause": threading.Event(), "cancel": threading.Event()}
                self._controls[job_id] = ctrl
            return ctrl

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        requeued = self.store.requeue_interrupted_transfer_jobs()
        if requeued:
            log.info("服务重启，%s 个秒传任务已重新排队", requeued)
        self._thread = threading.Thread(
            target=self._loop, name="transfer-worker", daemon=True,
        )
        self._parse_thread = threading.Thread(
            target=self._parse_loop, name="transfer-prefetch", daemon=True,
        )
        self._thread.start()
        self._parse_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._parse_wake.set()

    def notify(self) -> None:
        self._wake.set()
        with self._prefetch_lock:
            self._prefetch_checked_job_id = None
        self._parse_wake.set()

    def create_job(self, *, share_url: str, pass_code: str,
                   target_folder_id: str, target_folder_path: str,
                   source: str = "web") -> dict:
        job = self.store.create_transfer_job(
            share_url=share_url,
            pass_code=pass_code,
            target_folder_id=target_folder_id,
            target_folder_path=target_folder_path,
            source=source,
        )
        self._record_share(share_url, pass_code, source)
        self.notify()
        return job

    def _record_share(self, share_url: str, pass_code: str, source: str) -> None:
        """把秒传用的链接登记进链接库（它没有对应的 Telegram 消息）。

        手动加的、Bot 加的都要进 —— 否则链接页里搜不到，也不会被定时校验
        覆盖到。判重交给 store.add_share 的 link_id 唯一约束：已经采到过的
        链接只刷新 last_seen_at，来源保留首次那个，不会因为秒传又冒出一行。
        """
        link_id = pikpak.link_id_of(share_url)
        if not link_id:
            return
        # 用户可能只粘了个分享 id，这里统一补成能点的完整链接
        url = f"https://mypikpak.com/s/{link_id}"
        label = TRANSFER_SOURCE_LABELS.get(source, source or "秒传")
        try:
            result = self.store.add_share(
                link_id=link_id, url=url, pass_code=pass_code or "", source=label,
            )
        except Exception:
            # 登记失败不该把建任务搞挂：任务才是用户点这一下要的东西
            log.warning("秒传链接登记入库失败：%s", link_id, exc_info=True)
            return
        if result.get("share_created"):
            log.info("秒传链接已入库：%s（%s）", link_id, label)

    # ---------- 任务控制 ----------
    def pause(self, job_id: int) -> dict | None:
        job = self.store.get_transfer_job(job_id)
        if not job:
            return None
        if job.get("status") not in ("queued", "running"):
            return job
        self._control(int(job_id))["pause"].set()
        with self._prefetch_lock:
            if self._active_job_id == int(job_id):
                self._active_phase = "paused"
        self.store.update_transfer_job(int(job_id), status="paused", message="已暂停")
        self._parse_wake.set()
        self._emit_progress(job_id, {"type": "snapshot"})
        return self.store.get_transfer_job(job_id)

    def resume(self, job_id: int) -> dict | None:
        job = self.store.get_transfer_job(job_id)
        if not job:
            return None
        ctrl = self._control(int(job_id))
        ctrl["pause"].clear()
        if job.get("status") == "paused":
            if self._active_job_id == int(job_id):
                with self._prefetch_lock:
                    self._active_phase = "transferring"
                self._parse_wake.set()
                self.store.update_transfer_job(
                    int(job_id), status="running", message="已继续",
                )
            else:
                # 重启过，任务不在跑：重新排队等执行
                self.store.update_transfer_job(
                    int(job_id), status="queued", message="已继续，等待执行",
                )
                self.notify()
        self._emit_progress(job_id, {"type": "snapshot"})
        return self.store.get_transfer_job(job_id)

    def cancel(self, job_id: int) -> dict | None:
        job = self.store.get_transfer_job(job_id)
        if not job:
            return None
        if job.get("status") in ("finished", "failed", "canceled"):
            return job
        ctrl = self._control(int(job_id))
        ctrl["cancel"].set()
        ctrl["pause"].clear()   # 解开暂停，等着的线程才能退出
        if self._active_job_id != int(job_id):
            # 还没开始跑：直接结束，没处理的文件保持 pending，之后可以重试
            self.store.update_transfer_job(
                int(job_id), status="canceled", current_file="",
                message="已终止", finished_at=iso(utcnow()),
            )
            self._emit_progress(job_id, {"type": "final"})
            self._parse_wake.set()
        return self.store.get_transfer_job(job_id)

    def retry(self, job_id: int) -> dict | None:
        job = self.store.get_transfer_job(job_id)
        if not job:
            return None
        if self._active_job_id == int(job_id):
            return job   # 正在跑，先别重试
        store = self.store
        removed = store.dedupe_transfer_items(int(job_id))
        if removed:
            log.info("任务 #%s 重试前清理了 %s 条重复明细", job_id, removed)
        # 失败和未命中都重跑：未命中很多是哈希/接口问题，重试一次值得
        reset_count = store.reset_transfer_items(int(job_id), ("failed", "skipped"))
        log.info("任务 #%s 重试：%s 条失败/跳过明细恢复为待处理", job_id, reset_count)
        counts = store.transfer_item_counts(int(job_id))
        bytes_info = store.transfer_item_bytes(int(job_id))
        store.update_transfer_job(
            int(job_id), status="queued", current_file="", finished_at="",
            completed_files=counts.get("completed", 0),
            completed_bytes=bytes_info["completed"],
            failed_files=counts.get("failed", 0),
            skipped_files=counts.get("skipped", 0),
            total_bytes=bytes_info["total"],
            message="已重新排队",
        )
        self.notify()
        self._emit_progress(job_id, {"type": "reset"})
        return store.get_transfer_job(job_id)

    # ---------- 队列 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.next_queued_transfer_job()
            if job is None:
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue
            job_id = int(job["id"])
            with self._prefetch_lock:
                waiting_prefetch = self._prefetching_job_id == job_id
                if not waiting_prefetch:
                    self._active_job_id = job_id
                    self._active_phase = "starting"
            if waiting_prefetch:
                # 队首还在预解析：等它完成，别抢同一个任务或打乱队列顺序
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception as e:
                log.exception("秒传任务 #%s 异常", job.get("id"))
                self.store.update_transfer_job(
                    job_id,
                    status="failed",
                    current_file="",
                    message=f"任务异常：{e}",
                    finished_at=iso(utcnow()),
                )
                self._emit_progress(job_id, {"type": "final"})
            finally:
                with self._prefetch_lock:
                    self._active_job_id = None
                    self._active_phase = None
                self._parse_wake.set()
                with self._control_lock:
                    self._controls.pop(job_id, None)

    # ---------- 下一单预解析 ----------
    def _parse_loop(self) -> None:
        """当前任务在秒传时，把队列下一单先解析成明细。"""
        while not self._stop.is_set():
            self._parse_wake.wait(timeout=2.0)
            self._parse_wake.clear()
            if self._stop.is_set():
                break
            job = None
            try:
                job = self._claim_prefetch_job()
                if job is None:
                    continue
                job_id = int(job["id"])
                self._prefetch_job(job)
            except Exception as e:
                detail = str(e)[:300]
                if job is None:
                    log.warning(
                        "预解析线程查询任务失败：%s: %s",
                        type(e).__name__, detail,
                    )
                else:
                    job_id = int(job["id"])
                    log.warning(
                        "任务 #%s 预解析失败：%s: %s",
                        job_id, type(e).__name__, detail,
                    )
                    fresh = self.store.get_transfer_job(job_id)
                    if fresh and fresh.get("status") == "queued":
                        self.store.update_transfer_job(
                            job_id, status="failed", current_file="",
                            message=f"解析失败：{type(e).__name__}: {detail}",
                            finished_at=iso(utcnow()),
                        )
                        self._emit_progress(job_id, {"type": "final"})
            finally:
                with self._prefetch_lock:
                    self._prefetching_job_id = None
                if job is not None:
                    self._wake.set()

    def _claim_prefetch_job(self) -> dict | None:
        """只预解析队首且尚未解析的一单，并占住它避免主线程重复解析。"""
        with self._prefetch_lock:
            if (
                self._active_phase != "transferring"
                or self._prefetching_job_id is not None
            ):
                return None
            job = self.store.next_unparsed_queued_transfer_job()
            if not job:
                self._prefetch_checked_job_id = None
                return None
            job_id = int(job["id"])
            if job_id == self._prefetch_checked_job_id:
                return None
            self._prefetch_checked_job_id = job_id
            self._prefetching_job_id = job_id
            return job

    def _prefetch_job(self, job: dict) -> None:
        store = self.store
        job_id = int(job["id"])
        settings = load_settings(store)
        # 和正在秒传的任务并行时，预解析压低扫描并发，别两边一起抢资源
        settings = {
            **settings,
            "scan_workers": max(1, min(int(settings["scan_workers"]), 8)),
        }
        proxy = self.proxy_resolver() if self.proxy_resolver else None
        with pikpak_api._client(proxy, 30.0) as api_client:
            fresh = store.get_transfer_job(job_id)
            if not fresh or fresh.get("status") != "queued":
                return
            store.update_transfer_job(job_id, message="正在预解析分享链接…")
            total = self._parse_share(job, settings, proxy, api_client)
        if not total:
            fresh = store.get_transfer_job(job_id)
            if fresh and fresh.get("status") == "finished":
                self._emit_progress(job_id, {"type": "final"})
            return
        fresh = store.get_transfer_job(job_id)
        if fresh and fresh.get("status") == "queued":
            store.update_transfer_job(
                job_id, message=f"已预解析 {total} 个文件，等待秒传",
            )
            self._emit_progress(job_id, {"type": "snapshot"})
            log.info("任务 #%s 预解析完成：%s 个文件", job_id, total)

    def _parse_share(self, job: dict, settings: dict, proxy,
                     api_client: httpx.Client) -> int:
        """解析分享并落明细；超过单任务上限时自动拆成多个排队任务。"""
        store = self.store
        job_id = int(job["id"])
        files, pass_code_token, share_title = list_share_files(
            store, job["share_url"], job.get("pass_code") or "", proxy,
            scan_workers=settings["scan_workers"], client=api_client,
        )
        base_title = derive_share_title(files, share_title)
        groups = plan_share_groups(files, SPLIT_FILES, base_title)
        if len(groups) > 1:
            # 解析期间被暂停/终止时不再生成子任务，避免取消一个却跑出一串
            fresh = store.get_transfer_job(job_id)
            if not fresh or fresh.get("status") not in ("queued", "running"):
                return 0
            created = store.split_transfer_job(job, groups, pass_code_token)
            if not created:
                return 0
            total = _to_int(created[0].get("total_files")) or len(groups[0]["items"])
            log.info(
                "任务 #%s 共 %s 个文件，超过单任务上限 %s，已自动拆分为 %s 个子任务",
                job_id, len(files), SPLIT_FILES, len(groups),
            )
            return total
        store.add_transfer_items(job_id, files)
        total = len(files)
        bytes_info = store.transfer_item_bytes(job_id)
        fields: dict[str, Any] = {
            "total_files": total,
            "total_bytes": bytes_info["total"],
            "pass_code_token": pass_code_token,
        }
        if base_title:
            fields["title"] = base_title
        if not total:
            # 解析时可能刚好被终止，不能把 canceled/failed 覆盖成 finished
            fresh = store.get_transfer_job(job_id)
            if fresh and fresh.get("status") in ("canceled", "failed"):
                store.update_transfer_job(job_id, **fields)
            else:
                fields.update(
                    status="finished",
                    message="分享里没有可秒传的文件",
                    finished_at=iso(utcnow()),
                )
                store.update_transfer_job(job_id, **fields)
            return total
        store.update_transfer_job(job_id, **fields)
        return total

    # ---------- 任务执行 ----------
    def _run_job(self, job: dict) -> None:
        """一个任务复用 HTTP 连接。

        PikPak 分享接口必须走代理，每次重建客户端都要重做一遍
        SOCKS5 + TLS 握手（实测单次 3~6 秒），按文件建连接会慢得离谱。
        CDN 采样也复用一条直连，省掉每个文件的 TLS 握手。
        """
        proxy = self.proxy_resolver() if self.proxy_resolver else None
        with pikpak_api._client(proxy, 30.0) as api_client, httpx.Client(
            timeout=40.0, follow_redirects=True, trust_env=False,
        ) as cdn_client:
            self._run_job_inner(job, api_client, cdn_client)

    def _run_job_inner(self, job: dict, api_client: httpx.Client,
                       cdn_client: httpx.Client) -> None:
        store = self.store
        job_id = int(job["id"])
        settings = load_settings(store)
        ctrl = self._control(job_id)
        ctrl["pause"].clear()
        ctrl["cancel"].clear()
        with self._prefetch_lock:
            self._active_phase = "parsing"
        # 入队和点按钮可能撞在一起：以库里的状态为准，别跑到一半才发现已取消
        fresh = store.get_transfer_job(job_id)
        if fresh and fresh.get("status") == "canceled":
            self._emit_progress(job_id, {"type": "final"})
            return
        if fresh and fresh.get("status") == "paused":
            ctrl["pause"].set()
        proxy = self.proxy_resolver() if self.proxy_resolver else None
        existing_item = store.list_transfer_items(job_id, limit=1)
        resume_existing = bool(existing_item)
        prefetched = False
        if resume_existing:
            job = store.get_transfer_job(job_id) or job
            total = _to_int(job.get("total_files")) or len(
                store.list_transfer_items(job_id, limit=5000)
            )
            prefetched = "预解析" in str(job.get("message") or "")
            store.update_transfer_job(
                job_id, status="running", started_at=iso(utcnow()),
                current_file="", message="继续秒传已有明细…",
            )
            log.info("任务 #%s 已有 %s 条明细，跳过重新解析", job_id, total)
        else:
            store.update_transfer_job(
                job_id, status="running", started_at=iso(utcnow()),
                current_file="", message="正在解析分享链接…",
            )
            total = self._parse_share(job, settings, proxy, api_client)
            if not total:
                fresh = store.get_transfer_job(job_id)
                if fresh and fresh.get("status") == "running":
                    store.update_transfer_job(
                        job_id, status="failed",
                        message="解析未产生可秒传文件",
                        finished_at=iso(utcnow()),
                    )
                self._emit_progress(job_id, {"type": "final"})
                return

        job = store.get_transfer_job(job_id) or job
        pass_code_token = str(job.get("pass_code_token") or "")
        counters = {
            "completed": _to_int(job.get("completed_files")),
            "failed": _to_int(job.get("failed_files")),
            "skipped": _to_int(job.get("skipped_files")),
        }
        bytes_info = store.transfer_item_bytes(job_id)
        completed_bytes = bytes_info["completed"]
        total_bytes = bytes_info["total"]
        # 光鸭在国内可直连；代理是给 PikPak 分享接口用的，走代理反而握手超时
        auth = GuangyaAuth(store)
        target_folder_id = str(job.get("target_folder_id") or "")
        target_folder_path = str(job.get("target_folder_path") or "/")
        folder_note = ""
        if target_folder_id:
            try:
                ok, reason = auth.check_folder(target_folder_id)
            except GuangyaAuthError:
                raise
            except Exception as e:
                # 校验接口网络抖动（超时/代理握手失败等）：先按原目录继续，
                # 具体能不能传由后面的文件级请求决定，别一上来就把整单判死
                log.warning("秒传目标目录校验失败（%s），先按原目录继续", e)
                ok, reason = True, ""
            if not ok:
                # 目录被删了或换了账号：退回根目录，别让整单都报「父目录不存在」
                log.warning("秒传目标目录 %s 不可用（%s），改用光鸭根目录", target_folder_id, reason)
                folder_note = f"原目录不可用（{reason}），已改用根目录"
                target_folder_id, target_folder_path = "", "/"
                auth.set_folder("", "/")

        store.update_transfer_job(
            job_id, total_files=total,
            total_bytes=total_bytes,
            completed_bytes=completed_bytes,
            target_folder_id=target_folder_id,
            target_folder_path=target_folder_path,
            pass_code_token=pass_code_token,
            message=(
                (f"继续秒传，共 {total} 个文件"
                 if resume_existing and not prefetched
                 else f"已解析 {total} 个文件，开始秒传")
                + (f"（{folder_note}）" if folder_note else "")
            ),
        )
        if resume_existing:
            self._emit_progress(job_id, {"type": "snapshot"})
        else:
            self._emit_progress(job_id, {"type": "parsed", "status": "running"})

        with self._prefetch_lock:
            self._active_phase = (
                "paused" if ctrl["pause"].is_set() else "transferring"
            )
        self._parse_wake.set()
        folder_cache: dict[str, str] = {"": target_folder_id}
        share_id = pikpak.link_id_of(job["share_url"])
        folder_lock = threading.Lock()
        count_lock = threading.Lock()
        stop = threading.Event()
        stop_reason: list[str] = []
        # CID 分片上限在这里当光鸭请求的全局并发上限：两个并发参数取小的
        workers = max(1, min(settings["transfer_workers"], settings["cid_chunk_limit"]))
        total_pending = store.transfer_item_counts(job_id).get("pending", 0)
        # 单文件失败仍逐条写库，但每个任务最多留 20 条 warning，避免刷爆日志
        error_log_lock = threading.Lock()
        error_log_count = 0

        def log_item_error(path: str, kind: str, detail: Any) -> None:
            nonlocal error_log_count
            with error_log_lock:
                if error_log_count >= 20:
                    return
                error_log_count += 1
            log.warning("秒传 %s %s：%s", path[:240], kind, str(detail)[:300])

        def transfer_one(item: dict) -> tuple[dict, str, str]:
            raw_path = str(item.get("path") or "")
            path = "/".join(
                sanitize_path_name(part) for part in raw_path.split("/") if part
            )
            if not path:
                path = sanitize_path_name(item.get("name"))
            safe_name = sanitize_path_name(
                item.get("name") or path.rsplit("/", 1)[-1]
            )
            # 暂停：在这里等；终止：直接放回未处理，稍后可以重试
            while ctrl["pause"].is_set():
                if ctrl["cancel"].is_set():
                    return item, "pending", "已终止"
                time.sleep(0.3)
            if ctrl["cancel"].is_set():
                return item, "pending", "已终止"
            if stop.is_set():
                return item, "skipped", "前一个文件遇到登录问题，任务已中止"
            try:
                gcid = str(item.get("gcid") or "").strip()
                if not gcid:
                    # 没有 GCID 就没得秒传，连光鸭都不用调，也不用建目录
                    return item, "skipped", "PikPak 没返回 GCID，无法秒传"
                cid = str(item.get("cid") or "").strip()
                if not cid:
                    # 光鸭靠 CID 匹配资源库：下载 3×20KB 样本现算，算完存库下次直接用
                    cid = compute_item_cid(
                        store, proxy, share_id, pass_code_token, item,
                        api_client=api_client, cdn_client=cdn_client,
                    )
                    if cid:
                        item["cid"] = cid
                relative_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                # 建目录串行：并发建同一个目录会重复创建、拿不到稳定 id
                with folder_lock:
                    parent_id = self._ensure_folder(
                        auth, folder_cache, target_folder_id, relative_dir,
                    )
                result = auth.flash_upload(
                    name=safe_name,
                    size=_to_int(item.get("size_bytes")),
                    gcid=gcid,
                    cid=cid,
                    parent_id=parent_id,
                )
                if result["ok"]:
                    return item, "completed", ""
                if result["status"] == "miss":
                    return item, "skipped", result["error"]
                if result["status"] == "space":
                    # 空间不足是账号级问题，继续传每个都会失败
                    stop.set()
                    stop_reason.append(f"光鸭空间不足：{result['error']}")
                    return item, "failed", result["error"]
                return item, "failed", result["error"]
            except GuangyaAuthError as e:
                # 登录态失效：通知其它线程别再发请求
                stop.set()
                message = str(e)[:300]
                stop_reason.append(message)
                return item, "failed", message
            except GuangyaLoginError as e:
                # 单个文件/目录被光鸭拒绝（目录名不合法等）：只记这一条，继续后面
                log_item_error(path, "被光鸭拒绝", e)
                return item, "failed", str(e)[:300]
            except Exception as e:
                detail = str(e)
                log_item_error(path, f"失败（{type(e).__name__}）", detail)
                return item, "failed", f"{type(e).__name__}: {detail[:300]}"

        if total_pending:
            # 分批取明细并提交，20k 文件也不会一次性堆出两万个 Future
            batch_size = max(workers * 8, 100)
            last_id = 0
            processed = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while True:
                    batch = store.list_pending_transfer_items(
                        job_id, after_id=last_id, limit=batch_size,
                    )
                    if not batch:
                        break
                    last_id = int(batch[-1]["id"])
                    futures = [pool.submit(transfer_one, item) for item in batch]
                    for future in as_completed(futures):
                        item, status, error = future.result()
                        processed += 1
                        if status == "pending":
                            continue   # 被终止，保持未处理状态
                        with count_lock:
                            counters[status] = counters.get(status, 0) + 1
                            if status == "completed":
                                completed_bytes += _to_int(item.get("size_bytes"))
                            snapshot = dict(counters)
                            done_bytes = completed_bytes
                        store.update_transfer_item_and_progress(
                            int(item["id"]),
                            job_id,
                            status=status,
                            error=error,
                            cid=str(item.get("cid") or ""),
                            completed_files=snapshot["completed"],
                            completed_bytes=done_bytes,
                            failed_files=snapshot["failed"],
                            skipped_files=snapshot["skipped"],
                            current_file=str(item.get("path") or ""),
                            message=(
                                f"正在秒传 {processed}/{total_pending}"
                                f"（{workers} 并发）"
                            ),
                        )
                        self._emit_progress(job_id, {
                            "type": "progress",
                            "status": "running",
                            "completed_files": snapshot["completed"],
                            "failed_files": snapshot["failed"],
                            "skipped_files": snapshot["skipped"],
                            "completed_bytes": done_bytes,
                        })
                    if ctrl["cancel"].is_set() or stop_reason:
                        break

        if ctrl["cancel"].is_set():
            store.update_transfer_job(
                job_id, status="canceled", current_file="",
                completed_files=counters["completed"],
                completed_bytes=completed_bytes,
                failed_files=counters["failed"],
                skipped_files=counters["skipped"],
                message="已终止",
                finished_at=iso(utcnow()),
            )
            self._emit_progress(job_id, {"type": "final"})
            return

        if stop_reason:
            store.update_transfer_job(
                job_id, status="failed", current_file="",
                completed_files=counters["completed"],
                completed_bytes=completed_bytes,
                failed_files=counters["failed"],
                skipped_files=counters["skipped"],
                message=f"任务已停止：{stop_reason[0]}",
                finished_at=iso(utcnow()),
            )
            self._emit_progress(job_id, {"type": "final"})
            return

        # 收尾：没勾选的待处理条目要标成「跳过」，否则任务已结束但计数对不上
        # （比如显示 80/100，剩 20 条永远停在 pending，看着像卡住）
        unselected = store.skip_unselected_items(job_id)
        if unselected:
            counters["skipped"] += unselected
            log.info("任务 #%s 有 %s 个文件未勾选，已跳过", job_id, unselected)

        store.update_transfer_job(
            job_id, status="finished", current_file="",
            completed_files=counters["completed"],
            completed_bytes=completed_bytes,
            failed_files=counters["failed"],
            skipped_files=counters["skipped"],
            message=(
                f"完成：秒传成功 {counters['completed']} 个，"
                f"未命中/跳过 {counters['skipped']} 个，"
                f"失败 {counters['failed']} 个"
            ),
            finished_at=iso(utcnow()),
        )
        self._emit_progress(job_id, {"type": "final"})

    @staticmethod
    def _ensure_folder(auth: GuangyaAuth, cache: dict[str, str],
                       root_id: str, relative_dir: str) -> str:
        """按需在光鸭建出分享里的目录层级，返回该目录的 parentId。"""
        if not relative_dir:
            return root_id
        parent_id = root_id
        built = ""
        for part in relative_dir.split("/"):
            if not part:
                continue
            part = sanitize_path_name(part)
            built = f"{built}/{part}" if built else part
            if built not in cache:
                cache[built] = auth.create_folder(part, parent_id)
            parent_id = cache[built]
        return parent_id
