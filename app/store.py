"""SQLite 存储：原始消息留档 + 分享链接去重 + 校验结果 + 查询分页。

一张 shares 表 = 一条链接一行（唯一），备注单独放 share_remarks，
因为同一链接常被多人多次转发、每次备注不同。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

# datetime 的 time 起别名：直接叫 time 会遮蔽上面那个 time 模块，
# _cached() 里的 time.monotonic() 就会变成 datetime.time.monotonic()。
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app import timeutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      INTEGER NOT NULL,
  chat_title   TEXT    NOT NULL DEFAULT '',
  message_id   INTEGER NOT NULL,
  sender_id    INTEGER,
  sender_name  TEXT    NOT NULL DEFAULT '',
  posted_at    TEXT    NOT NULL,
  raw_text     TEXT    NOT NULL,
  link_id      TEXT,
  created_at   TEXT    NOT NULL,
  UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_link ON messages(link_id);
-- 按群统计（消息数 / 去重链接数）要按 chat_id 分组再数 link_id。
-- 只有 (chat_id, message_id) 那个唯一索引的话，COUNT(DISTINCT link_id)
-- 还得回表取 link_id；补一个覆盖索引就能只走索引。
CREATE INDEX IF NOT EXISTS idx_messages_chat_link ON messages(chat_id, link_id);
-- 实时活动条按时间倒着取最近 N 分钟，没索引就得全表扫
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS shares (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id              TEXT    NOT NULL UNIQUE,
  url                  TEXT    NOT NULL,
  pass_code            TEXT,
  first_seen_at        TEXT    NOT NULL,
  last_seen_at         TEXT    NOT NULL,
  checked_at           TEXT,
  check_status         TEXT,
  check_title          TEXT,
  check_file_num       INTEGER,
  check_size_bytes     INTEGER,
  check_expire_seconds INTEGER,
  check_error          TEXT,
  source               TEXT    NOT NULL DEFAULT '',
  hidden               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shares_status  ON shares(check_status);
CREATE INDEX IF NOT EXISTS idx_shares_checked ON shares(checked_at);

CREATE TABLE IF NOT EXISTS share_remarks (
  share_id   INTEGER NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
  remark     TEXT    NOT NULL,
  chat_title TEXT    NOT NULL DEFAULT '',
  posted_at  TEXT    NOT NULL,
  PRIMARY KEY (share_id, remark)
);
CREATE INDEX IF NOT EXISTS idx_remarks_share ON share_remarks(share_id);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 监听白名单：**按账号分开**。管理页在哪个账号下勾的，就归哪个账号。
-- 每个账号采集时只用自己的勾选（见 app/collector.py 的 Collector）。
CREATE TABLE IF NOT EXISTS watched_chats (
  account_id INTEGER NOT NULL DEFAULT 0,
  chat_id    INTEGER NOT NULL,
  title      TEXT    NOT NULL DEFAULT '',
  username   TEXT    NOT NULL DEFAULT '',
  kind       TEXT    NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  media_enabled INTEGER NOT NULL DEFAULT 0,
  added_at   TEXT    NOT NULL,
  PRIMARY KEY (account_id, chat_id)
);

-- Telegram 媒体只保存元数据和本地缩略图，不缓存原图 / 视频。
CREATE TABLE IF NOT EXISTS media_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    INTEGER NOT NULL DEFAULT 0,
  chat_id       INTEGER NOT NULL,
  chat_title    TEXT    NOT NULL DEFAULT '',
  chat_username TEXT    NOT NULL DEFAULT '',
  message_id    INTEGER NOT NULL,
  grouped_id    INTEGER,
  item_index    INTEGER NOT NULL DEFAULT 0,
  media_type    TEXT    NOT NULL,
  document_id   INTEGER,
  access_hash   INTEGER,
  file_name     TEXT    NOT NULL DEFAULT '',
  mime_type     TEXT    NOT NULL DEFAULT '',
  size_bytes    INTEGER,
  width         INTEGER,
  height        INTEGER,
  duration_sec  INTEGER,
  posted_at     TEXT    NOT NULL DEFAULT '',
  thumb_path    TEXT    NOT NULL DEFAULT '',
  thumb_status  TEXT    NOT NULL DEFAULT 'pending',
  thumb_error   TEXT    NOT NULL DEFAULT '',
  created_at    TEXT    NOT NULL,
  UNIQUE (account_id, chat_id, message_id, item_index)
);
CREATE INDEX IF NOT EXISTS idx_media_created ON media_items(created_at);
CREATE INDEX IF NOT EXISTS idx_media_chat ON media_items(chat_id);
CREATE INDEX IF NOT EXISTS idx_media_type ON media_items(media_type);

-- Telegram 账号：一个账号一个 session 文件。active=1 的是「当前使用」账号，
-- 群列表、校验出口和 PikPak 详情走它；采集按各自的 collect_enabled 开关跑。
-- session 只存文件名，目录是 config.toml 里 telegram.session 所在的那个目录，
-- 这样换项目目录、换盘符都不用改库。
CREATE TABLE IF NOT EXISTS accounts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  phone           TEXT    NOT NULL DEFAULT '',
  alias           TEXT    NOT NULL DEFAULT '',
  user_id         INTEGER,
  first_name      TEXT    NOT NULL DEFAULT '',
  last_name       TEXT    NOT NULL DEFAULT '',
  username        TEXT    NOT NULL DEFAULT '',
  session         TEXT    NOT NULL DEFAULT '',
  proxy_id        INTEGER NOT NULL DEFAULT 0,
  active          INTEGER NOT NULL DEFAULT 0,
  collect_enabled INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT    NOT NULL DEFAULT '',
  last_login_at   TEXT    NOT NULL DEFAULT ''
);

-- 代理池：给 Telegram 账号绑定的出口。
-- http / https / socks5 是标准代理；mtproto 是 tg://proxy 那种 MTProto 代理，
-- 只能用 secret 连 Telegram，代理不了 PikPak 的 HTTP 请求（secret 只在它身上有值）。
CREATE TABLE IF NOT EXISTS proxies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL DEFAULT '',
  proxy_type TEXT NOT NULL DEFAULT 'http',
  addr       TEXT NOT NULL DEFAULT '',
  port       INTEGER NOT NULL DEFAULT 0,
  username   TEXT NOT NULL DEFAULT '',
  password   TEXT NOT NULL DEFAULT '',
  secret     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transfer_jobs (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  share_url          TEXT    NOT NULL,
  pass_code          TEXT    NOT NULL DEFAULT '',
  pass_code_token    TEXT    NOT NULL DEFAULT '',
  title              TEXT    NOT NULL DEFAULT '',
  source             TEXT    NOT NULL DEFAULT 'web',
  target_folder_id   TEXT    NOT NULL DEFAULT '',
  target_folder_path TEXT    NOT NULL DEFAULT '/',
  status             TEXT    NOT NULL DEFAULT 'queued',
  total_files        INTEGER NOT NULL DEFAULT 0,
  total_bytes        INTEGER NOT NULL DEFAULT 0,
  completed_files    INTEGER NOT NULL DEFAULT 0,
  completed_bytes    INTEGER NOT NULL DEFAULT 0,
  failed_files       INTEGER NOT NULL DEFAULT 0,
  skipped_files      INTEGER NOT NULL DEFAULT 0,
  current_file       TEXT    NOT NULL DEFAULT '',
  message            TEXT    NOT NULL DEFAULT '',
  created_at         TEXT    NOT NULL,
  started_at         TEXT    NOT NULL DEFAULT '',
  finished_at        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_transfer_jobs_status ON transfer_jobs(status);
CREATE INDEX IF NOT EXISTS idx_transfer_jobs_created ON transfer_jobs(created_at);

CREATE TABLE IF NOT EXISTS transfer_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES transfer_jobs(id) ON DELETE CASCADE,
  path        TEXT    NOT NULL,
  name        TEXT    NOT NULL,
  size_bytes  INTEGER NOT NULL DEFAULT 0,
  gcid        TEXT    NOT NULL DEFAULT '',
  cid         TEXT    NOT NULL DEFAULT '',
  file_id     TEXT    NOT NULL DEFAULT '',
  status      TEXT    NOT NULL DEFAULT 'pending',
  error       TEXT    NOT NULL DEFAULT '',
  -- 选择性秒传：0 表示这一条被排除在外，不会被秒传。
  -- 默认 1，老库升级后行为和以前完全一致。
  selected    INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL,
  updated_at  TEXT    NOT NULL,
  UNIQUE (job_id, path)
);
CREATE INDEX IF NOT EXISTS idx_transfer_items_job ON transfer_items(job_id);

"""

# 校验状态里这两个算「还有效」
VALID_STATUSES = ("OK", "PASS_CODE_EMPTY")
# 这几个都算「打不开了」：NOT_FOUND 是主动删的，另外几个是 PikPak 自己标的。
# 只按 NOT_FOUND 统计会把「已删除 / 已过期 / 违规资源」漏掉（实测漏了 200+ 条）。
GONE_STATUSES = ("NOT_FOUND", "DELETED", "EXPIRED", "SENSITIVE_RESOURCE")
# 状态码 → 中文。管理页的筛选下拉和后端返回的 status_labels 都用它。
STATUS_LABELS = {
    "OK": "有效",
    "PASS_CODE_EMPTY": "需提取码",
    "NOT_FOUND": "已失效",
    "DELETED": "已删除",
    "EXPIRED": "已过期",
    "SENSITIVE_RESOURCE": "违规资源",
    "ERROR": "检测出错",
    "PASS_CODE_ERROR": "提取码错误",
}

