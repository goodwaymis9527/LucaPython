"""統一一份 DB 介面：開發（本機 Docker 的 postgres17）與正式（Neon pooler / RDS）只靠 DATABASE_URL 切換。

與 Node 版 lib/db.js 的差別：
- Python 沒有 Neon 的 HTTP transport（443），本機 outbound 5432 被封時連不出去，
  所以本機一律走 Docker 裡的 PostgreSQL；正式區在 VPC 內可出 5432，Neon pooler 可用。
- psycopg v3 的 ConnectionPool 是「每 worker 一個池」；Neon 的 pooler 是 transaction mode，
  所以 prepared statements 必須關掉（settings.prepare 預設 False）。

回傳一律是 list[dict]（dict_row），與 Node 版 query() 回 rows 陣列的行為一致。
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings, sslmode

_pool: ConnectionPool | None = None


def conninfo() -> str:
    if settings.database_url:
        url = settings.database_url
        mode = sslmode()
        if mode:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode={mode}"
        return url

    # 沒給 URI 時退回 PG* 環境變數（pgAdmin / psql 習慣的那套命名）
    parts = [
        f"host={os.environ.get('PGHOST', 'localhost')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"dbname={os.environ.get('PGDATABASE', 'awsLucaPysn')}",
        f"user={os.environ.get('PGUSER', 'admin')}",
    ]
    password = os.environ.get("PGPASSWORD")
    if password:
        parts.append(f"password={password}")
    parts.append(f"connect_timeout={settings.connect_timeout}")
    mode = settings.sslmode()
    if mode:
        parts.append(f"sslmode={mode}")
    return " ".join(parts)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not settings.database_url and not os.environ.get("PGDATABASE"):
            raise RuntimeError(
                "DATABASE_URL 沒有設定。開發請檢查 .env.local，正式區請檢查 ECS 任務的環境變數"
            )
        _pool = ConnectionPool(
            conninfo=conninfo(),
            min_size=settings.pool_min,
            max_size=settings.pool_max,
            timeout=settings.connect_timeout,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "application_name": settings.app_name,
                "connect_timeout": settings.connect_timeout,
            },
        )
        _pool.open(wait=False)
    return _pool


def query(sql: str, params: tuple | list | None = None) -> list[dict[str, Any]]:
    """只用參數化，不拼字串 SQL：%s 由 driver 送，不會字串插值。"""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params, prepare=settings.prepare)
            if cur.description is None:
                conn.commit()
                return []
            rows = cur.fetchall()
            conn.commit()
            return rows


def list_items(limit: int = 50) -> list[dict[str, Any]]:
    capped = min(int(limit) or 50, 200)
    return query(
        "SELECT id, title, body, created_at FROM items ORDER BY id DESC LIMIT %s",
        [capped],
    )


def add_item(title: str, body: str = "") -> dict[str, Any]:
    rows = query(
        "INSERT INTO items (title, body) VALUES (%s, %s) "
        "RETURNING id, title, body, created_at",
        [title, body],
    )
    return rows[0] if rows else {}


def ping_db() -> bool:
    rows = query("SELECT 1 AS ok")
    return bool(rows) and rows[0]["ok"] == 1


def db_info() -> dict[str, Any]:
    """給頁面與 /api/health 顯示「現在接的是哪一邊」，排錯時很好用。"""
    parsed = urlsplit(settings.database_url) if settings.database_url else None
    return {
        "driver": settings.driver,
        "region": settings.region,
        "env": settings.app_env,
        # 只印 host / db 部分，不要把含密碼的完整字串帶出去
        "host": (parsed.hostname if parsed else None) or os.environ.get("PGHOST"),
        "database": (parsed.path.lstrip("/") if parsed else None) or os.environ.get("PGDATABASE"),
        "pool": {"min": settings.pool_min, "max": settings.pool_max},
        "prepare": settings.prepare,
    }
