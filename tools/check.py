"""校验分享链接是否还有效。

用法：
    python -m tools.check --limit 20            # 校验 20 条待校验/过期的
    python -m tools.check --all --limit 500     # 强制重校验 500 条
    python -m tools.check --url https://mypikpak.com/s/xxxx   # 单条，直接打印
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import pikpak  # noqa: E402
from app.store import Store, iso, utcnow  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="校验分享链接有效性")
    ap.add_argument("--db", default=None, help="数据库路径，默认取 config.toml")
    ap.add_argument("--limit", type=int, default=50, help="本次校验多少条")
    ap.add_argument("--all", action="store_true", help="忽略 checked_at，全部重校验")
    ap.add_argument("--url", default=None, help="只校验单条链接并打印结果")
    args = ap.parse_args()

    cfg = config_module.load()

    if args.url:
        res = pikpak.check_share(args.url, timeout=cfg.checker.timeout_seconds)
        print(f"{res.link_id}\n  状态 = {res.status}\n  文件名 = {res.title or '-'}"
              f"\n  文件数 = {res.file_num}\n  剩余秒 = {res.expire_seconds}"
              f"\n  解析来源 = {res.source}\n  错误 = {res.error or '-'}")
        return

    store = Store(args.db or cfg.db_path())
    store.init()
    if args.all:
        with store._write() as c:  # noqa: SLF001 - CLI 内部工具
            c.execute("UPDATE shares SET checked_at = NULL")
        print(f"已重置校验时间（当前 {iso(utcnow())}），将全部重校验")

    tally = asyncio.run(
        pikpak.check_pending(store, cfg.checker, limit=args.limit)
    )
    print("本次结果：", tally)
    print("库内统计：", store.stats())


if __name__ == "__main__":
    main()
