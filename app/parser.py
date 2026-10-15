"""消息解析：从群消息里抽出 mypikpak 分享链接、提取码、备注。

这一层是整个采集链路最容易出错的地方，规则都写在这里并配了测试：
链接和提取码经常分两条消息发出，用 CodeMerger 合并。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# 分享链接域名。PikPak 国内是 mypikpak.com / mypikpak.net，国际站是 pikpak.com。
SHARE_HOSTS: tuple[str, ...] = ("mypikpak.com", "mypikpak.net", "pikpak.com")

_HOST_ALT = "|".join(h.replace(".", r"\.") for h in SHARE_HOSTS)

# 例：https://mypikpak.com/s/EXAMPLEShareId000000000001 / mypikpak.com/s/xxx
LINK_RE = re.compile(
    rf"(?:https?://)?(?:www\.|m\.)?(?:{_HOST_ALT})/s/(?P<sid>[A-Za-z0-9_\-]{{4,}})",
    re.I,
)

# 链接自带提取码：?pass_code=1234
PASS_CODE_IN_URL_RE = re.compile(r"[?&]pass_?code=([A-Za-z0-9]{1,16})", re.I)

# 「提取码：f766」「密码 f766」「code:1234」等
CODE_LABEL_RE = re.compile(
    r"(?:提取码|提取密码|访问码|分享码|分享密码|口令|密码|pass_?code|passcode|code|pwd)"
    r"\s*[:：=]?\s*([A-Za-z0-9]{4,16})",
    re.I,
)

# 「提取码：无」「密码 不需要」
NO_CODE_RE = re.compile(
    r"(?:提取码|提取密码|访问码|分享码|密码|口令)\s*[:：=]?\s*(?:无|没有|不需要|none|n/?a|no)",
    re.I,
)

# 整条消息就是一个 4 位码（很常见）
BARE_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{4,}")
FWD_PREFIX_RE = re.compile(r"^\s*(?:转发自|转自|Forwarded from)\s*[:：]?\s*\S*\s*", re.I)
WS_RE = re.compile(r"[ \t\u3000]+")

REMARK_MAX_LEN = 200


@dataclass
class LinkHit:
    url: str
    link_id: str


@dataclass
class ParsedShare:
    url: str
    link_id: str
    pass_code: str | None
    remark: str
    raw_text: str
    # True 表示这条只是「给已有链接补提取码」，调用方应更新而不是当作新命中计数
    update_only: bool = False


def _normalize_url(raw: str) -> str:
    u = raw.strip()
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    u = re.sub(r"^http://", "https://", u, flags=re.I)  # 站点只有 https，统一成 https
    u = re.sub(r"^https://(?:www\.|m\.)", "https://", u, flags=re.I)
    return u


def extract_links(text: str) -> list[LinkHit]:
    """按出现顺序抽取分享链接，同一 link_id 只留一次（忽略域名差异）。"""
    if not text:
        return []
    out: list[LinkHit] = []
    seen: set[str] = set()
    for m in LINK_RE.finditer(text):
        sid = m.group("sid")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(LinkHit(url=_normalize_url(m.group(0)), link_id=sid))
    return out


def is_bare_code(text: str) -> str | None:
    """整条消息就是一个 4 位码（且不含链接）时返回该码。"""
    if not text:
        return None
    t = text.strip()
    if LINK_RE.search(t):
        return None
    return t if BARE_CODE_RE.match(t) else None


def find_pass_code(text: str, links: list[LinkHit] | None = None) -> str | None:
    """抽取提取码。显式「无」返回 None。"""
    if not text:
        return None
    for hit in links or extract_links(text):
        m = PASS_CODE_IN_URL_RE.search(hit.url)
        if m:
            return m.group(1)
    if NO_CODE_RE.search(text):
        return None
    m = CODE_LABEL_RE.search(text)
    if m:
        return m.group(1)
    return is_bare_code(text)


def build_remark(text: str, links: list[LinkHit] | None = None) -> str:
    """备注 = 剔除链接与提取码之后的剩余文本，取首行有效内容。"""
    if not text:
        return ""
    s = text
    s = MARKDOWN_LINK_RE.sub(lambda m: m.group(1), s)  # [标题](url) -> 标题
    for hit in links or extract_links(text):
        s = s.replace(hit.url, " ")
    s = LINK_RE.sub(" ", s)
    s = PASS_CODE_IN_URL_RE.sub(" ", s)
    s = CODE_LABEL_RE.sub(" ", s)
    s = MENTION_RE.sub(" ", s)
    s = FWD_PREFIX_RE.sub("", s)

    for line in s.splitlines():
        line = WS_RE.sub(" ", line).strip(" \t-—·:：|")
        if line and not BARE_CODE_RE.match(line):
            return line[:REMARK_MAX_LEN]
    return ""


def parse_message(text: str) -> list[ParsedShare]:
    """一条消息 → 若干 ParsedShare（多链接时提取码套用到全部）。"""
    links = extract_links(text)
    if not links:
        return []
    code = find_pass_code(text, links)
    remark = build_remark(text, links)
    return [
        ParsedShare(
            url=hit.url,
            link_id=hit.link_id,
            pass_code=code,
            remark=remark,
            raw_text=text,
        )
        for hit in links
    ]


def keep_message(text: str, include: list[str] | None = None, exclude: list[str] | None = None) -> bool:
    """关键词白/黑名单过滤，大小写不敏感。"""
    t = (text or "").lower()
    if exclude and any(k.lower() in t for k in exclude if k):
        return False
    if include and not any(k.lower() in t for k in include if k):
        return False
    return True


class CodeMerger:
    """链接和提取码分两条消息发出时，把后发的码补到前一条链接上。

    常见形态：
        消息A: https://mypikpak.com/s/xxxx
        消息B: f766          <- 同一发送者，几十秒内
    """

    def __init__(self, window_seconds: int = 90) -> None:
        self.window = timedelta(seconds=max(0, window_seconds))
        self._pending: dict[tuple[int, int], tuple[ParsedShare, datetime]] = {}

    def observe(
        self,
        chat_id: int,
        sender_id: int | None,
        text: str,
        now: datetime,
    ) -> list[ParsedShare]:
        key = (chat_id, sender_id or 0)
        shares = parse_message(text)
        if shares:
            if not shares[0].pass_code:
                self._pending[key] = (shares[0], now)
            else:
                self._pending.pop(key, None)
            return shares

        code = is_bare_code(text)
        if not code:
            return []
        item = self._pending.pop(key, None)
        if not item:
            return []
        share, ts = item
        if now - ts > self.window:
            return []
        share.pass_code = code
        share.update_only = True
        return [share]

    def clear(self) -> None:
        self._pending.clear()
