"""从数据库导出 shuju.json（静态页格式），可选顺带生成 data.js。

用法：
    python -m tools.export_json                      # 导出到 shuju.json
    python -m tools.export_json --build              # 再调 build.ps1 生成 data.js
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app.store import Store  # noqa: E402

ROOT = config_module.ROOT


def main() -> None:
    ap = argparse.ArgumentParser(description="导出链接库为静态页 JSON")
    ap.add_argument("--db", default=None, help="数据库路径，默认取 config.toml")
    ap.add_argument("--out", default="shuju.json", help="输出文件（默认 shuju.json）")
    ap.add_argument("--limit", type=int, default=100000, help="最多导出多少条")
    ap.add_argument("--build", action="store_true", help="导出后再运行 build.ps1 生成 data.js")
    args = ap.parse_args()

    cfg = config_module.load()
    store = Store(args.db or cfg.db_path())
    store.init()
    items = store.export_items(limit=args.limit)
    payload = {"status": 200, "items": items}

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"已导出 {len(items)} 条到 {out}")

    if args.build:
        script = ROOT / "build.ps1"
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            check=True, cwd=ROOT,
        )


if __name__ == "__main__":
    main()
