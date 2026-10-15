"""备份与还原：把整套数据打成一个归档，或者从归档搬回来。

换机器部署时（本机 → 服务器、旧容器 → 新容器）不想重新配一遍，就靠这个。
归档里除了日志什么都有：

    config.toml    应用配置（api_id / api_hash / 管理员密码 / 代理设置）
    pikpak.db      SQLite 库：链接、备注、账号、代理、秒传任务、各种开关
    session/       Telegram 登录态，等同账号密码，换机器后不用重新登录
    media/         缩略图
    shuju.json     tools/export_json.py 导出的静态快照
    data.js        同上，静态页用的那份

日志不进归档：那是运行痕迹，换台机器没有意义，只会让归档白白大一圈。

几个约定：

* 归档是 tar.gz，根目录一份 ``manifest.json``，其余成员名就是上面这些逻辑路径。
  还原时按同一套规则写回去，所以两边共用一个 :func:`plan`，不会走偏。
* 数据库用 SQLite 的在线备份 API 取，不直接拷文件 —— WAL 里可能还有没落盘的
  事务，直接拷出来的库轻则丢数据，重则和残留的 -wal 对不上直接损坏。
* 还原先解到临时目录、验完数据库再动现场数据。归档坏了不能把现有库删一半。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import __version__
from app import config as config_module

log = logging.getLogger("backup")

FORMAT = "pikpak-guangya-backup"
# 归档格式版本。加字段不用动它，只有「旧程序读不了新归档」时才 +1。
VERSION = 1
MANIFEST_NAME = "manifest.json"
ARCHIVE_SUFFIX = ".tar.gz"
# 命令行备份的默认落点，放在数据目录里，`docker cp` 出来就行。
# 它自己不进备份 —— plan() 是显式清单，不会把上一份归档套进下一份。
BACKUP_DIRNAME = "backups"
CONFIG_NAME = "config.toml"
DB_NAME = "pikpak.db"
SESSION_DIRNAME = "session"
MEDIA_DIRNAME = "media"
EXPORT_NAMES = ("shuju.json", "data.js")
# 永远不备份的东西（给页面展示用，实际由 plan() 的清单保证）
EXCLUDED = ("logs",)


class BackupError(Exception):
    """归档不对：不是备份、版本不认识、内容损坏。"""


@dataclass(frozen=True)
class Entry:
    arcname: str
    src: Path
    kind: str  # "file" | "dir"


# ---------------------------------------------------------------- 路径

def data_dir(cfg) -> Path:
    """数据目录：容器里是 /data，本机就是项目根目录。"""
    if getattr(cfg, "path", None):
        return Path(cfg.path).resolve().parent
    return config_module.ROOT


def backup_dir(cfg) -> Path:
    return data_dir(cfg) / BACKUP_DIRNAME


def session_dir(cfg) -> Path:
    """登录态文件所在目录。

    正常就是数据目录下的 ``session/``。config.toml 把 session 指到别处时，
    只有那个目录确实叫 session（或在数据目录里）才认 —— 否则整包带走用户的
    家目录就太吓人了，退回到数据目录下的 session/。
    """
    parent = Path(cfg.session_path()).parent
    if parent.name.lower() == SESSION_DIRNAME:
        return parent
    try:
        parent.resolve().relative_to(data_dir(cfg))
    except ValueError:
        return data_dir(cfg) / SESSION_DIRNAME
    return parent


def media_dir() -> Path:
    """缩略图目录。容器里 /app/media 是指向 /data/media 的软链，resolve 掉。"""
    return (config_module.ROOT / MEDIA_DIRNAME).resolve()


def default_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"pikpak-backup-{stamp}{ARCHIVE_SUFFIX}"


def _iter_files(root: Path):
    """目录下的普通文件。软链跳过：备份里出现软链只会让还原变得不可预料。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


# ---------------------------------------------------------------- 清单

def plan(cfg) -> list[Entry]:
    """这次备份会装哪些东西。备份和还原共用，两边不会走偏。"""
    entries: list[Entry] = []

    config_path = Path(cfg.path) if getattr(cfg, "path", None) else None
    if config_path and config_path.is_file():
        entries.append(Entry(CONFIG_NAME, config_path, "file"))

    db = Path(cfg.db_path())
    if db.is_file():
        entries.append(Entry(DB_NAME, db, "file"))

    sdir = session_dir(cfg)
    if sdir.is_dir():
        entries.append(Entry(SESSION_DIRNAME, sdir, "dir"))

    mdir = media_dir()
    if mdir.is_dir():
        entries.append(Entry(MEDIA_DIRNAME, mdir, "dir"))

    for name in EXPORT_NAMES:
        path = config_module.ROOT / name
        if path.is_file():
            entries.append(Entry(name, path, "file"))

    return entries


