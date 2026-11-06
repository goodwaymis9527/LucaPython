"""環境變數集中處（對應 Node 版的 next.config.js + process.env）。

讀訂單：程序環境變數 > .env.local > .env
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (BASE_DIR / ".env.local", BASE_DIR / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    driver: str
    app_env: str
    region: str
    pool_min: int
    pool_max: int
    connect_timeout: int
    prepare: bool
    port: int
    workers: int
    log_level: str
    app_name: str


settings = Settings(
    database_url=_env("DATABASE_URL"),
    driver=(_env("DB_DRIVER", "docker") or "docker").lower(),
    app_env=_env("APP_ENV", "development"),
    region=_env("AWS_REGION", "local"),
    pool_min=_env_int("DB_POOL_MIN", 1),
    pool_max=_env_int("DB_POOL_MAX", 5),
    connect_timeout=_env_int("DB_CONNECT_TIMEOUT", 10),
    # Neon pooler（transaction mode）不支援 prepared statements，所以預設關掉
    prepare=(_env("DB_PREPARE", "false").lower() in ("1", "true", "yes")),
    port=_env_int("PORT", 8000),
    workers=_env_int("GUNICORN_WORKERS", 1),
    log_level=_env("LOG_LEVEL", "info"),
    app_name=_env("APP_NAME", "luca-python"),
)


def db_host() -> str | None:
    """只取 host 部分給頁面顯示，不要把含密碼的完整字串帶出去。"""
    if settings.database_url:
        try:
            return urlsplit(settings.database_url).hostname
        except ValueError:
            return None
    return _env("PGHOST") or "localhost"


def sslmode() -> str:
    """DB_SSLMODE 若設定且 URI 裡沒有 sslmode，就補到 conninfo 上。"""
    mode = _env("DB_SSLMODE")
    if mode and settings.database_url and "sslmode" not in settings.database_url:
        return mode
    return ""
