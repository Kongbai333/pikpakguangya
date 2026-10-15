"""备份 / 还原：归档内容、还原覆盖、坏归档的处理。"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup
from app import config as config_module
from app.api import create_app
from app.store import Store, iso, utcnow


def _cfg(tmp_path: Path, *, root: str = "data", session: str = "session/collector") -> config_module.Config:
    """造一份「数据目录 = tmp/data」的配置，别碰真实项目目录。"""
    base = tmp_path / root
    base.mkdir(parents=True, exist_ok=True)
    return config_module.Config(
        telegram=config_module.TelegramConfig(session=session),
        storage=config_module.StorageConfig(db=str(base / "pikpak.db")),
        checker=config_module.CheckerConfig(enabled=False),
        admin=config_module.AdminConfig(password="test-admin"),
        path=base / "config.toml",
    )


def _seed(cfg: config_module.Config) -> None:
    """造出「一套正在跑的数据」：配置、库、登录态、缩略图、导出快照、日志。"""
    base = Path(cfg.path).parent
    Path(cfg.path).write_text('[admin]\npassword = "secret"\n', encoding="utf-8")
    store = Store(cfg.db_path())
    store.init()
    store.add_message(
        chat_id=-100123, chat_title="测试群", message_id=1, posted_at=iso(utcnow()),
        raw_text="示例", link_id="AAAAAAAAAAAAAAAAAAAAAA",
        url="https://mypikpak.com/s/AAAAAAAAAAAAAAAAAAAAAA", pass_code="f766", remark="示例合集",
    )

    session = Path(cfg.session_path())
    session.parent.mkdir(parents=True, exist_ok=True)
    session.with_suffix(".session").write_bytes(b"session-bytes")

    thumb = backup.media_dir() / "thumbs" / "1" / "2" / "3-0.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg-bytes")

    (backup.data_dir(cfg) / "logs").mkdir(parents=True, exist_ok=True)
    (backup.data_dir(cfg) / "logs" / "server.log").write_text("log line\n", encoding="utf-8")


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    """把 media 目录也挪进 tmp —— 它写死在项目根目录下。"""
    root = tmp_path / "media-root"
    root.mkdir()
    monkeypatch.setattr(config_module, "ROOT", root)
    return root


def test_archive_has_everything_except_logs(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    manifest = backup.create_archive(cfg, archive)

    assert manifest["format"] == backup.FORMAT
    assert manifest["excluded"] == ["logs"]
    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        assert backup.MANIFEST_NAME in names
        assert "config.toml" in names
        assert "pikpak.db" in names
        assert "session/collector.session" in names
        assert "media/thumbs/1/2/3-0.jpg" in names
        assert not [n for n in names if n.startswith("logs")]


def test_archive_keeps_db_consistent(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    backup.create_archive(cfg, archive)

    with tarfile.open(archive, "r:gz") as tar:
        extracted = tmp_path / "out.db"
        extracted.write_bytes(tar.extractfile("pikpak.db").read())
    conn = sqlite3.connect(extracted)
    try:
        assert conn.execute("SELECT COUNT(*) FROM shares").fetchone()[0] == 1
    finally:
        conn.close()


def test_default_target_lands_in_backups_dir(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    manifest = backup.create_archive(cfg)
    assert Path(manifest["path"]).parent == backup.backup_dir(cfg)
    assert Path(manifest["path"]).name.startswith("pikpak-backup-")


def test_restore_round_trip(tmp_path, media_root):
    source = _cfg(tmp_path, root="source")
    _seed(source)
    archive = tmp_path / "backup.tar.gz"
    backup.create_archive(source, archive)

    # 目标机器：全新目录，什么都没有
    target = _cfg(tmp_path, root="target")
    Path(target.path).parent.mkdir(parents=True, exist_ok=True)
    result = backup.restore_archive(target, archive, store=Store(target.db_path()))

    assert set(result["applied"]) == {"config.toml", "pikpak.db", "session/", "media/"}
    assert result["restart_required"] is True
    assert "secret" in Path(target.path).read_text(encoding="utf-8")
    assert Path(target.session_path()).with_suffix(".session").read_bytes() == b"session-bytes"
    assert (backup.media_dir() / "thumbs/1/2/3-0.jpg").read_bytes() == b"jpeg-bytes"
    store = Store(target.db_path())
    assert store.stats()["shares"] == 1
    # 日志目录不会被还原出来
    assert not (backup.data_dir(target) / "logs" / "server.log").exists()


def test_restore_replaces_stale_session_and_thumbs(tmp_path, media_root):
    source = _cfg(tmp_path, root="source")
    _seed(source)
    archive = tmp_path / "backup.tar.gz"
    backup.create_archive(source, archive)

    target = _cfg(tmp_path, root="target")
    session = Path(target.session_path())
    session.parent.mkdir(parents=True, exist_ok=True)
    session.with_suffix(".session").write_bytes(b"stale")
    stale_thumb = backup.media_dir() / "thumbs" / "old.jpg"
    stale_thumb.parent.mkdir(parents=True, exist_ok=True)
    stale_thumb.write_bytes(b"old")

    backup.restore_archive(target, archive)

    assert session.with_suffix(".session").read_bytes() == b"session-bytes"
    assert not stale_thumb.exists()


def test_restore_clears_stale_wal(tmp_path, media_root):
    source = _cfg(tmp_path, root="source")
    _seed(source)
    archive = tmp_path / "backup.tar.gz"
    backup.create_archive(source, archive)

    target = _cfg(tmp_path, root="target")
    db = Path(target.db_path())
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    (db.parent / "pikpak.db-wal").write_bytes(b"stale-wal")
    (db.parent / "pikpak.db-shm").write_bytes(b"stale-shm")

    backup.restore_archive(target, archive)

    assert not (db.parent / "pikpak.db-wal").exists()
    assert not (db.parent / "pikpak.db-shm").exists()
    assert Store(db).stats()["shares"] == 1


def test_rejects_foreign_archive(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "other.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps({"format": "something-else"}).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(backup.BackupError, match="不是本项目的备份归档"):
        backup.restore_archive(cfg, archive)


def test_rejects_newer_version(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "future.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(
            {"format": backup.FORMAT, "version": backup.VERSION + 1}
        ).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(backup.BackupError, match="先升级程序"):
        backup.restore_archive(cfg, archive)


def test_rejects_plain_gzip(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    bogus = tmp_path / "bogus.tar.gz"
    bogus.write_bytes(b"not a tar at all")
    with pytest.raises(backup.BackupError):
        backup.restore_archive(cfg, bogus)


def test_rejects_path_traversal_member(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(
            {"format": backup.FORMAT, "version": backup.VERSION}
        ).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        body = b"pwned"
        evil = tarfile.TarInfo("../../pwned.txt")
        evil.size = len(body)
        tar.addfile(evil, io.BytesIO(body))

    with pytest.raises(backup.BackupError, match="越界路径"):
        backup.restore_archive(cfg, archive)
    assert not (tmp_path / "pwned.txt").exists()


def test_rejects_corrupt_db_before_touching_anything(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "broken.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(
            {"format": backup.FORMAT, "version": backup.VERSION}
        ).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        body = b"definitely not sqlite"
        bad = tarfile.TarInfo("pikpak.db")
        bad.size = len(body)
        tar.addfile(bad, io.BytesIO(body))

    with pytest.raises(backup.BackupError, match="不是 SQLite"):
        backup.restore_archive(cfg, archive)
    # 现场数据必须原样还在
    assert Path(cfg.path).exists()
    assert Store(cfg.db_path()).stats()["shares"] == 1


def test_describe_lists_entries_and_excludes_logs(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    info = backup.describe(cfg)
    names = {item["name"] for item in info["items"]}
    assert {"config.toml", "pikpak.db", "session", "media"} <= names
    assert info["excluded"] == ["logs"]
    assert info["total_bytes"] > 0


# ------------------------------------------------------------------ 接口

def test_api_backup_info_and_download(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/backup").status_code == 401   # 未登录
        client.post("/api/web-auth/login", json={"password": "test-admin"})

        info = client.get("/api/backup").json()
        assert {item["name"] for item in info["items"]} >= {"config.toml", "pikpak.db"}

        res = client.get("/api/backup/download")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/gzip"
        assert "attachment" in res.headers["content-disposition"]
        with tarfile.open(fileobj=io.BytesIO(res.content), mode="r:gz") as tar:
            assert "config.toml" in tar.getnames()


def test_api_restore_upload(tmp_path, media_root):
    source = _cfg(tmp_path, root="source")
    _seed(source)
    archive = tmp_path / "backup.tar.gz"
    backup.create_archive(source, archive)

    target = _cfg(tmp_path, root="target")
    Path(target.path).parent.mkdir(parents=True, exist_ok=True)
    app = create_app(target)
    with TestClient(app) as client:
        client.post("/api/web-auth/login", json={"password": "test-admin"})
        res = client.post(
            "/api/backup/restore",
            params={"name": "backup.tar.gz"},
            content=archive.read_bytes(),
            headers={"Content-Type": "application/gzip"},
        )
        assert res.status_code == 200, res.text
        assert set(res.json()["applied"]) >= {"config.toml", "pikpak.db"}

        # 还原之后库里立刻能查到
        assert client.get("/api/items").json()["total"] == 1


def test_api_restore_rejects_garbage(tmp_path, media_root):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/api/web-auth/login", json={"password": "test-admin"})
        res = client.post("/api/backup/restore", content=b"not an archive")
        assert res.status_code == 400
