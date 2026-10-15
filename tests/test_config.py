"""配置里的相对路径按配置文件所在目录解析，不是按代码目录。

容器里代码目录是镜像的一部分（`/app`，重建就没了），配置和数据在数据卷上
（`/data`）。用户把本机那份 config.toml 直接搬进去时里面写的是相对路径，
拼到代码目录上数据就会「凭空消失」。
"""

from __future__ import annotations

from pathlib import Path

from app import config as config_module


def _layout(tmp_path, monkeypatch):
    """代码目录和数据目录分开，模拟容器。"""
    code = tmp_path / "app"
    code.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config_module, "ROOT", code)
    return code, data


def test_relative_paths_resolve_next_to_config_file(tmp_path, monkeypatch):
    _, data = _layout(tmp_path, monkeypatch)
    (data / "config.toml").write_text(
        '[telegram]\nsession = "session/collector"\n\n[storage]\ndb = "pikpak.db"\n',
        encoding="utf-8",
    )

    cfg = config_module.load(data / "config.toml")

    assert cfg.db_path() == data / "pikpak.db"
    assert cfg.session_path() == data / "session" / "collector"


def test_defaults_resolve_against_config_path_not_code_dir(tmp_path, monkeypatch):
    """数据目录里还没有 config.toml（首次启动）时也不能退回代码目录。"""
    code, data = _layout(tmp_path, monkeypatch)

    cfg = config_module.load(data / "config.toml")

    assert cfg.db_path() == data / "pikpak.db"
    assert cfg.session_path() == data / "session" / "collector"
    assert code not in cfg.db_path().parents


def test_absolute_paths_are_left_alone(tmp_path, monkeypatch):
    _, data = _layout(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere.db"
    (data / "config.toml").write_text(
        f'[storage]\ndb = "{elsewhere.as_posix()}"\n', encoding="utf-8"
    )

    cfg = config_module.load(data / "config.toml")

    assert cfg.db_path() == elsewhere


def test_config_without_path_falls_back_to_root(tmp_path, monkeypatch):
    """手工构造、没带 path 的配置退回代码目录（保持老行为）。"""
    code, _ = _layout(tmp_path, monkeypatch)

    cfg = config_module.Config()

    assert cfg.db_path() == code / "pikpak.db"
    assert cfg.session_path() == code / "session" / "collector"
    assert isinstance(cfg.db_path(), Path)