def describe(cfg) -> dict:
    """给设置页看的清单：装什么、多大、日志被排除了。"""
    items: list[dict] = []
    total = 0
    for entry in plan(cfg):
        if entry.kind == "file":
            size = entry.src.stat().st_size
            count = 1
        else:
            files = list(_iter_files(entry.src))
            size = sum(p.stat().st_size for p in files)
            count = len(files)
        items.append({
            "name": entry.arcname,
            "kind": entry.kind,
            "files": count,
            "bytes": size,
        })
        total += size
    return {
        "status": 200,
        "data_dir": str(data_dir(cfg)),
        "backup_dir": str(backup_dir(cfg)),
        "items": items,
        "total_bytes": total,
        "excluded": list(EXCLUDED),
        "suggested_name": default_name(),
    }


# ---------------------------------------------------------------- 打包

@contextlib.contextmanager
def _db_snapshot(src: Path):
    """取一份一致的库副本。

    不能直接拷 pikpak.db：WAL 里可能还有没落盘的事务，拷出来轻则丢数据，
    重则和残留的 -wal 对不上直接损坏。走 SQLite 自己的在线备份 API 就没这问题。
    """
    fd, name = tempfile.mkstemp(prefix="pikpak-db-", suffix=".db")
    os.close(fd)
    tmp = Path(name)
    try:
        source = sqlite3.connect(str(src))
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
                # 归档里的库不带 WAL：还原时不用连 -wal / -shm 一起搬，
                # 校验时也能直接只读打开（WAL 库只读打开要先有 -shm）。
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        finally:
            source.close()
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def create_archive(cfg, dest: Path | str | None = None) -> dict:
    """把数据打包成 tar.gz，返回 manifest。

    dest 省略时写到数据目录的 ``backups/`` 下，文件名带时间戳。
    """
    entries = plan(cfg)
    if not entries:
        raise BackupError("没有可备份的数据（配置、数据库、登录态都不存在）")

    target = Path(dest) if dest else backup_dir(cfg) / default_name()
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "app_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": [entry.arcname for entry in entries],
        "excluded": list(EXCLUDED),
    }

    # 先写 .part 再改名：中途失败不会留下一个看起来能用、其实残缺的归档。
    partial = target.with_name(target.name + ".part")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            for entry in entries:
                if entry.kind == "file":
                    if entry.arcname == DB_NAME:
                        with _db_snapshot(entry.src) as snapshot:
                            tar.add(snapshot, arcname=DB_NAME)
                    else:
                        tar.add(entry.src, arcname=entry.arcname)
                    continue
                for path in _iter_files(entry.src):
                    rel = path.relative_to(entry.src).as_posix()
                    tar.add(path, arcname=f"{entry.arcname}/{rel}")

            payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(payload))
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    manifest["path"] = str(target)
    manifest["bytes"] = target.stat().st_size
    log.info("已生成备份 %s（%s 字节）", target, manifest["bytes"])
    return manifest


# ---------------------------------------------------------------- 还原

