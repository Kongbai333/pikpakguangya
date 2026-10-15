"""把 {items:[{ID,LINK,CODE,MSG}]} 形式的 JSON 导入数据库。

用途：你已有的 shuju.json（或 Telegram 官方导出后自己解析出的 JSON）可以
直接进库，不用等 Telegram 登录就能把整条链路跑通、看到页面效果。

用法：
    python -m tools.import_json --file shuju.json --chat-title 历史导入
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app import pikpak  # noqa: E402
from app.store import Store  # noqa: E402


def import_items(store: Store, items: list[dict], chat_id: int = 0, chat_title: str = "导入") -> dict:
    base = datetime.now(timezone.utc)
    stats = {"read": 0, "shares": 0, "updated": 0, "skipped": 0}
    for idx, raw in enumerate(items):
        stats["read"] += 1
        url = (raw.get("LINK") or raw.get("link") or "").strip()
        link_id = pikpak.link_id_of(url)
        if not link_id:
            stats["skipped"] += 1
            continue
        code = (raw.get("CODE") or raw.get("code") or "").strip()
        pass_code = None if code in ("", "无") else code
        remark = (raw.get("MSG") or raw.get("msg") or "").strip()
        # 保持文件顺序：给每条一个递增时间戳，方便页面按时间排序
        posted_at = (base + timedelta(seconds=idx)).isoformat(timespec="seconds")
        res = store.add_message(
            chat_id=chat_id,
            chat_title=chat_title,
            message_id=idx + 1,
            posted_at=posted_at,
            raw_text=remark or url,
            link_id=link_id,
            url=url if url.startswith("http") else f"https://mypikpak.com/s/{link_id}",
            pass_code=pass_code,
            remark=remark,
        )
        if res.get("share_created"):
            stats["shares"] += 1
        else:
            stats["updated"] += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 JSON 到链接库")
    ap.add_argument("--file", default="shuju.json", help="输入 JSON（默认 shuju.json）")
    ap.add_argument("--db", default=None, help="数据库路径，默认取 config.toml 的 storage.db")
    ap.add_argument("--chat-id", type=int, default=0, help="伪装的 chat_id，默认 0")
    ap.add_argument("--chat-title", default="导入", help="来源标记，会显示在页面的来源列")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"找不到文件 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SystemExit("JSON 结构应为 {items:[...]} 或直接是数组")

    cfg = config_module.load()
    store = Store(args.db or cfg.db_path())
    store.init()
    stats = import_items(store, items, chat_id=args.chat_id, chat_title=args.chat_title)

    print(f"读取 {stats['read']} 条，新增链接 {stats['shares']} 条，"
          f"已存在仅补备注 {stats['updated']} 条，跳过 {stats['skipped']} 条")
    print("库内统计：", store.stats())


if __name__ == "__main__":
    main()
