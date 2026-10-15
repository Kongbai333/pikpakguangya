"""配置加载：config.toml（缺失时用默认值，只有监听器需要 telegram 段）。"""

from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"
# 配置文件位置的覆盖入口。Docker 里代码目录是镜像的一部分、配置在数据卷上，
# 就用它指过去（见 Dockerfile 的 PIKPAK_CONFIG）。
CONFIG_ENV = "PIKPAK_CONFIG"

T = TypeVar("T")


def config_path() -> Path:
    """配置文件路径：环境变量优先，其次项目根目录的 config.toml。"""
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _apply_env_overrides(cfg: Config) -> Config:
    """监听地址的兜底覆盖。

    容器里必须监听 0.0.0.0，否则端口映射不出去；而本机跑的配置写的是
    127.0.0.1。用户把本地 config.toml 直接拷进容器是很常见的操作，
    真让他对着一个连不上的容器排查太亏，所以留两个环境变量兜底。
    """
    host = os.environ.get("PIKPAK_HOST", "").strip()
    port = os.environ.get("PIKPAK_PORT", "").strip()
    if host:
        cfg.api.host = host
    if port.isdigit():
        cfg.api.port = int(port)
    return cfg


@dataclass
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session: str = "session/collector"


@dataclass
class CollectorConfig:
    watch: list[str] = field(default_factory=list)
    backfill_limit: int = 200
    merge_window_seconds: int = 90
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)


@dataclass
class StorageConfig:
    db: str = "pikpak.db"


@dataclass
class CheckerConfig:
    enabled: bool = True
    interval_minutes: int = 60
    recheck_after_hours: int = 24
    # 单批校验条数
    batch_size: int = 100
    # 并发别开大：PikPak 有风控，实测请求过密会直接断 TLS
    concurrency: int = 2
    min_delay_seconds: float = 1.5
    timeout_seconds: int = 25
    # 连续失败这么多次就中止本批并等下一轮（被限流时保护账号和 IP）
    max_consecutive_errors: int = 5
    # 上次是 ERROR 的链接多久后重试（限流/网络抖动恢复得比 24 小时快）
    error_retry_minutes: int = 30


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    max_items: int = 50000


@dataclass
class AdminConfig:
    password: str = ""
    session_hours: int = 24


@dataclass
class AppConfig:
    """展示用时区。

    库里存的一律是 UTC；这里只影响「按天 / 按分钟」分桶的统计和页面标签。
    留空 = 跟随本机时区。Windows 上要用 IANA 名字（如 Asia/Shanghai）
    需要装 tzdata，装不上会自动退回本机时区并告警。
    """
    timezone: str = ""


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    checker: CheckerConfig = field(default_factory=CheckerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    app: AppConfig = field(default_factory=AppConfig)
    path: Path | None = None

    def resolve_path(self, value: str) -> Path:
        """把配置里的路径解析成绝对路径。

        相对路径按**配置文件所在目录**解析，不是按代码目录。本机跑的时候配置就在
        项目根目录，两者是同一个地方，行为不变；容器里配置在数据卷上
        （`/data/config.toml`），用户把本机那份 config.toml 直接搬进去
        （里面写的是 `pikpak.db` / `session/collector` 这种相对路径）也不会跑到
        代码目录里去 —— 代码目录是镜像的一部分，容器一重建就没了，数据会凭空
        「消失」。
        """
        p = Path(value)
        if p.is_absolute():
            return p
        base = Path(self.path).parent if self.path else ROOT
        return base / p

    def db_path(self) -> Path:
        """数据库路径。"""
        return self.resolve_path(self.storage.db)

    def session_path(self) -> Path:
        """登录态文件路径（Telethon 会再补 `.session`）。"""
        return self.resolve_path(self.telegram.session)

def _build(cls: type[T], data: Any) -> T:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise TypeError(f"{cls.__name__} 配置段应为表（table），实际是 {type(data).__name__}")
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        warnings.warn(f"配置段 [{cls.__name__}] 里有未知键被忽略：{sorted(unknown)}", stacklevel=2)
    return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[call-arg]


def load(path: Path | str | None = None) -> Config:
    """读取配置。文件不存在时返回默认值并告警（Web 展示不需要 Telegram 凭据）。"""
    p = Path(path) if path is not None else config_path()
    if not p.exists():
        warnings.warn(f"未找到配置文件 {p}，使用默认配置（监听器无法启动）", stacklevel=2)
        return _apply_env_overrides(Config(path=p))
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(
        telegram=_build(TelegramConfig, raw.get("telegram")),
        collector=_build(CollectorConfig, raw.get("collector")),
        storage=_build(StorageConfig, raw.get("storage")),
        checker=_build(CheckerConfig, raw.get("checker")),
        api=_build(ApiConfig, raw.get("api")),
        admin=_build(AdminConfig, raw.get("admin")),
        app=_build(AppConfig, raw.get("app")),
        path=p,
    )
    return _apply_env_overrides(cfg)