def read_manifest(archive: Path | str) -> dict:
    """读归档的 manifest，顺便确认它确实是本项目的备份。"""
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"找不到文件 {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(MANIFEST_NAME)
            handle = tar.extractfile(member)
            if handle is None:
                raise BackupError("归档里没有 manifest.json")
            manifest = json.loads(handle.read().decode("utf-8"))
    except BackupError:
        raise
    except (tarfile.TarError, OSError, KeyError, EOFError) as e:
        raise BackupError(f"不是有效的备份归档：{e}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BackupError("归档里的 manifest.json 读不出来，文件可能损坏") from e

    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("这个文件不是本项目的备份归档")
    version = manifest.get("version")
    if not isinstance(version, int) or version > VERSION:
        raise BackupError(
            f"归档版本 {version} 比当前程序支持的 {VERSION} 新，先升级程序再还原"
        )
    return manifest


def _safe_name(name: str) -> str:
    """归档成员名必须是干净的相对路径，挡住 ../ 和绝对路径。"""
    raw = (name or "").replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise BackupError(f"归档里有绝对路径，拒绝解压：{name}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise BackupError(f"归档里有越界路径，拒绝解压：{name}")
    return "/".join(parts)


def _verify_db(path: Path) -> None:
    """确认归档里的库是能用的本项目的库。验不过就别动现场数据。"""
    with path.open("rb") as handle:
        head = handle.read(16)
    if head[:15] != b"SQLite format 3":
        raise BackupError("归档里的数据库不是 SQLite 文件")
    # 可读写打开（解出来的副本是我们自己的）：万一归档里的库还带着 WAL，
    # 只读打开会因为建不了 -shm 直接失败，正常备份反而被拒。
    conn = sqlite3.connect(str(path))
    try:
        names = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # 把 WAL 里可能有的内容落回主文件，之后只拷主文件才是完整的
        with contextlib.suppress(sqlite3.DatabaseError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError as e:
        raise BackupError(f"归档里的数据库打不开：{e}") from e
    finally:
        conn.close()
    if "shares" not in names:
        raise BackupError("归档里的数据库不像本项目的库（没有 shares 表）")


def _mirror_dir(src: Path, dst: Path) -> tuple[int, list[str]]:
    """把 src 的内容覆盖到 dst：同名覆盖，归档里没有的删掉。

    是「换成归档里那一份」而不是合并 —— 不然旧账号的登录态、旧缩略图会留在
    新数据里，看着像还原成功，实际是两份数据混在一起。删不掉的（文件被占用）
    记一条警告，不因此中断整个还原。
    """
    dst.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    count = _sync_tree(src, dst, warnings)
    return count, warnings


def _sync_tree(src: Path, dst: Path, warnings: list[str]) -> int:
    """递归覆盖：src 里有的写过去，dst 里多出来的删掉。"""
    count = 0
    wanted: set[str] = set()
    for child in sorted(src.iterdir()):
        if child.is_symlink():
            continue
        wanted.add(child.name)
        if child.is_dir():
            target = dst / child.name
            target.mkdir(parents=True, exist_ok=True)
            count += _sync_tree(child, target, warnings)
        elif child.is_file():
            shutil.copy2(child, dst / child.name)
            count += 1
    for child in list(dst.iterdir()):
        if child.name in wanted:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as e:
            warnings.append(f"{child} 删不掉（{e}），可能还在被占用")
    return count


def restore_archive(cfg, archive: Path | str, *, store=None) -> dict:
    """从归档还原配置和数据。日志目录不碰。"""
    manifest = read_manifest(archive)

    staging = Path(tempfile.mkdtemp(prefix="pikpak-restore-"))
    try:
        files = 0
        total = 0
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = _safe_name(member.name)
                if not name or name == MANIFEST_NAME:
                    continue
                if member.isdir():
                    (staging / name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    # 软链 / 设备文件：备份里本来就不该有，直接忽略
                    continue
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                with target.open("wb") as out:
                    shutil.copyfileobj(handle, out)
                files += 1
                total += int(member.size or 0)

        # 先验库再动现场数据：归档损坏时不能把现有数据删掉一半
        staged_db = staging / DB_NAME
        if staged_db.is_file():
            _verify_db(staged_db)

        applied: list[str] = []
        warnings: list[str] = []

        # 还原之后配置说了算：归档里带了 config.toml，就用它算数据文件该放哪儿。
        # 少了这一步，「先还原再首次启动」那条路（此时数据目录里还没有 config.toml）
        # 只能拿默认值去猜，数据库会落到代码目录而不是数据卷上。
        staged_config = staging / CONFIG_NAME
        final_config = (
            Path(cfg.path) if getattr(cfg, "path", None)
            else data_dir(cfg) / CONFIG_NAME
        )
        effective = cfg
        if staged_config.is_file():
            try:
                effective = config_module.load(staged_config)
                # 相对路径要按**最终位置**解析，不能按临时解压目录
                effective.path = final_config
            except Exception as e:      # 配置坏了也要把别的数据还原回去
                effective = cfg
                warnings.append(
                    f"归档里的 config.toml 读不出来（{e}），按当前配置还原"
                )
            final_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_config, final_config)
            applied.append(CONFIG_NAME)

        if staged_db.is_file():
            target = Path(effective.db_path())
            target.parent.mkdir(parents=True, exist_ok=True)
            swap = target.with_name(target.name + ".restore")
            shutil.copy2(staged_db, swap)
            os.replace(swap, target)
            # 旧库的 WAL / SHM 必须一起清掉：它们属于被换掉的那个库，
            # 留着会让 SQLite 把旧事务套到新库上，直接损坏。
            for suffix in ("-wal", "-shm"):
                side = target.with_name(target.name + suffix)
                with contextlib.suppress(OSError):
                    side.unlink()
            applied.append(DB_NAME)
            if store is not None and Path(store.path) != target:
                # 配置里的库路径和当前进程用的不一样（刚被还原的配置改的），
                # 就地改掉，别让页面继续读一个空库
                store.path = target

        for arcname, target_dir in (
            (SESSION_DIRNAME, session_dir(effective)),
            (MEDIA_DIRNAME, media_dir()),
        ):
            staged = staging / arcname
            if not staged.is_dir():
                continue
            count, failed = _mirror_dir(staged, target_dir)
            files += count
            applied.append(f"{arcname}/")
            warnings.extend(failed)

        for name in EXPORT_NAMES:
            staged = staging / name
            if staged.is_file():
                shutil.copy2(staged, config_module.ROOT / name)
                applied.append(name)

        if store is not None:
            # 换完库立刻按新库跑一次建表 / 迁移；这一步顺带清掉聚合缓存
            store.init()

        log.info("已从 %s 还原：%s", archive, "、".join(applied) or "（归档是空的）")
        return {
            "status": 200,
            "applied": applied,
            "files": files,
            "bytes": total,
            "created_at": manifest.get("created_at", ""),
            "app_version": manifest.get("app_version", ""),
            "warnings": warnings,
            # config.toml 和登录态都是启动时读进内存的，热还原不生效
            "restart_required": True,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)
