"""命令行备份：把整套数据打成一个 tar.gz。

本机：
    python -m tools.backup
    python -m tools.backup --out D:\\备份\\pikpak.tar.gz

容器里（打完 docker cp 出来）：
    docker exec pikpak-collector python -m tools.backup
    docker cp pikpak-collector:/data/backups/<归档名> .

和设置页的「下载备份」是同一套逻辑，装的东西完全一样：配置、数据库、
Telegram 登录态、缩略图、导出的静态快照；日志不进归档。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup  # noqa: E402
from app import config as config_module  # noqa: E402
from app import log_setup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="备份配置与数据")
    ap.add_argument("--out", default=None, help="输出文件，默认写到数据目录的 backups/")
    args = ap.parse_args()

    log_setup.setup_logging()
    cfg = config_module.load()
    try:
        manifest = backup.create_archive(cfg, args.out)
    except backup.BackupError as e:
        raise SystemExit(f"备份失败：{e}")

    print(f"已生成备份 {manifest['path']}")
    print(f"  包含：{'、'.join(manifest['entries'])}")
    print(f"  大小：{manifest['bytes']} 字节")
    print(f"  排除：{'、'.join(manifest['excluded'])}")


if __name__ == "__main__":
    main()
