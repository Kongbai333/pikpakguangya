"""命令行还原：把归档里的配置和数据写回去。

本机：
    python -m tools.restore D:\\备份\\pikpak-backup-20261015-120000.tar.gz

容器里（先把归档放进去，或者直接还原数据卷里的那一份）：
    docker cp pikpak-backup-xxx.tar.gz pikpak-collector:/data/
    docker exec pikpak-collector python -m tools.restore /data/pikpak-backup-xxx.tar.gz
    docker restart pikpak-collector

和设置页的「还原」是同一套逻辑：日志不动，其余按归档那一份覆盖。
config.toml 和登录态是启动时读进内存的，还原完必须重启才会生效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup  # noqa: E402
from app import config as config_module  # noqa: E402
from app import log_setup  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="从备份归档还原配置与数据")
    ap.add_argument("archive", help="备份归档路径（.tar.gz）")
    ap.add_argument("--db", default=None, help="还原到哪个数据库，默认取 config.toml")
    args = ap.parse_args()

    log_setup.setup_logging()
    cfg = config_module.load()
    store = Store(args.db or cfg.db_path())
    try:
        result = backup.restore_archive(cfg, args.archive, store=store)
    except backup.BackupError as e:
        raise SystemExit(f"还原失败：{e}")

    print(f"已还原：{'、'.join(result['applied']) or '（归档是空的）'}")
    print(f"  文件：{result['files']} 个，{result['bytes']} 字节")
    if result.get("created_at"):
        print(f"  备份时间：{result['created_at']}（程序版本 {result['app_version']}）")
    for warning in result.get("warnings", []):
        print(f"  ⚠ {warning}")
    print("日志没有动。config.toml 和登录态要重启服务 / 容器才会生效。")


if __name__ == "__main__":
    main()
