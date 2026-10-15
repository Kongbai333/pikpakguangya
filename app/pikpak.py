"""分享链接有效性校验。

做法（实测得出，不是猜的）：GET 分享页，解析 Nuxt SSR 里的 __NUXT_DATA__，
读 pinia.share.data.share_status：

    OK               链接可直接访问，title 字段就是文件名
    PASS_CODE_EMPTY  链接存在，但需要提取码
    NOT_FOUND        分享已被删除 / 撤销 / 不存在

已知限制：提取码是否正确无法通过这种方式验证（?pass_code= 参数不生效，
SSR 只渲染无码状态）。要校验码得逆向 PikPak 私有 API，本模块不做。

__NUXT_DATA__ 是 devalue 的扁平格式：数组里放值，数字是「指向数组下标的引用」。
_resolve 递归解引用；如果 PikPak 改了前端，解析会失败，此时回退到 <title> 启发式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from app.proxy import ProxyConfig
from app import pikpak_api

log = logging.getLogger("pikpak")

SHARE_URL = "https://mypikpak.com/s/{sid}"

STATUS_OK = "OK"
STATUS_NEED_CODE = "PASS_CODE_EMPTY"
STATUS_GONE = "NOT_FOUND"
# 只有详情接口（app/pikpak_api.py）能判出来：HTML 那条路永远分不清
# 「没给码」和「码不对」。注意它不属于「失效」——链接本身还在。
STATUS_BAD_CODE = "PASS_CODE_ERROR"
STATUS_ERROR = "ERROR"

VALID_STATUSES = (STATUS_OK, STATUS_NEED_CODE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NUXT_RE = re.compile(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
LINK_ID_RE = re.compile(r"/s/([A-Za-z0-9_\-]{4,})")


@dataclass
class CheckResult:
    link_id: str
    status: str
    title: str = ""
    file_num: int | None = None
    size_bytes: int | None = None
    expire_seconds: int | None = None
    error: str = ""
    source: str = ""

    @property
    def is_valid(self) -> bool:
        return self.status in VALID_STATUSES


# ---------- __NUXT_DATA__ 解析 ----------
def _resolve(arr: list[Any], i: Any, seen: dict[int, Any] | None = None, depth: int = 0) -> Any:
    """devalue 扁平数组解引用。整数视为下标引用（PikPak 的数字都是字符串，实测无歧义）。"""
    if seen is None:
        seen = {}
    if depth > 60 or not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(arr):
        return i
    if i in seen:
        return seen[i]
    v = arr[i]
    if isinstance(v, list):
        if v and v[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
            return _resolve(arr, v[1], seen, depth + 1)
        out: list[Any] = []
        seen[i] = out
        for x in v:
            out.append(_resolve(arr, x, seen, depth + 1))
        return out
    if isinstance(v, dict):
        out_d: dict[str, Any] = {}
        seen[i] = out_d
        for k, x in v.items():
            out_d[k] = _resolve(arr, x, seen, depth + 1)
        return out_d
    seen[i] = v
    return v


def _find_share_node(obj: Any, path: str = "") -> tuple[str, dict] | None:
    """在解引用后的对象树里找第一个带 share_status 的字典，优先 pinia.share.data。"""
    best: tuple[str, dict] | None = None
    stack = [(path, obj, 0)]
    while stack:
        p, node, depth = stack.pop()
        if depth > 14:
            continue
        if isinstance(node, dict):
            if isinstance(node.get("share_status"), str) and node["share_status"]:
                if "/pinia/share/data" in p:
                    return (p, node)
                if best is None:
                    best = (p, node)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    stack.append((f"{p}/{k}", v, depth + 1))
        elif isinstance(node, list):
            for idx, v in enumerate(node[:50]):
                if isinstance(v, (dict, list)):
                    stack.append((f"{p}[{idx}]", v, depth + 1))
    return best


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_share_html(html: str) -> dict:
    """从分享页 HTML 提取状态。返回 {status, title, file_num, expire_seconds, source}。"""
    m = NUXT_RE.search(html or "")
    if m:
        try:
            arr = json.loads(m.group(1))
            node = _find_share_node(_resolve(arr, 0))
            if node:
                _, data = node
                status = str(data.get("share_status") or "").strip()
                if status:
                    return {
                        "status": status,
                        "title": str(data.get("title") or "").strip(),
                        "file_num": _to_int(data.get("file_num")),
                        "expire_seconds": _to_int(data.get("expiration_left_seconds")),
                        "source": "nuxt",
                    }
        except (ValueError, TypeError, RecursionError) as e:  # 前端改版时不要炸掉整轮校验
            log.warning("__NUXT_DATA__ 解析失败，回退 <title>：%s", e)

    return _parse_title_fallback(html or "")


def _parse_title_fallback(html: str) -> dict:
    """兜底：<title> 形如「文件名 Shared by 昵称 | PikPak」。

    无文件名 → 需要提取码；连昵称都没有 → 分享不存在。
    """
    m = TITLE_RE.search(html or "")
    title = (m.group(1) if m else "").strip()
    head = title[:-len("| PikPak")].strip() if title.endswith("| PikPak") else title.strip()
    blank = {"file_num": None, "expire_seconds": None, "source": "title"}
    if head.startswith("Shared by"):
        # 「Shared by 8**8」= 没渲染出文件名 → 需要提取码；「Shared by」= 连昵称都没有 → 分享不存在
        sharer = head[len("Shared by"):].strip()
        return {"status": STATUS_NEED_CODE if sharer else STATUS_GONE, "title": "", **blank}
    left, _, _ = head.partition(" Shared by ")
    if left.strip():
        return {"status": STATUS_OK, "title": left.strip(), **blank}
    return {"status": STATUS_GONE, "title": "", **blank}


# ---------- 单个链接校验 ----------
def link_id_of(url_or_id: str) -> str:
    m = LINK_ID_RE.search(url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def fetch_share_html(url: str, timeout: int = 25, proxy: ProxyConfig | None = None,
                     trust_env: bool = False) -> str:
    """抓分享页。

    用 httpx 而不是 urllib，原因有两个：
    1. 可访问的分享会 302 跳到 /s/{id}/{token}，httpx 能自动跟随；
    2. 装了 socksio 后 httpx 能直接走 socks5 代理，不用自己写 SOCKS 握手。

    trust_env 默认 False：**只按本工具的代理配置走，不读系统/环境变量代理**。
    原因很实际——Windows 上开了 Clash 会写系统代理，httpx 默认会拿去用，
    于是「不使用代理（直连）」其实走了代理，甚至本地地址都被代理成 502。
    """
    import httpx

    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
        "trust_env": trust_env,
        "headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }
    proxy_url = proxy.httpx_proxy() if proxy else None
    if proxy_url:
        kwargs["proxy"] = proxy_url
    with httpx.Client(**kwargs) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def check_share(url_or_id: str, timeout: int = 25, proxy: ProxyConfig | None = None,
                pass_code: str | None = None,
                api_proxy: ProxyConfig | None = None) -> CheckResult:
    """同步校验一个链接。网络异常返回 status=ERROR，不抛异常。

    优先走 PikPak 详情接口（能拿到真文件名，还能判出「提取码错误」），
    它需要代理、且可能因为改版失效 —— 所以只要 `ok=False` 就回退到抓分享页。
    """
    sid = link_id_of(url_or_id)
    if not sid:
        return CheckResult(link_id="", status=STATUS_ERROR, error="无法从输入中提取 link_id")

    if api_proxy is not None and api_proxy.enabled:
        detail = pikpak_api.fetch_share_detail(sid, pass_code, api_proxy, timeout)
        if detail.get("ok"):
            return CheckResult(
                link_id=sid,
                status=detail["status"],
                title=detail["title"],
                file_num=detail["file_num"],
                size_bytes=detail.get("size_bytes"),
                expire_seconds=detail["expire_seconds"],
                source="api",
            )
        # 详情接口没查成（地区限制 / 改版 / 网络）→ 往下走 HTML

    try:
        html = fetch_share_html(SHARE_URL.format(sid=sid), timeout=timeout, proxy=proxy)
    except Exception as e:  # 超时、DNS、连接重置、被限流……
        return CheckResult(link_id=sid, status=STATUS_ERROR, error=f"{type(e).__name__}: {e}")

    parsed = parse_share_html(html)
    return CheckResult(
        link_id=sid,
        status=parsed["status"],
        title=parsed["title"],
        file_num=parsed["file_num"],
        expire_seconds=parsed["expire_seconds"],
        source=parsed["source"],
    )


def _api_proxy(store, default_proxy: ProxyConfig | None,
               resolver=None) -> ProxyConfig | None:
    """详情接口用哪个代理。

    和分享页校验（可以直连）是两件事：这个接口**必须**走代理，
    直连一律 PROHIBITED。

    resolver：可选的「当前该走哪个出口」回调（管理页注入的是「当前使用那个账号
    绑定的直接代理」）。
    """
    if store is not None and store.get_state("pikpak.api.enabled", "1") == "0":
        return None
    if resolver is not None:
        try:
            got = resolver()
        except Exception:      # 账号出口不可用时不要偷偷换成另一条代理
            got = None
        return got.normalized() if got is not None and got.enabled else None
    return None


# ---------- 批量 / 定时校验 ----------
async def check_pending(store, cfg, limit: int | None = None,
                        default_proxy: ProxyConfig | None = None,
                        api_proxy_resolver=None) -> dict:
    """校验一批「没校验过 / 过期需重校验」的链接。

    连续失败到 max_consecutive_errors 次就中止本批（多半是被限流了），
    已经写库的结果保留，下一轮接着来。
    """
    proxy = None
    api = _api_proxy(store, default_proxy, api_proxy_resolver)
    rows = store.shares_to_check(
        limit=limit or getattr(cfg, "batch_size", 100),
        recheck_after_hours=cfg.recheck_after_hours,
        error_retry_minutes=getattr(cfg, "error_retry_minutes", 30),
    )
    tally = {"checked": 0, "ok": 0, "need_code": 0, "gone": 0, "error": 0, "aborted": False}
    if not rows:
        return tally

    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    max_errors = max(1, getattr(cfg, "max_consecutive_errors", 5))
    consecutive_errors = 0
    aborted = False

    async def one(row: dict) -> None:
        nonlocal consecutive_errors, aborted
        async with sem:
            if aborted:
                return
            # 抖动一下，避免固定节奏被判定为爬虫
            await asyncio.sleep(max(0.0, cfg.min_delay_seconds) * (0.5 + random.random()))
            res = await asyncio.to_thread(
                check_share, row["url"], cfg.timeout_seconds, proxy,
                row.get("pass_code"), api,
            )
            store.save_check(
                row["id"], res.status, title=res.title, file_num=res.file_num,
                size_bytes=res.size_bytes, expire_seconds=res.expire_seconds,
                error=res.error,
            )
            tally["checked"] += 1
            key = {STATUS_OK: "ok", STATUS_NEED_CODE: "need_code",
                   STATUS_GONE: "gone", STATUS_ERROR: "error"}.get(res.status, "error")
            tally[key] += 1
            if res.status == STATUS_ERROR:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    aborted = True
                    tally["aborted"] = True
                    log.warning("连续 %s 次校验失败，中止本批（最后一次：%s）", consecutive_errors, res.error)
            else:
                consecutive_errors = 0
            log.debug("%s -> %s %s", row["link_id"], res.status, res.title)

    await asyncio.gather(*(one(r) for r in rows))
    log.info("校验完成：%s", tally)
    return tally


async def check_ids(store, cfg, ids: list[int],
                    default_proxy: ProxyConfig | None = None,
                    api_proxy_resolver=None) -> dict:
    """校验指定的这几条（管理页「重新检测」用），返回每条的结果。

    和 check_pending 的区别：不挑「过期没」，你说检哪条就检哪条；
    也不做连续失败熔断（用户明确点了，就该给结果）。
    """
    proxy = None
    api = _api_proxy(store, default_proxy, api_proxy_resolver)
    rows = store.shares_by_ids(ids)
    tally: dict[str, Any] = {
        "checked": 0, "ok": 0, "need_code": 0, "bad_code": 0,
        "gone": 0, "error": 0, "results": [],
    }
    if not rows:
        return tally

    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    lock = asyncio.Lock()
    bucket = {
        STATUS_OK: "ok", STATUS_NEED_CODE: "need_code",
        STATUS_BAD_CODE: "bad_code", STATUS_GONE: "gone",
    }

    async def one(row: dict) -> None:
        async with sem:
            await asyncio.sleep(max(0.0, cfg.min_delay_seconds) * (0.5 + random.random()))
            res = await asyncio.to_thread(
                check_share, row["url"], cfg.timeout_seconds, proxy,
                row.get("pass_code"), api,
            )
            store.save_check(
                row["id"], res.status, title=res.title, file_num=res.file_num,
                size_bytes=res.size_bytes, expire_seconds=res.expire_seconds,
                error=res.error,
            )
            async with lock:
                tally["checked"] += 1
                tally[bucket.get(res.status, "error")] += 1
                tally["results"].append({
                    "id": int(row["id"]),
                    "link_id": row["link_id"],
                    "status": res.status,
                    "title": res.title,
                    "file_num": res.file_num,
                    "expire_seconds": res.expire_seconds,
                    "error": res.error,
                })

    await asyncio.gather(*(one(r) for r in rows))
    tally["results"].sort(key=lambda r: r["id"])
    log.info("按 id 校验 %s 条：ok=%s need_code=%s gone=%s error=%s",
             tally["checked"], tally["ok"], tally["need_code"], tally["gone"], tally["error"])
    return tally


async def run_forever(store, cfg, batch: int | None = None,
                      default_proxy: ProxyConfig | None = None,
                      api_proxy_resolver=None) -> None:
    """后台常驻：每 interval_minutes 跑一批。代理每轮重新读，改完不用重启。"""
    while True:
        try:
            await check_pending(store, cfg, limit=batch, default_proxy=default_proxy,
                                api_proxy_resolver=api_proxy_resolver)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("定时校验出错，等下一轮")
        await asyncio.sleep(max(1, cfg.interval_minutes) * 60)