SORT_COLUMNS = {
    "id": "s.id",
    "first_seen_at": "s.first_seen_at",
    "last_seen_at": "s.last_seen_at",
    "pass_code": "s.pass_code",
    "check_status": "s.check_status",
    "check_title": "s.check_title",
    "source": "s.source",
    "hidden": "s.hidden",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def age_seconds(iso_str: str) -> float | None:
    """ISO 时间戳距现在多少秒。空值或解析不了返回 None。

    监听器心跳、任务时间都用它算「多久没更新了」。
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def canonical_chat_id(chat_id: int) -> int:
    """统一成**带 `-100` 前缀**的那种写法。

    同一个超级群有两个 id：dialog 给的是 `-1003408025435`，而 messages 表里存的是
    Telethon 的 `entity.id`（`3408025435`）。以前勾选直接按原样存，
    同一群就会变成两行 —— 监控室里显示成「重复的群」，一个 109 条链接、一个 0 条。

    普通群（-123456 这种）本来就是负的、不带 -100，原样保留。
    """
    n = int(chat_id)
    if n < 0:
        return n
    return int("-100" + str(n))


def chat_id_variants(chat_id: int) -> list[int]:
    """查库时同一个群的两种写法都试一下。返回顺序：原样、另一种。"""
    s = str(chat_id)
    if s.startswith("-100"):
        return [chat_id, int(s[4:])]
    if chat_id > 0:
        return [chat_id, int("-100" + s)]
    return [chat_id]


def _migration_done(c: sqlite3.Connection, name: str) -> bool:
    """一次性数据迁移是否已经跑过。标记存在 state 表里。"""
    row = c.execute(
        "SELECT value FROM state WHERE key = ?", (f"migration.{name}",)
    ).fetchone()
    return bool(row and row["value"] == "1")


def _mark_migration(c: sqlite3.Connection, name: str) -> None:
    c.execute(
        "INSERT INTO state (key, value) VALUES (?, '1')"
        " ON CONFLICT(key) DO UPDATE SET value = '1'",
        (f"migration.{name}",),
    )


class Store:
    # 聚合查询的缓存时长。仪表盘每 5 秒轮询一次，而 stats / chat_stats 都是
    # 全表扫 —— 重复算没有意义。写操作会立刻失效对应缓存，所以这里只兜住
    # 「没人写、但有人一直读」的情况，TTL 不用长。
    CACHE_TTL_SECONDS = 8.0

    def __init__(self, path: Path | str, tz=None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        # 展示用时区。库里存的都是 UTC，只有分桶统计按它切。
        self._tz = tz if tz is not None else timeutil.system_tz()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        # 每次失效都 +1。用来判断「算这份结果的过程中有没有人写过」——
        # 有的话这份结果可能已经过期，不能存进缓存。
        self._cache_gen = 0

    # ---------- 聚合缓存 ----------
    def _cached(self, key: str, loader):
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < self.CACHE_TTL_SECONDS:
                return hit[1]
            generation = self._cache_gen
        value = loader()
        with self._cache_lock:
            if self._cache_gen == generation:
                self._cache[key] = (time.monotonic(), value)
        return value

    def _invalidate(self, keys: tuple[str, ...] | None = None) -> None:
        """失效聚合缓存。keys 为 None 表示全清，空元组表示什么都不用清。"""
        with self._cache_lock:
            self._cache_gen += 1
            if keys is None:
                self._cache.clear()
                return
            for key in keys:
                self._cache.pop(key, None)

    # ---------- 连接 ----------
    def _connect(self) -> sqlite3.Connection:
        # timeout 就是 SQLite 的 busy timeout，别再单独 PRAGMA 设一遍 ——
        # 那会把这里的 15 秒覆盖成更短的值。
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        # WAL 是库级持久设置，只在 init 里设一次；每个连接重复设会额外抢锁。
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self, *, invalidate: tuple[str, ...] | None = None) -> Iterator[sqlite3.Connection]:
        """写事务。

        invalidate：这次写会影响哪些聚合缓存。默认 None = 全部失效（保守，
        新加的写方法不用操心）；确认与聚合无关的热路径（秒传进度、媒体缩略图）
        传空元组，免得把统计缓存冲掉。
        """
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            else:
                self._invalidate(invalidate)
            finally:
                conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        with self._write() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """给已有的库补新列。SQLite 的 ADD COLUMN 很便宜，查 PRAGMA 就行。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(shares)")}
        if "source" not in cols:
            c.execute("ALTER TABLE shares ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        if "hidden" not in cols:
            c.execute("ALTER TABLE shares ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "check_size_bytes" not in cols:
            c.execute("ALTER TABLE shares ADD COLUMN check_size_bytes INTEGER")
        # 统计一律按程序实际采集落库的时间，不能用 Telegram 消息时间。
        # 老数据从留档消息回填，时间字段以前存的是消息时间，也在这次统一纠正。
        #
        # ⚠️ 这三条会重写 shares 全表。以前每次启动都无条件跑一遍：库一大，
        # 启动就慢，而且整个 pikpak.db 文件会被改写（放在 OneDrive 之类的
        # 同步目录里就是每次启动重传几十兆）。现在只跑一次。
        if not _migration_done(c, "shares_backfill_v1"):
            c.execute(
                "UPDATE shares SET first_seen_at = COALESCE(("
                "  SELECT MIN(m.created_at) FROM messages m"
                "   WHERE m.link_id = shares.link_id AND IFNULL(m.created_at,'') <> ''"
                "), first_seen_at)"
            )
            c.execute(
                "UPDATE shares SET last_seen_at = COALESCE(("
                "  SELECT MAX(m.created_at) FROM messages m"
                "   WHERE m.link_id = shares.link_id AND IFNULL(m.created_at,'') <> ''"
                "), last_seen_at)"
            )
            # 老数据没有来源，从留档消息里回填一次
            c.execute(
                "UPDATE shares SET source = COALESCE(("
                "  SELECT m.chat_title FROM messages m"
                "   WHERE m.link_id = shares.link_id AND IFNULL(m.chat_title,'') <> ''"
                "   ORDER BY m.id LIMIT 1"
                "), '')"
                " WHERE IFNULL(source,'') = ''"
            )
            _mark_migration(c, "shares_backfill_v1")

        # 秒传表补列：cid 是光鸭秒传要的内容 ID，file_id 用来向 PikPak 要下载直链
        tcols = {r["name"] for r in c.execute("PRAGMA table_info(transfer_items)")}
        if tcols:
            if "cid" not in tcols:
                c.execute("ALTER TABLE transfer_items ADD COLUMN cid TEXT NOT NULL DEFAULT ''")
            if "file_id" not in tcols:
                c.execute("ALTER TABLE transfer_items ADD COLUMN file_id TEXT NOT NULL DEFAULT ''")
            if "selected" not in tcols:
                # 默认 1：老任务升级后照旧全传，不会因为多了这一列就什么都不传
                c.execute(
                    "ALTER TABLE transfer_items ADD COLUMN selected INTEGER NOT NULL DEFAULT 1"
                )
        jcols = {r["name"] for r in c.execute("PRAGMA table_info(transfer_jobs)")}
        if jcols:
            if "pass_code_token" not in jcols:
                c.execute(
                    "ALTER TABLE transfer_jobs ADD COLUMN pass_code_token"
                    " TEXT NOT NULL DEFAULT ''"
                )
            if "title" not in jcols:
                c.execute(
                    "ALTER TABLE transfer_jobs ADD COLUMN title"
                    " TEXT NOT NULL DEFAULT ''"
                )
            if "total_bytes" not in jcols:
                c.execute(
                    "ALTER TABLE transfer_jobs ADD COLUMN total_bytes"
                    " INTEGER NOT NULL DEFAULT 0"
                )
            if "completed_bytes" not in jcols:
                c.execute(
                    "ALTER TABLE transfer_jobs ADD COLUMN completed_bytes"
                    " INTEGER NOT NULL DEFAULT 0"
                )
            if "source" not in jcols:
                c.execute(
                    "ALTER TABLE transfer_jobs ADD COLUMN source"
                    " TEXT NOT NULL DEFAULT 'web'"
                )

        # watched_chats 以前是「一个群一行」，不区分账号。多账号之后要按账号分开，
        # 而 SQLite 改不了主键 —— 只能建新表把数据搬过去（account_id 先留 0，
        # 等账号表就绪后由 assign_orphan_watched() 挂到「当前使用」的账号上）。
        wcols = {r["name"] for r in c.execute("PRAGMA table_info(watched_chats)")}
        if wcols and "account_id" not in wcols:
            c.execute("ALTER TABLE watched_chats RENAME TO watched_chats_old")
            c.executescript(
                "CREATE TABLE watched_chats ("
                "  account_id INTEGER NOT NULL DEFAULT 0,"
                "  chat_id    INTEGER NOT NULL,"
                "  title      TEXT    NOT NULL DEFAULT '',"
                "  username   TEXT    NOT NULL DEFAULT '',"
                "  kind       TEXT    NOT NULL DEFAULT '',"
                "  enabled    INTEGER NOT NULL DEFAULT 1,"
                "  added_at   TEXT    NOT NULL,"
                "  PRIMARY KEY (account_id, chat_id)"
                ");"
            )
            c.execute(
                "INSERT INTO watched_chats"
                " (account_id, chat_id, title, username, kind, enabled, added_at)"
                " SELECT 0, chat_id, title, username, kind, enabled, added_at"
                "   FROM watched_chats_old"
            )
            c.execute("DROP TABLE watched_chats_old")

        # MTProto 代理的 secret（tg://proxy 链接里的那串）。只有这一种类型用得到。
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(proxies)")}
        if pcols and "secret" not in pcols:
            c.execute("ALTER TABLE proxies ADD COLUMN secret TEXT NOT NULL DEFAULT ''")

        acols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        if acols and "proxy_id" not in acols:
            c.execute("ALTER TABLE accounts ADD COLUMN proxy_id INTEGER NOT NULL DEFAULT 0")
        # 本地备注名：不覆盖 Telegram 同步回来的 first_name / last_name
        if acols and "alias" not in acols:
            c.execute("ALTER TABLE accounts ADD COLUMN alias TEXT NOT NULL DEFAULT ''")
        # 采集开关：老库升级时让「当前使用」的账号保持原行为，开机自动采集
        if acols and "collect_enabled" not in acols:
            c.execute(
                "ALTER TABLE accounts ADD COLUMN collect_enabled INTEGER NOT NULL DEFAULT 0"
            )
            c.execute("UPDATE accounts SET collect_enabled = active")
        wcols = {r["name"] for r in c.execute("PRAGMA table_info(watched_chats)")}
        if wcols and "media_enabled" not in wcols:
            c.execute(
                "ALTER TABLE watched_chats ADD COLUMN media_enabled INTEGER NOT NULL DEFAULT 0"
            )

        # watched_chats 里同一个群可能有两行（带 -100 和不带），统一成带 -100 的写法并合并。
        # 合并规则：enabled 取「有过启用」、标题/用户名/类型取第一个非空、added_at 取最早。
        rows = c.execute(
            "SELECT account_id, chat_id, title, username, kind, enabled, added_at"
            "  FROM watched_chats ORDER BY enabled DESC, added_at"
        ).fetchall()
        if any(int(r["chat_id"]) > 0 for r in rows):
            merged: dict[tuple[int, int], dict] = {}
            order: list[tuple[int, int]] = []
            for r in rows:
                key = (int(r["account_id"]), canonical_chat_id(int(r["chat_id"])))
                if key not in merged:
                    merged[key] = {
                        "title": "", "username": "", "kind": "",
                        "enabled": 0, "added_at": r["added_at"] or "",
                    }
                    order.append(key)
                m = merged[key]
                for f in ("title", "username", "kind"):
                    if not m[f] and (r[f] or ""):
                        m[f] = r[f]
                m["enabled"] = max(m["enabled"], int(r["enabled"] or 0))
                if r["added_at"] and (not m["added_at"] or r["added_at"] < m["added_at"]):
                    m["added_at"] = r["added_at"]
            c.execute("DELETE FROM watched_chats")
            for key in order:
                account_id, chat_id = key
                m = merged[key]
                c.execute(
                    "INSERT INTO watched_chats"
                    " (account_id, chat_id, title, username, kind, enabled, added_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (account_id, chat_id, m["title"], m["username"], m["kind"],
                     m["enabled"], m["added_at"] or iso(utcnow())),
                )

    # ---------- 写入 ----------
    def add_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        posted_at: str,
        raw_text: str,
        chat_title: str = "",
        sender_id: int | None = None,
        sender_name: str = "",
        link_id: str | None = None,
        url: str | None = None,
        pass_code: str | None = None,
        remark: str = "",
    ) -> dict:
        """原始消息 + 链接 + 备注一起落库。重复消息不会重复建链接。"""
        with self._write() as c:
            created_at = iso(utcnow())
            cur = c.execute(
                "INSERT OR IGNORE INTO messages"
                " (chat_id, chat_title, message_id, sender_id, sender_name, posted_at, raw_text, link_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    chat_id, chat_title, message_id, sender_id, sender_name,
                    posted_at, raw_text, link_id, created_at,
                ),
            )
            inserted = cur.rowcount > 0
            if not link_id or not url:
                return {"message_inserted": inserted, "share_id": None, "share_created": False}

            share_id, created = self._upsert_share(
                c, link_id, url, pass_code, created_at, chat_title
            )
            if remark:
                c.execute(
                    "INSERT OR IGNORE INTO share_remarks (share_id, remark, chat_title, posted_at)"
                    " VALUES (?,?,?,?)",
                    (share_id, remark, chat_title, posted_at),
                )
            return {"message_inserted": inserted, "share_id": share_id, "share_created": created}

    def add_share(
        self,
        *,
        link_id: str,
        url: str,
        pass_code: str = "",
        source: str = "",
    ) -> dict:
        """只登记一条分享链接，没有对应的 Telegram 消息。

        秒传任务手动加的、Bot 加的链接也进链接库：否则「我明明传过这个」
        在链接页里搜不到，也不会被定时校验覆盖到。
        判重靠 shares.link_id 的唯一约束（_upsert_share 里处理）——
        已经采到过的链接只更新 last_seen_at，source 保持首次来源不动。
        """
        link_id = (link_id or "").strip()
        if not link_id or not url:
            return {"share_id": None, "share_created": False}
        with self._write() as c:
            share_id, created = self._upsert_share(
                c, link_id, url, pass_code or "", iso(utcnow()), source or ""
            )
        return {"share_id": share_id, "share_created": created}

    def _upsert_share(
        self,
        c: sqlite3.Connection,
        link_id: str,
        url: str,
        pass_code: str | None,
        created_at: str,
        source: str = "",
    ) -> tuple[int, bool]:
        row = c.execute("SELECT id FROM shares WHERE link_id = ?", (link_id,)).fetchone()
        if row:
            # 已存在：只更新「最近一次见到」。source 保持首次来源不动 ——
            # 一个链接可能被转到好几个群，你要知道的是它最早出自哪。
            c.execute(
                "UPDATE shares SET last_seen_at = ?,"
                " pass_code = COALESCE(NULLIF(?, ''), pass_code),"
                " url = CASE WHEN ? <> '' THEN ? ELSE url END"
                " WHERE id = ?",
                (created_at, pass_code or "", url, url, row["id"]),
            )
            return int(row["id"]), False
        cur = c.execute(
            "INSERT INTO shares"
            " (link_id, url, pass_code, first_seen_at, last_seen_at, source)"
            " VALUES (?,?,?,?,?,?)",
            (link_id, url, pass_code, created_at, created_at, source),
        )
        return int(cur.lastrowid or 0), True

    def update_pass_code(self, link_id: str, code: str | None) -> bool:
        """补提取码：只在原来是空的时候写入，不覆盖已有值。"""
        if not code:
            return False
        with self._write() as c:
            cur = c.execute(
                "UPDATE shares SET pass_code = ? WHERE link_id = ?"
                " AND (pass_code IS NULL OR pass_code = '')",
                (code, link_id),
            )
            return cur.rowcount > 0

    def save_check(
        self,
        share_id: int,
        status: str,
        title: str = "",
        file_num: int | None = None,
        size_bytes: int | None = None,
        expire_seconds: int | None = None,
        error: str = "",
    ) -> None:
        with self._write() as c:
            c.execute(
                "UPDATE shares SET checked_at = ?, check_status = ?, check_title = ?,"
                " check_file_num = ?, check_size_bytes = ?, check_expire_seconds = ?,"
                " check_error = ? WHERE id = ?",
                (iso(utcnow()), status, title, file_num, size_bytes, expire_seconds,
                 error, share_id),
            )

    def set_state(self, key: str, value: str) -> None:
        # state 表存的是心跳、登录态、缓存快照这类东西，和 shares/messages 的
        # 聚合无关；心跳每 30 秒写一次，不该把统计缓存冲掉。
        with self._write(invalidate=()) as c:
            c.execute(
                "INSERT INTO state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._read() as c:
            row = c.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------- 查询 ----------
    def query(
        self,
        q: str = "",
        page: int = 1,
        size: int = 50,
        sort: str = "id",
        direction: str = "desc",
        only_valid: bool = False,
        with_code: bool | None = None,
    ) -> dict:
        tokens = [t for t in (q or "").lower().split() if t]
        # 前台永远不显示被隐藏的（管理页「前台显示」开关）
        where: list[str] = ["s.hidden = 0"]
        params: list[Any] = []
        for tok in tokens:
            like = f"%{_like_escape(tok)}%"
            where.append(
                "("
                "s.url LIKE ? ESCAPE '\\' OR s.link_id LIKE ? ESCAPE '\\'"
                " OR IFNULL(s.pass_code,'') LIKE ? ESCAPE '\\'"
                " OR IFNULL(s.check_title,'') LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM share_remarks r WHERE r.share_id = s.id"
                "            AND r.remark LIKE ? ESCAPE '\\')"
                ")"
            )
            params.extend([like] * 5)
        if only_valid:
            where.append("s.check_status IN ('OK','PASS_CODE_EMPTY')")
        if with_code is True:
            where.append("IFNULL(s.pass_code,'') <> ''")
        elif with_code is False:
            where.append("IFNULL(s.pass_code,'') = ''")

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        col = SORT_COLUMNS.get(sort, "s.id")
        order = "DESC" if str(direction).lower().startswith("desc") else "ASC"
        size = max(1, min(int(size or 50), 1000))
        page = max(1, int(page or 1))

        with self._read() as c:
            total = int(c.execute("SELECT COUNT(*) AS n FROM shares s" + clause, params).fetchone()["n"])
            rows = c.execute(
                "SELECT s.* FROM shares s" + clause
                + f" ORDER BY {col} {order}, s.id {order} LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            ).fetchall()
            items = [self._row_to_item(r) for r in rows]
            self._attach_remarks(c, items)
        return {"total": total, "page": page, "size": size, "items": items}

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict:
        return {
            "ID": row["id"],
            "LINK": row["url"],
            "CODE": row["pass_code"] or "无",
            "MSG": "",
            "TITLE": row["check_title"] or "",
            "STATUS": row["check_status"] or "",
            "FILE_NUM": row["check_file_num"],
            "SIZE_BYTES": row["check_size_bytes"],
            "EXPIRE_SECONDS": row["check_expire_seconds"],
            "CHECKED_AT": row["checked_at"] or "",
            "FIRST_SEEN": row["first_seen_at"],
            "LAST_SEEN": row["last_seen_at"],
            "CHAT": "",
            "SOURCE": row["source"] or "",
            "HIDDEN": bool(row["hidden"]),
        }

    @staticmethod
    def _attach_remarks(c: sqlite3.Connection, items: list[dict]) -> None:
        ids = [it["ID"] for it in items]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT share_id, remark, chat_title FROM share_remarks"
            f" WHERE share_id IN ({marks}) ORDER BY posted_at, rowid",
            ids,
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            grouped.setdefault(int(r["share_id"]), []).append(r)
        for it in items:
            rs = grouped.get(int(it["ID"]), [])
            it["MSG"] = " / ".join(dict.fromkeys(r["remark"] for r in rs))
            it["CHAT"] = " / ".join(dict.fromkeys(r["chat_title"] for r in rs if r["chat_title"]))

    def stats(self) -> dict:
        """全库计数。

        以前是 11 条独立的 COUNT(*)，每条都把 shares 扫一遍；这个函数被
        /api/stats、/api/items、/api/admin/links 同时调用，仪表盘每 5 秒就要
        跑两轮。现在合成一次扫描，外面再套一层短 TTL 缓存。
        """
        def load() -> dict:
            gone_marks = ",".join("?" * len(GONE_STATUSES))
            with self._read() as c:
                row = c.execute(
                    "SELECT COUNT(*) AS shares,"
                    " COALESCE(SUM(CASE WHEN check_status = 'OK' THEN 1 ELSE 0 END), 0) AS ok,"
                    " COALESCE(SUM(CASE WHEN check_status = 'PASS_CODE_EMPTY'"
                    "                   THEN 1 ELSE 0 END), 0) AS need_code,"
                    f" COALESCE(SUM(CASE WHEN check_status IN ({gone_marks})"
                    "                   THEN 1 ELSE 0 END), 0) AS gone,"
                    " COALESCE(SUM(CASE WHEN check_status = 'ERROR' THEN 1 ELSE 0 END), 0) AS error,"
                    " COALESCE(SUM(CASE WHEN IFNULL(check_status,'') = ''"
                    "                   THEN 1 ELSE 0 END), 0) AS unchecked,"
                    " COALESCE(SUM(CASE WHEN check_status = 'PASS_CODE_ERROR'"
                    "                   THEN 1 ELSE 0 END), 0) AS bad_code,"
                    " COALESCE(SUM(CASE WHEN IFNULL(pass_code,'') <> ''"
                    "                   THEN 1 ELSE 0 END), 0) AS with_code,"
                    " IFNULL(MAX(checked_at), '') AS last_checked_at"
                    "  FROM shares",
                    GONE_STATUSES,
                ).fetchone()
                msg = c.execute(
                    "SELECT COUNT(*) AS n, IFNULL(MAX(created_at), '') AS last_at FROM messages"
                ).fetchone()
            return {
                "shares": int(row["shares"]),
                "messages": int(msg["n"]),
                "ok": int(row["ok"]),
                "need_code": int(row["need_code"]),
                "gone": int(row["gone"]),
                "error": int(row["error"]),
                "unchecked": int(row["unchecked"]),
                "bad_code": int(row["bad_code"]),
                "with_code": int(row["with_code"]),
                "last_checked_at": row["last_checked_at"] or "",
                "last_message_at": msg["last_at"] or "",
            }

        return self._cached("stats", load)

    def chat_stats(self) -> dict[str, dict]:
        """每个群采集到多少：消息数 / 链接数 / 最后一次实际采集时间。

        键是字符串形式的 chat_id —— JSON 对象的键只能是字符串，前端直接用同一种形式。

        这是全库最贵的一条查询（messages 全表 + COUNT(DISTINCT)），而
        /api/listener 和 /api/monitor 每 5 秒各调一次，所以必须缓存。
        """
        def load() -> dict[str, dict]:
            with self._read() as c:
                rows = c.execute(
                    "SELECT chat_id,"
                    "       COUNT(*) AS messages,"
                    "       COUNT(DISTINCT CASE WHEN link_id IS NOT NULL AND link_id <> ''"
                    "                          THEN link_id END) AS links,"
                    "       MAX(created_at) AS last_at"
                    "  FROM messages GROUP BY chat_id"
                ).fetchall()
            return {
                str(int(r["chat_id"])): {
                    "messages": int(r["messages"]),
                    "links": int(r["links"]),
                    "last_at": r["last_at"] or "",
                }
                for r in rows
            }

        return self._cached("chat_stats", load)

    def query_by_chat(self, chat_id: int, page: int = 1, size: int = 50) -> dict:
        """某个群里采集到的链接，按它在该群最后一次出现的消息倒序。

        同一链接在同一群被转发多次时只保留最后一次（备注也取最后一次的）。
        这样列表长度 = 去重后的链接数，不会被刷屏的重复转发撑大。
        """
        size = max(1, min(int(size or 50), 500))
        page = max(1, int(page or 1))
        ids = chat_id_variants(int(chat_id))
        marks = ",".join("?" * len(ids))
        cond = f"m.chat_id IN ({marks}) AND m.link_id IS NOT NULL AND m.link_id <> ''"
        with self._read() as c:
            total = int(c.execute(
                f"SELECT COUNT(DISTINCT m.link_id) AS n FROM messages m WHERE {cond}",
                ids,
            ).fetchone()["n"])
            rows = c.execute(
                "SELECT s.*, m.posted_at AS msg_at, m.sender_name AS sender"
                "  FROM messages m JOIN shares s ON s.link_id = m.link_id"
                f" WHERE {cond}"
                "   AND m.id = (SELECT MAX(m2.id) FROM messages m2"
                "               WHERE m2.chat_id = m.chat_id AND m2.link_id = m.link_id)"
                " ORDER BY m.id DESC LIMIT ? OFFSET ?",
                ids + [size, (page - 1) * size],
            ).fetchall()
            items = [self._row_to_item(r) for r in rows]
            self._attach_remarks(c, items)
        for it, row in zip(items, rows):
            it["MSG_AT"] = row["msg_at"] or ""
            it["SENDER"] = row["sender"] or ""
        return {"total": total, "page": page, "size": size, "items": items}

    def shares_to_check(
        self,
        limit: int = 200,
        recheck_after_hours: int = 24,
        error_retry_minutes: int = 30,
    ) -> list[dict]:
        """待校验列表：没校验过的优先，其次是最久没校验的。

        recheck_after_hours <= 0 表示全部视为过期（「全部重校验」）。
        上次结果是 ERROR 的（多为被限流或网络抖动）按 error_retry_minutes 更早重试。
        """
        hours = max(0, recheck_after_hours)
        limit = max(1, int(limit))
        with self._read() as c:
            if hours == 0:
                rows = c.execute(
                    "SELECT id, link_id, url, pass_code FROM shares"
                    " ORDER BY (checked_at IS NOT NULL), checked_at ASC, id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                cutoff = iso(utcnow() - timedelta(hours=hours))
                err_cutoff = iso(utcnow() - timedelta(minutes=max(0, error_retry_minutes)))
                rows = c.execute(
                    "SELECT id, link_id, url, pass_code FROM shares"
                    " WHERE checked_at IS NULL"
                    "    OR checked_at < ?"
                    "    OR (check_status = 'ERROR' AND checked_at <= ?)"
                    " ORDER BY (checked_at IS NOT NULL), checked_at ASC, id ASC LIMIT ?",
                    (cutoff, err_cutoff, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def export_items(self, limit: int = 100000) -> list[dict]:
        """导出成与 shuju.json 相同的 {ID, LINK, CODE, MSG} 结构，供静态页使用。

        被隐藏的（hidden=1）不导出 —— 静态页没有接口，只能靠这里过滤。
        """
        with self._read() as c:
            rows = c.execute(
                "SELECT s.* FROM shares s WHERE s.hidden = 0 ORDER BY s.id ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            items = [self._row_to_item(r) for r in rows]
            self._attach_remarks(c, items)
        out = []
        for it in items:
            row = {"ID": it["ID"], "LINK": it["LINK"], "CODE": it["CODE"], "MSG": it["MSG"]}
            row.update({
                "STATUS": it["STATUS"], "TITLE": it["TITLE"],
                "SIZE_BYTES": it["SIZE_BYTES"], "CHAT": it["CHAT"], "SOURCE": it["SOURCE"],
            })
            out.append(row)
        return out

    # ---------- 分享链接管理（管理页） ----------
    def query_admin(
        self,
        q: str = "",
        page: int = 1,
        size: int = 50,
        sort: str = "id",
        direction: str = "desc",
        source: str = "",
        hidden: bool | None = None,
        status: str = "",
        with_code: bool | None = None,
    ) -> dict:
        """管理页的链接列表：包含已隐藏的，可按来源 / 显示状态 / 校验状态筛。"""
        tokens = [t for t in (q or "").lower().split() if t]
        where: list[str] = []
        params: list[Any] = []
        for tok in tokens:
            like = f"%{_like_escape(tok)}%"
            where.append(
                "("
                "s.url LIKE ? ESCAPE '\\' OR s.link_id LIKE ? ESCAPE '\\'"
                " OR IFNULL(s.pass_code,'') LIKE ? ESCAPE '\\'"
                " OR IFNULL(s.check_title,'') LIKE ? ESCAPE '\\'"
                " OR IFNULL(s.source,'') LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM share_remarks r WHERE r.share_id = s.id"
                "            AND r.remark LIKE ? ESCAPE '\\')"
                ")"
            )
            params.extend([like] * 6)
        if source:
            where.append("s.source = ?")
            params.append(source)
        if hidden is not None:
            where.append("s.hidden = ?")
            params.append(1 if hidden else 0)
        if status == "unchecked":
            where.append("IFNULL(s.check_status,'') = ''")
        elif status:
            where.append("s.check_status = ?")
            params.append(status)
        if with_code is True:
            where.append("IFNULL(s.pass_code,'') <> ''")
        elif with_code is False:
            where.append("IFNULL(s.pass_code,'') = ''")

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        col = SORT_COLUMNS.get(sort, "s.id")
        order = "DESC" if str(direction).lower().startswith("desc") else "ASC"
        size = max(1, min(int(size or 50), 500))
        page = max(1, int(page or 1))
        with self._read() as c:
            total = int(c.execute(
                "SELECT COUNT(*) AS n FROM shares s" + clause, params
            ).fetchone()["n"])
            rows = c.execute(
                "SELECT s.* FROM shares s" + clause
                + f" ORDER BY {col} {order}, s.id {order} LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            ).fetchall()
            items = [self._row_to_item(r) for r in rows]
            self._attach_remarks(c, items)
        return {"total": total, "page": page, "size": size, "items": items}

    def shares_by_ids(self, ids: list[int]) -> list[dict]:
        """按主键取链接（管理页「重新检测」用）。"""
        if not ids:
            return []
        ints = [int(i) for i in ids]
        marks = ",".join("?" * len(ints))
        with self._read() as c:
            rows = c.execute(
                f"SELECT id, link_id, url, pass_code FROM shares WHERE id IN ({marks})", ints
            ).fetchall()
        return [dict(r) for r in rows]

    def find_transfer_job_by_link_id(self, link_id: str) -> dict | None:
        """这条分享是不是已经建过秒传任务。

        share_url 存的是用户粘贴的原文（链接 / 带参数的链接 / 裸 ID 都可能），
        所以只能先 LIKE 粗筛。但只靠 `%id%` 会误判：id 是另一个 id 的子串时
        会把新分享当成「已提交过」挡掉。粗筛之后按 link_id 精确比对一次。
        """
        sid = str(link_id or "").strip()
        if not sid:
            return None
        from app import pikpak  # 延迟导入：store 是被 pikpak 链路间接依赖的底层模块

        like = f"%{_like_escape(sid)}%"
        with self._read() as c:
            rows = c.execute(
                "SELECT id, status, title, share_url FROM transfer_jobs"
                " WHERE share_url LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 50",
                (like,),
            ).fetchall()
        for row in rows:
            if pikpak.link_id_of(row["share_url"]) == sid:
                return dict(row)
        return None

    def check_stats(self) -> dict:
        """各校验状态有多少条，供管理页的状态筛选下拉用。"""
        def load() -> dict:
            with self._read() as c:
                rows = c.execute(
                    "SELECT IFNULL(check_status,'') AS st, COUNT(*) AS n FROM shares GROUP BY st"
                ).fetchall()
            return {(r["st"] or "unchecked"): int(r["n"]) for r in rows}

        return self._cached("check_stats", load)

    def daily_stats(self, days: int = 30) -> dict:
        """按天汇总，给仪表盘的走势图用。

        日期按**配置的时区**切（库里存的是 UTC，SQL 里用偏移量换算）。
        没有数据的那天补 0，否则折线图会跳着走，看着像没采集。
        """
        days = max(2, min(int(days or 30), 365))
        tz = self._tz
        offset = timeutil.sqlite_offset(tz)
        today = datetime.now(tz).date()
        start = today - timedelta(days=days - 1)
        # 起点那天的 00:00（本地）换算成 UTC，用来走 created_at 索引
        cutoff = iso(datetime.combine(start, dt_time.min, tzinfo=tz).astimezone(timezone.utc))

        buckets: dict[str, dict] = {}
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            buckets[d] = {"date": d, "new_links": 0, "messages": 0,
                          "checked": 0, "gone": 0, "cumulative": 0}

        def load() -> dict:
            # 每次调用都重新建一份，避免被调用两次时计数翻倍
            fresh = {k: dict(v) for k, v in buckets.items()}
            with self._read() as c:
                # 每天采集到多少消息 / 多少条去重链接
                for r in c.execute(
                    "SELECT substr(datetime(created_at, ?), 1, 10) AS d,"
                    "       COUNT(*) AS messages,"
                    "       COUNT(DISTINCT CASE WHEN IFNULL(link_id,'') <> ''"
                    "                          THEN link_id END) AS links"
                    "  FROM messages WHERE created_at >= ?"
                    " GROUP BY d",
                    (offset, cutoff),
                ):
                    b = fresh.get(r["d"])
                    if b:
                        b["messages"] = int(r["messages"])
                        b["new_links"] = int(r["links"])

                # 每天首次实际入库的链接数（同一个链接只算第一次采集）
                for r in c.execute(
                    "SELECT substr(datetime(first_seen_at, ?), 1, 10) AS d,"
                    "       COUNT(*) AS n FROM shares"
                    " WHERE first_seen_at >= ? GROUP BY d",
                    (offset, cutoff),
                ):
                    b = fresh.get(r["d"])
                    if b:
                        b["new_links"] = int(r["n"])

                # 每天校验了多少条、其中判失效多少条
                marks = ",".join("?" * len(GONE_STATUSES))
                for r in c.execute(
                    "SELECT substr(datetime(checked_at, ?), 1, 10) AS d,"
                    "       COUNT(*) AS checked,"
                    f"      SUM(CASE WHEN check_status IN ({marks}) THEN 1 ELSE 0 END) AS gone"
                    "  FROM shares WHERE checked_at IS NOT NULL AND checked_at >= ?"
                    " GROUP BY d",
                    (offset, *GONE_STATUSES, cutoff),
                ):
                    b = fresh.get(r["d"])
                    if b:
                        b["checked"] = int(r["checked"])
                        b["gone"] = int(r["gone"] or 0)

                # 起点之前的累计数，好让累计线从正确的高度接上
                base = int(c.execute(
                    "SELECT COUNT(*) AS n FROM shares WHERE first_seen_at < ?",
                    (cutoff,),
                ).fetchone()["n"])

            series = [fresh[(start + timedelta(days=i)).isoformat()] for i in range(days)]
            acc = base
            for row in series:
                acc += row["new_links"]
                row["cumulative"] = acc
            return {
                "days": days,
                "from": start.isoformat(),
                "to": today.isoformat(),
                "timezone": timeutil.label(tz),
                "base_total": base,
                "total": acc,
                "series": series,
            }

        return load()

    def activity_stats(self, minutes: int = 60) -> dict:
        """最近 N 分钟按分钟汇总的采集活动，给仪表盘的实时监控条用。

        分桶按配置的时区切（库里是 UTC，SQL 里换算），但每条桶的 `at` 给的是
        UTC 时刻 —— 前端用 `new Date(at)` 按浏览器时区显示，服务端和浏览器
        时区一致时标签就对得上。
        """
        minutes = max(10, min(int(minutes or 60), 240))
        tz = self._tz
        offset = timeutil.sqlite_offset(tz)
        now_local = datetime.now(tz).replace(second=0, microsecond=0)
        start_local = now_local - timedelta(minutes=minutes - 1)
        cutoff = iso(start_local.astimezone(timezone.utc))

        def load() -> dict:
            buckets: dict[str, dict] = {}
            series: list[dict] = []
            for i in range(minutes):
                local_at = start_local + timedelta(minutes=i)
                bucket = {
                    "at": iso(local_at.astimezone(timezone.utc)),
                    "label": local_at.strftime("%H:%M"),
                    "messages": 0,
                    "links": 0,
                }
                buckets[local_at.strftime("%Y-%m-%dT%H:%M")] = bucket
                series.append(bucket)

            with self._read() as c:
                rows = c.execute(
                    "SELECT strftime('%Y-%m-%dT%H:%M', datetime(created_at, ?)) AS hm,"
                    "       COUNT(*) AS messages,"
                    "       COUNT(DISTINCT CASE WHEN IFNULL(link_id,'') <> ''"
                    "                          THEN link_id END) AS links"
                    "  FROM messages WHERE created_at >= ?"
                    " GROUP BY hm",
                    (offset, cutoff),
                ).fetchall()
            for row in rows:
                hit = buckets.get(row["hm"])
                if hit is not None:
                    hit["messages"] = int(row["messages"])
                    hit["links"] = int(row["links"])
            return {
                "minutes": minutes,
                "timezone": timeutil.label(tz),
                "series": series,
                "messages": sum(b["messages"] for b in series),
                "links": sum(b["links"] for b in series),
            }

        return self._cached("activity", load)

    def source_stats(self) -> list[dict]:
        """按来源汇总，供管理页做筛选下拉。"""
        def load() -> list[dict]:
            with self._read() as c:
                rows = c.execute(
                    "SELECT CASE WHEN IFNULL(source,'') = '' THEN '(未知来源)' ELSE source END AS src,"
                    "       COUNT(*) AS total,"
                    "       SUM(CASE WHEN hidden = 1 THEN 1 ELSE 0 END) AS hidden,"
                    "       MAX(last_seen_at) AS last_at"
                    "  FROM shares GROUP BY src ORDER BY total DESC, src ASC"
                ).fetchall()
            return [
                {
                    "source": r["src"],
                    "total": int(r["total"]),
                    "hidden": int(r["hidden"] or 0),
                    "last_at": r["last_at"] or "",
                }
                for r in rows
            ]

        return self._cached("source_stats", load)

    def set_hidden(self, ids: list[int], hidden: bool = True) -> int:
        """前台显示开关。ids 为空时对全部生效（配合「隐藏所有筛选结果」）。"""
        with self._write() as c:
            if not ids:
                cur = c.execute("UPDATE shares SET hidden = ?", (1 if hidden else 0,))
            else:
                marks = ",".join("?" * len(ids))
                cur = c.execute(
                    f"UPDATE shares SET hidden = ? WHERE id IN ({marks})",
                    [1 if hidden else 0, *[int(i) for i in ids]],
                )
            return cur.rowcount

    def delete_shares(self, ids: list[int]) -> int:
        """彻底删除分享链接（备注靠外键级联一起删）。

        原始消息（messages）保留 —— 那是留档，删掉就没法追溯「这条链接是从哪来的」。
        消息上的 link_id 一并清空，避免它继续以「已入库」的身份出现在群链接列表里。
        """
        if not ids:
            return 0
        ints = [int(i) for i in ids]
        marks = ",".join("?" * len(ints))
        with self._write() as c:
            links = [
                r["link_id"] for r in c.execute(
                    f"SELECT link_id FROM shares WHERE id IN ({marks})", ints
                ).fetchall()
            ]
            cur = c.execute(f"DELETE FROM shares WHERE id IN ({marks})", ints)
            if links:
                lm = ",".join("?" * len(links))
                c.execute(
                    f"UPDATE messages SET link_id = NULL WHERE link_id IN ({lm})", links
                )
            return cur.rowcount

    def set_source(self, ids: list[int], source: str) -> int:
        """改来源。老数据（迁移前入库的）来源是空的，可以手动补。"""
        if not ids:
            return 0
        ints = [int(i) for i in ids]
        marks = ",".join("?" * len(ints))
        with self._write() as c:
            cur = c.execute(
                f"UPDATE shares SET source = ? WHERE id IN ({marks})",
                [source or "", *ints],
            )
            return cur.rowcount

    # ---------- PikPak 秒传到光鸭的任务 ----------
    @staticmethod
    def _transfer_job_row(row: sqlite3.Row) -> dict:
        out = dict(row)
        total = int(out.get("total_files") or 0)
        done = sum(int(out.get(k) or 0) for k in (
            "completed_files", "failed_files", "skipped_files",
        ))
        out["progress"] = round(done / total * 100, 1) if total else 0
        return out

    def create_transfer_job(
        self,
        *,
        share_url: str,
        pass_code: str,
        target_folder_id: str,
        target_folder_path: str,
        source: str = "web",
    ) -> dict:
        now = iso(utcnow())
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO transfer_jobs"
                " (share_url, pass_code, target_folder_id, target_folder_path,"
                "  source, status, created_at)"
                " VALUES (?,?,?,?,?, 'queued', ?)",
                (
                    share_url, pass_code, target_folder_id, target_folder_path,
                    source or "web", now,
                ),
            )
            job_id = int(cur.lastrowid or 0)
            row = c.execute(
                "SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._transfer_job_row(row) if row else {}

    @staticmethod
    def _insert_transfer_items(c: sqlite3.Connection, job_id: int,
                               items: list[dict], now: str) -> None:
        """在调用方已有的事务里插入明细，并刷新 total_files。"""
        if not items:
            return
        rows = [
            (
                int(job_id), item.get("path") or "", item.get("name") or "",
                int(item.get("size") or 0), item.get("gcid") or "",
                item.get("cid") or "", item.get("file_id") or "",
                "pending", "", now, now,
            )
            for item in items
        ]
        c.executemany(
            "INSERT INTO transfer_items"
            " (job_id, path, name, size_bytes, gcid, cid, file_id, status,"
            "  error, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            # 重跑同一任务时只补 file_id/gcid，状态和已算好的 cid 不动
            " ON CONFLICT(job_id, path) DO UPDATE SET"
            "  name = excluded.name,"
            "  size_bytes = excluded.size_bytes,"
            "  gcid = CASE WHEN transfer_items.gcid = ''"
            "              THEN excluded.gcid ELSE transfer_items.gcid END,"
            "  file_id = CASE WHEN transfer_items.file_id = ''"
            "                THEN excluded.file_id ELSE transfer_items.file_id END",
            rows,
        )
        c.execute(
            "UPDATE transfer_jobs SET"
            " total_files = ("
            "  SELECT COUNT(*) FROM transfer_items WHERE job_id = ?"
            " ),"
            " total_bytes = ("
            "  SELECT COALESCE(SUM(size_bytes), 0) FROM transfer_items"
            "  WHERE job_id = ?"
            " ) WHERE id = ?",
            (int(job_id), int(job_id), int(job_id)),
        )

    def add_transfer_items(self, job_id: int, items: list[dict]) -> int:
        if not items:
            return 0
        now = iso(utcnow())
        with self._write() as c:
            self._insert_transfer_items(c, job_id, items, now)
        return len(items)

    def split_transfer_job(self, parent_job: dict, groups: list[dict],
                           pass_code_token: str) -> list[dict]:
        """把大分享拆成多个任务；第 1 组写回原任务，其余新建排队任务。"""
        if len(groups) <= 1:
            return []
        now = iso(utcnow())
        parent_id = int(parent_job["id"])
        share_url = str(parent_job.get("share_url") or "")
        pass_code = str(parent_job.get("pass_code") or "")
        target_folder_id = str(parent_job.get("target_folder_id") or "")
        target_folder_path = str(parent_job.get("target_folder_path") or "/")
        source = str(parent_job.get("source") or "web")
        total_groups = len(groups)
        job_ids: list[int] = []
        with self._write() as c:
            current = c.execute(
                "SELECT status FROM transfer_jobs WHERE id = ?", (parent_id,)
            ).fetchone()
            if not current or current["status"] not in ("queued", "running"):
                return []
            for index, group in enumerate(groups):
                items = list(group.get("items") or [])
                title = str(group.get("title") or "")
                total_bytes = sum(int(item.get("size") or 0) for item in items)
                if index == 0:
                    job_id = parent_id
                    c.execute(
                        "UPDATE transfer_jobs SET title = ?, total_files = ?,"
                        " total_bytes = ?, pass_code_token = ?, message = ?"
                        " WHERE id = ?",
                        (
                            title, len(items), total_bytes, pass_code_token,
                            f"已自动拆分，当前为第 1/{total_groups} 组",
                            job_id,
                        ),
                    )
                else:
                    child_title = (
                        f"原 #{parent_id} · {title}"
                        if title else f"原 #{parent_id}"
                    )
                    cur = c.execute(
                        "INSERT INTO transfer_jobs"
                        " (share_url, pass_code, target_folder_id,"
                        "  target_folder_path, source, status, created_at,"
                        "  title, message, total_files, total_bytes,"
                        "  pass_code_token)"
                        " VALUES (?,?,?,?,?, 'queued', ?, ?, ?, ?, ?, ?)",
                        (
                            share_url, pass_code, target_folder_id,
                            target_folder_path, source, now, child_title,
                            (
                                f"由任务 #{parent_id} 自动拆分，"
                                f"当前为第 {index + 1}/{total_groups} 组"
                            ),
                            len(items), total_bytes, pass_code_token,
                        ),
                    )
                    job_id = int(cur.lastrowid or 0)
                self._insert_transfer_items(c, job_id, items, now)
                job_ids.append(job_id)
        return [self.get_transfer_job(job_id) or {} for job_id in job_ids]

    def update_transfer_job(self, job_id: int, **fields) -> None:
        allowed = {
            "status", "total_files", "completed_files", "failed_files",
            "skipped_files", "current_file", "message", "started_at", "finished_at",
            "target_folder_id", "target_folder_path", "pass_code_token",
            "title", "total_bytes", "completed_bytes",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._write(invalidate=()) as c:
            c.execute(
                f"UPDATE transfer_jobs SET {assignments} WHERE id = ?",
                [*values.values(), int(job_id)],
            )

    def update_transfer_item(
        self,
        item_id: int,
        *,
        status: str,
        error: str = "",
        cid: str | None = None,
    ) -> None:
        assignments = ["status = ?", "error = ?", "updated_at = ?"]
        values: list = [status, error, iso(utcnow())]
        if cid is not None:
            assignments.append("cid = ?")
            values.append(cid)
        values.append(int(item_id))
        with self._write(invalidate=()) as c:
            c.execute(
                f"UPDATE transfer_items SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def update_transfer_item_and_progress(
        self,
        item_id: int,
        job_id: int,
        *,
        status: str,
        completed_files: int,
        completed_bytes: int,
        failed_files: int,
        skipped_files: int,
        current_file: str,
        message: str,
        error: str = "",
        cid: str = "",
    ) -> None:
        """秒传热路径：明细状态和任务进度放在一个事务里，少一次连接和提交。

        每个文件都会调一次，所以显式声明「不影响聚合缓存」—— 否则一次两万文件的
        任务会把统计缓存冲掉两万次。
        """
        with self._write(invalidate=()) as c:
            c.execute(
                "UPDATE transfer_items SET status = ?, error = ?,"
                " cid = CASE WHEN ? <> '' THEN ? ELSE cid END,"
                " updated_at = ? WHERE id = ?",
                (status, error, cid, cid, iso(utcnow()), int(item_id)),
            )
            c.execute(
                "UPDATE transfer_jobs SET completed_files = ?, completed_bytes = ?,"
                " failed_files = ?, skipped_files = ?, current_file = ?,"
                " message = ? WHERE id = ?",
                (
                    int(completed_files), int(completed_bytes),
                    int(failed_files), int(skipped_files),
                    str(current_file or ""), str(message or ""), int(job_id),
                ),
            )

    def set_transfer_item_cid(self, item_id: int, cid: str) -> None:
        with self._write(invalidate=()) as c:
            c.execute(
                "UPDATE transfer_items SET cid = ?, updated_at = ? WHERE id = ?",
                (cid, iso(utcnow()), int(item_id)),
            )

    def get_transfer_job(self, job_id: int) -> dict | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM transfer_jobs WHERE id = ?", (int(job_id),)
            ).fetchone()
        return self._transfer_job_row(row) if row else None

    def list_transfer_jobs(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM transfer_jobs"
                " ORDER BY CASE status"
                "   WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
                "   id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._transfer_job_row(row) for row in rows]

    def list_active_transfer_jobs(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit or 200), 200))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM transfer_jobs"
                " WHERE status IN ('running', 'queued', 'paused')"
                " ORDER BY CASE status"
                "   WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
                "   id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._transfer_job_row(row) for row in rows]

    def list_finished_transfer_jobs(self, page: int = 1, size: int = 20) -> dict:
        page = max(1, int(page or 1))
        size = max(1, min(int(size or 20), 100))
        with self._read() as c:
            total = int(c.execute(
                "SELECT COUNT(*) AS n FROM transfer_jobs"
                " WHERE status NOT IN ('running', 'queued', 'paused')"
            ).fetchone()["n"])
            rows = c.execute(
                "SELECT * FROM transfer_jobs"
                " WHERE status NOT IN ('running', 'queued', 'paused')"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            ).fetchall()
        return {
            "total": total,
            "page": page,
            "size": size,
            "pages": max(1, (total + size - 1) // size),
            "items": [self._transfer_job_row(row) for row in rows],
        }

    def transfer_job_counts(self) -> dict[str, int]:
        with self._read() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM transfer_jobs GROUP BY status"
            ).fetchall()
        return {str(row["status"] or ""): int(row["n"]) for row in rows}

    def list_transfer_items(self, job_id: int,
                            limit: int | None = 1000) -> list[dict]:
        sql = "SELECT * FROM transfer_items WHERE job_id = ? ORDER BY id"
        params: list = [int(job_id)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, min(int(limit or 1000), 5000)))
        with self._read() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_pending_transfer_items(self, job_id: int, after_id: int = 0,
                                    limit: int = 500) -> list[dict]:
        """按 id 分批取待处理明细，避免大任务一次性把全部明细和 Future 塞进内存。

        只取 selected=1 的：被排除的文件留着 pending 状态不动，方便随时改主意。
        """
        limit = max(1, min(int(limit or 500), 2000))
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM transfer_items"
                " WHERE job_id = ? AND status = 'pending' AND selected = 1 AND id > ?"
                " ORDER BY id LIMIT ?",
                (int(job_id), int(after_id), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def select_transfer_items(
        self,
        job_id: int,
        *,
        selected: bool,
        ids: list[int] | None = None,
        max_size: int | None = None,
        min_size: int | None = None,
    ) -> int:
        """勾选 / 取消勾选要秒传的文件，返回改动的行数。

        两种用法：
        - 传 ids：只改这几条（页面上逐条勾选；详情只显示前 N 条时也安全）
        - 不传：服务端批量改全部**待处理**的条目，避免把两万条 ID 传到浏览器再传回来

        只动 status='pending' 的：已经传完 / 失败 / 跳过的条目，勾不勾都没有意义。
        max_size / min_size 用来做「只传小于 10MB 的」这类筛选，单位字节。
        """
        conditions = ["job_id = ?", "status = 'pending'"]
        params: list[Any] = [int(job_id)]

        if ids is not None:
            ints = [int(i) for i in ids]
            if not ints:
                return 0
            conditions.append(f"id IN ({','.join('?' * len(ints))})")
            params.extend(ints)

        if max_size is not None:
            conditions.append("size_bytes <= ?")
            params.append(max(0, int(max_size)))
        if min_size is not None:
            conditions.append("size_bytes >= ?")
            params.append(max(0, int(min_size)))

        with self._write(invalidate=()) as c:
            cur = c.execute(
                f"UPDATE transfer_items SET selected = ?, updated_at = ?"
                f" WHERE {' AND '.join(conditions)}",
                [1 if selected else 0, iso(utcnow()), *params],
            )
            return cur.rowcount

    def skip_unselected_items(self, job_id: int) -> int:
        """任务收尾时，把没勾选的待处理条目标成「跳过」。

        不标的话它们会一直挂在 pending：任务已经结束，但计数对不上
        （比如显示 80/100，剩 20 条永远不动），看着像卡住了。
        """
        with self._write(invalidate=()) as c:
            cur = c.execute(
                "UPDATE transfer_items SET status = 'skipped', error = '未勾选，跳过',"
                " updated_at = ? WHERE job_id = ? AND status = 'pending' AND selected = 0",
                (iso(utcnow()), int(job_id)),
            )
            return cur.rowcount

    def transfer_item_selection(self, job_id: int) -> dict:
        """勾选概况，给页面显示「已选 N 项 / X GB」。"""
        with self._read() as c:
            row = c.execute(
                "SELECT COUNT(*) AS total,"
                "       SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected,"
                "       COALESCE(SUM(CASE WHEN selected = 1 THEN size_bytes ELSE 0 END), 0)"
                "         AS selected_bytes,"
                "       COALESCE(SUM(size_bytes), 0) AS total_bytes"
                "  FROM transfer_items WHERE job_id = ?",
                (int(job_id),),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "selected": int(row["selected"] or 0),
            "selected_bytes": int(row["selected_bytes"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
        }

    def transfer_item_counts(self, job_id: int) -> dict[str, int]:
        """按状态统计文件数，重试后要用它刷新 job 上的计数。"""
        with self._read() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM transfer_items"
                " WHERE job_id = ? GROUP BY status",
                (int(job_id),),
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def transfer_item_bytes(self, job_id: int) -> dict[str, int]:
        """总大小 / 已秒传成功的字节数。"""
        with self._read() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total,"
                " COALESCE(SUM(CASE WHEN status = 'completed'"
                "                   THEN size_bytes ELSE 0 END), 0) AS completed"
                " FROM transfer_items WHERE job_id = ?",
                (int(job_id),),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "completed": int(row["completed"] or 0),
        }

    def delete_transfer_job(self, job_id: int) -> bool:
        """删任务记录（文件明细跟着级联删）。"""
        with self._write() as c:
            cur = c.execute("DELETE FROM transfer_jobs WHERE id = ?", (int(job_id),))
            return cur.rowcount > 0

    def reset_transfer_items(self, job_id: int,
                             statuses: tuple[str, ...] = ("failed",)) -> int:
        """重试前把失败项改回待处理，返回改了几条。"""
        if not statuses:
            return 0
        marks = ",".join("?" * len(statuses))
        with self._write() as c:
            cur = c.execute(
                "UPDATE transfer_items SET status = 'pending', error = '',"
                " updated_at = ? WHERE job_id = ?"
                f" AND status IN ({marks})",
                [iso(utcnow()), int(job_id), *statuses],
            )
            return cur.rowcount

    def dedupe_transfer_items(self, job_id: int) -> int:
        """清理历史重试造成的重复明细，优先保留已完成记录。"""
        with self._write() as c:
            rows = c.execute(
                "SELECT id, file_id, path, status FROM transfer_items"
                " WHERE job_id = ?"
                " ORDER BY CASE status"
                "   WHEN 'completed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
                "   id",
                (int(job_id),),
            ).fetchall()
            seen: set[str] = set()
            duplicate_ids: list[int] = []
            for row in rows:
                key = str(row["file_id"] or "").strip() or str(row["path"] or "")
                if key in seen:
                    duplicate_ids.append(int(row["id"]))
                else:
                    seen.add(key)
            if not duplicate_ids:
                return 0
            marks = ",".join("?" * len(duplicate_ids))
            c.execute(
                f"DELETE FROM transfer_items WHERE id IN ({marks})",
                duplicate_ids,
            )
            c.execute(
                "UPDATE transfer_jobs SET total_files = ("
                " SELECT COUNT(*) FROM transfer_items WHERE job_id = ?"
                ") WHERE id = ?",
                (int(job_id), int(job_id)),
            )
            return len(duplicate_ids)

    def next_queued_transfer_job(self) -> dict | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM transfer_jobs WHERE status = 'queued'"
                " ORDER BY id LIMIT 1"
            ).fetchone()
        return self._transfer_job_row(row) if row else None

    def next_unparsed_queued_transfer_job(self) -> dict | None:
        """下一单还没明细的排队任务；已经预解析好的任务会被跳过。"""
        with self._read() as c:
            row = c.execute(
                "SELECT j.* FROM transfer_jobs j"
                " WHERE j.status = 'queued'"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM transfer_items i WHERE i.job_id = j.id"
                " )"
                " ORDER BY j.id LIMIT 1"
            ).fetchone()
        return self._transfer_job_row(row) if row else None

    def requeue_interrupted_transfer_jobs(self) -> int:
        with self._write() as c:
            cur = c.execute(
                "UPDATE transfer_jobs SET status = 'queued', current_file = '',"
                " message = '服务重启，任务已重新排队'"
                " WHERE status = 'running'"
            )
            return cur.rowcount

    # ---------- JSON 设置（代理配置等） ----------
    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False))

    # ---------- 监听群组（按账号分开） ----------
    def _watch_owner(self, account_id: int | None) -> int:
        """这次操作算哪个账号的。不传就跟随「当前使用」那个。"""
        if account_id:
            return int(account_id)
        acc = self.active_account()
        return int(acc["id"]) if acc else 0

    def list_watched(self, only_enabled: bool = False, account_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM watched_chats WHERE account_id = ?"
        if only_enabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY added_at, chat_id"
        with self._read() as c:
            rows = c.execute(sql, (self._watch_owner(account_id),)).fetchall()
        return [self._watched_row(r) for r in rows]

    def list_watched_all(self, only_enabled: bool = False) -> list[dict]:
        """全部账号的勾选（不分账号），监控室用。默认只给**正在监控**的那些。"""
        sql = "SELECT * FROM watched_chats"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY account_id, added_at, chat_id"
        with self._read() as c:
            rows = c.execute(sql).fetchall()
        return [self._watched_row(r) for r in rows]

    def watched_ids(self, account_id: int | None = None) -> list[int]:
        with self._read() as c:
            rows = c.execute(
                "SELECT chat_id FROM watched_chats WHERE account_id = ? AND enabled = 1",
                (self._watch_owner(account_id),),
            ).fetchall()
        return [int(r["chat_id"]) for r in rows]

    @staticmethod
    def _watched_row(row: sqlite3.Row) -> dict:
        return {
            "account_id": int(row["account_id"]),
            "chat_id": int(row["chat_id"]),
            "title": row["title"] or "",
            "username": row["username"] or "",
            "kind": row["kind"] or "",
            "enabled": bool(row["enabled"]),
            "media_enabled": bool(row["media_enabled"]) if "media_enabled" in row.keys() else False,
            "added_at": row["added_at"] or "",
        }

    @staticmethod
    def _upsert_watched(c, account_id, chat_id, title, username, kind, enabled) -> None:
        c.execute(
            "INSERT INTO watched_chats"
            " (account_id, chat_id, title, username, kind, enabled, added_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(account_id, chat_id) DO UPDATE SET"
            "   title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,"
            "   username = CASE WHEN excluded.username <> '' THEN excluded.username ELSE username END,"
            "   kind = CASE WHEN excluded.kind <> '' THEN excluded.kind ELSE kind END,"
            "   enabled = excluded.enabled",
            (int(account_id), canonical_chat_id(chat_id), title or "", username or "",
             kind or "", 1 if enabled else 0, iso(utcnow())),
        )

    def add_watched(self, chat_id: int, title: str = "", username: str = "",
                    kind: str = "", enabled: bool = True,
                    account_id: int | None = None) -> dict:
        owner = self._watch_owner(account_id)
        with self._write() as c:
            self._upsert_watched(c, owner, chat_id, title, username, kind, enabled)
        return {"account_id": owner, "chat_id": canonical_chat_id(chat_id), "enabled": bool(enabled)}

    def set_watched(self, chat_id: int, enabled: bool,
                    account_id: int | None = None) -> bool:
        """打开 / 关闭某个群的采集开关。

        和 add_watched 的区别：这个只改**已有**记录，不存在就返回 False。
        群列表里没勾过的群调它不该凭空出现在监控列表里。

        返回值是「有没有改到东西」，调用方（网页的勾选接口、Bot 的群组开关）
        用它区分「已关闭」和「这个群本来就没在监控」。
        """
        owner = self._watch_owner(account_id)
        with self._write() as c:
            cur = c.execute(
                "UPDATE watched_chats SET enabled = ?"
                " WHERE account_id = ? AND chat_id = ?",
                (1 if enabled else 0, owner, canonical_chat_id(chat_id)),
            )
            return cur.rowcount > 0

    def set_media_watched(self, chat_id: int, enabled: bool, title: str = "",
                          username: str = "", kind: str = "",
                          account_id: int | None = None) -> bool:
        """单独控制某个群是否采集图片 / 视频。"""
        owner = self._watch_owner(account_id)
        cid = canonical_chat_id(chat_id)
        with self._write() as c:
            c.execute(
                "INSERT INTO watched_chats"
                " (account_id, chat_id, title, username, kind, enabled, media_enabled, added_at)"
                " VALUES (?,?,?,?,?,0,?,?)"
                " ON CONFLICT(account_id, chat_id) DO UPDATE SET"
                " title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,"
                " username = CASE WHEN excluded.username <> '' THEN excluded.username ELSE username END,"
                " kind = CASE WHEN excluded.kind <> '' THEN excluded.kind ELSE kind END,"
                " media_enabled = excluded.media_enabled",
                (
                    owner, cid, title or "", username or "", kind or "",
                    1 if enabled else 0, iso(utcnow()),
                ),
            )
            return True

    def media_enabled(self, chat_id: int, account_id: int | None = None) -> bool:
        owner = self._watch_owner(account_id)
        with self._read() as c:
            row = c.execute(
                "SELECT media_enabled FROM watched_chats"
                " WHERE account_id = ? AND chat_id = ?",
                (owner, canonical_chat_id(chat_id)),
            ).fetchone()
        return bool(row and row["media_enabled"])

    def assign_orphan_watched(self) -> int:
        """把老库里 account_id=0 的监听记录归到当前账号。"""
        with self._write() as c:
            row = c.execute(
                "SELECT id FROM accounts ORDER BY active DESC, id LIMIT 1"
            ).fetchone()
            if not row:
                return 0
            account_id = int(row["id"])
            cur = c.execute(
                "UPDATE OR IGNORE watched_chats SET account_id = ? WHERE account_id = 0",
                (account_id,),
            )
            moved = cur.rowcount
            c.execute("DELETE FROM watched_chats WHERE account_id = 0")
            return moved

    # ---------- Telegram 媒体（缩略图 + 元数据） ----------
    def upsert_media_item(self, item: dict) -> int:
        fields = (
            "account_id", "chat_id", "chat_title", "chat_username", "message_id",
            "grouped_id", "item_index", "media_type", "document_id", "access_hash",
            "file_name", "mime_type", "size_bytes", "width", "height",
            "duration_sec", "posted_at", "created_at",
        )
        values = [item.get(name) for name in fields]
        with self._write(invalidate=()) as c:
            c.execute(
                f"INSERT INTO media_items ({','.join(fields)})"
                f" VALUES ({','.join('?' * len(fields))})"
                " ON CONFLICT(account_id, chat_id, message_id, item_index) DO UPDATE SET"
                " chat_title=excluded.chat_title, chat_username=excluded.chat_username,"
                " grouped_id=excluded.grouped_id, media_type=excluded.media_type,"
                " document_id=excluded.document_id, access_hash=excluded.access_hash,"
                " file_name=excluded.file_name, mime_type=excluded.mime_type,"
                " size_bytes=excluded.size_bytes, width=excluded.width,"
                " height=excluded.height, duration_sec=excluded.duration_sec,"
                " posted_at=excluded.posted_at",
                values,
            )
            row = c.execute(
                "SELECT id FROM media_items"
                " WHERE account_id=? AND chat_id=? AND message_id=? AND item_index=?",
                (
                    item.get("account_id"), item.get("chat_id"), item.get("message_id"),
                    item.get("item_index", 0),
                ),
            ).fetchone()
            return int(row["id"])

    def set_media_thumb(self, media_id: int, path: str = "",
                        status: str = "ok", error: str = "") -> None:
        with self._write(invalidate=()) as c:
            c.execute(
                "UPDATE media_items SET thumb_path=?, thumb_status=?, thumb_error=?"
                " WHERE id=?",
                (path or "", status, error, int(media_id)),
            )

    def list_media(self, page: int = 1, size: int = 60, media_type: str = "",
                   chat_id: int | None = None, account_id: int | None = None) -> dict:
        where, params = [], []
        if media_type:
            where.append("media_type = ?")
            params.append(media_type)
        if chat_id:
            where.append("chat_id = ?")
            params.append(canonical_chat_id(chat_id))
        if account_id:
            where.append("account_id = ?")
            params.append(int(account_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        size = max(1, min(int(size or 60), 200))
        page = max(1, int(page or 1))
        with self._read() as c:
            total = int(c.execute(
                "SELECT COUNT(*) AS n FROM media_items" + clause, params
            ).fetchone()["n"])
            rows = c.execute(
                "SELECT * FROM media_items" + clause
                + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [size, (page - 1) * size],
            ).fetchall()
        return {
            "total": total, "page": page, "size": size,
            "items": [dict(row) for row in rows],
        }

    def get_media(self, media_id: int) -> dict | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM media_items WHERE id=?", (int(media_id),)
            ).fetchone()
        return dict(row) if row else None

    # ---------- Telegram 账号（一个账号一个 session 文件） ----------
    @staticmethod
    def _account_row(row: sqlite3.Row) -> dict:
        alias = row["alias"] if "alias" in row.keys() else ""
        name = " ".join(
            x for x in [row["first_name"] or "", row["last_name"] or ""] if x
        )
        return {
            "id": int(row["id"]),
            "phone": row["phone"] or "",
            "alias": alias or "",
            "user_id": row["user_id"],
            "first_name": row["first_name"] or "",
            "last_name": row["last_name"] or "",
            "name": alias or name,
            "username": row["username"] or "",
            "session": row["session"] or "",
            "proxy_id": int(row["proxy_id"]) if "proxy_id" in row.keys() else 0,
            "collect_enabled": (
                int(row["collect_enabled"]) if "collect_enabled" in row.keys() else 0
            ),
            "created_at": row["created_at"] or "",
            "last_login_at": row["last_login_at"] or "",
        }

    def list_accounts(self) -> list[dict]:
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM accounts ORDER BY active DESC, id"
            ).fetchall()
        return [self._account_row(r) for r in rows]

    def get_account(self, account_id: int) -> dict | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM accounts WHERE id = ?", (int(account_id),)
            ).fetchone()
        return self._account_row(row) if row else None

    def active_account(self) -> dict | None:
        """当前使用的账号。没有 active 就退回第一个（老库、人为改过库都兜住）。"""
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM accounts ORDER BY active DESC, id LIMIT 1"
            ).fetchone()
        return self._account_row(row) if row else None

    def add_account(self, phone: str = "", session: str = "",
                    proxy_id: int = 0) -> dict:
        """新建账号。第一个账号自动设为「当前使用」。"""
        with self._write() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
            cur = c.execute(
                "INSERT INTO accounts (phone, session, proxy_id, active, created_at)"
                " VALUES (?,?,?,?,?)",
                (phone or "", session or "", int(proxy_id or 0),
                 1 if not n else 0, iso(utcnow())),
            )
            return {
                "id": int(cur.lastrowid), "active": not n,
                "session": session or "", "proxy_id": int(proxy_id or 0),
            }

    def update_account(self, account_id: int, **fields) -> bool:
        allowed = (
            "phone", "alias", "user_id", "first_name", "last_name",
            "username", "session", "last_login_at", "proxy_id", "collect_enabled",
        )
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return False
        vals.append(int(account_id))
        with self._write() as c:
            cur = c.execute(
                f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
            )
            return cur.rowcount > 0

    def set_active_account(self, account_id: int) -> bool:
        with self._write() as c:
            if not c.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)
            ).fetchone():
                return False
            c.execute("UPDATE accounts SET active = 0")
            c.execute("UPDATE accounts SET active = 1 WHERE id = ?", (int(account_id),))
            return True

    def delete_account(self, account_id: int) -> bool:
        """删账号。连同它勾选过的监听群一起删；删的正好是当前账号时顺位把下一个设成当前。"""
        with self._write() as c:
            c.execute("DELETE FROM watched_chats WHERE account_id = ?", (int(account_id),))
            deleted = c.execute(
                "DELETE FROM accounts WHERE id = ?", (int(account_id),)
            ).rowcount > 0
            if deleted and not c.execute(
                "SELECT 1 FROM accounts WHERE active = 1"
            ).fetchone():
                nxt = c.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
                if nxt:
                    c.execute("UPDATE accounts SET active = 1 WHERE id = ?", (int(nxt["id"]),))
            return deleted

    def ensure_default_account(self, session_name: str) -> bool:
        """老库升级：把 config.toml 里那个 session 登记成账号并设为当前使用。

        已经有账号了就不动 —— 否则每次启动都会多出一行。
        """
        with self._write() as c:
            if c.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]:
                return False
            c.execute(
                "INSERT INTO accounts (phone, session, active, created_at) VALUES (?,?,1,?)",
                ("", session_name, iso(utcnow())),
            )
            return True

    def clear_account_login(self, account_id: int) -> bool:
        """退出登录后把这一行改回「没登录」。

        用户信息清掉、手机号留着 —— 手机号是重新登录时对号入座的依据，
        清掉的话再添加同一个号就会被当成新账号，表格里出现两行同一个号。
        """
        with self._write() as c:
            cur = c.execute(
                "UPDATE accounts SET user_id = NULL, first_name = '', last_name = '',"
                " username = '', last_login_at = '' WHERE id = ?",
                (int(account_id),),
            )
            return cur.rowcount > 0

    # ---------- 直接代理池 ----------
    @staticmethod
    def _proxy_row(row: sqlite3.Row) -> dict:
        keys = row.keys()
        return {
            "id": int(row["id"]),
            "name": row["name"] or "",
            "proxy_type": row["proxy_type"] or "http",
            "addr": row["addr"] or "",
            "port": int(row["port"] or 0),
            "username": row["username"] or "",
            "password": row["password"] or "",
            # MTProto 代理的 secret；老库升级后这一列才存在
            "secret": (row["secret"] or "") if "secret" in keys else "",
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
        }

    def list_proxies(self) -> list[dict]:
        with self._read() as c:
            rows = c.execute("SELECT * FROM proxies ORDER BY id").fetchall()
        return [self._proxy_row(row) for row in rows]

    def get_proxy(self, proxy_id: int) -> dict | None:
        with self._read() as c:
            row = c.execute("SELECT * FROM proxies WHERE id = ?", (int(proxy_id),)).fetchone()
        return self._proxy_row(row) if row else None

    def add_proxy(self, name: str, proxy_type: str, addr: str, port: int,
                  username: str = "", password: str = "", secret: str = "") -> dict:
        now = iso(utcnow())
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO proxies (name, proxy_type, addr, port, username, password,"
                " secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (name or "", proxy_type or "http", addr or "", int(port or 0),
                 username or "", password or "", secret or "", now, now),
            )
            proxy_id = int(cur.lastrowid)
        return self.get_proxy(proxy_id)  # type: ignore[return-value]

    def update_proxy(self, proxy_id: int, name: str, proxy_type: str, addr: str,
                     port: int, username: str = "", password: str = "",
                     secret: str = "") -> bool:
        with self._write() as c:
            return c.execute(
                "UPDATE proxies SET name = ?, proxy_type = ?, addr = ?, port = ?,"
                " username = ?, password = ?, secret = ?, updated_at = ? WHERE id = ?",
                (name or "", proxy_type or "http", addr or "", int(port or 0),
                 username or "", password or "", secret or "", iso(utcnow()), int(proxy_id)),
            ).rowcount > 0

    def delete_proxy(self, proxy_id: int) -> bool:
        with self._write() as c:
            c.execute("UPDATE accounts SET proxy_id = 0 WHERE proxy_id = ?", (int(proxy_id),))
            return c.execute("DELETE FROM proxies WHERE id = ?", (int(proxy_id),)).rowcount > 0

