#!/usr/bin/env python3
"""套 schema：本機直接對 Docker 的 postgres17 跑，正式區在 VPC 內透過 ECS Execute Command 跑。

設計與 Node 版 scripts/migrate.js 相同：
- schema 由這份腳本管理，不在請求路徑裡跑 ensure_schema()
- 遷移要用「直接連線」：PgBouncer transaction mode 不支援 session 級行為（SET、prepared statements）
  → DATABASE_URL 若指向 `-pooler` endpoint，請改用直接 endpoint（或 Neon SQL Editor）

執行：
  本機（連 docker）  : python scripts/migrate.py
  容器內              : docker exec luca-python python scripts/migrate.py
  VPC 內（ECS exec）  : python scripts/migrate.py

若要用「本機連 Neon」這條路：本機 outbound 5432 被封（實測 TCP timeout），
Python 沒有 Neon 的 HTTP transport，所以本機連 Neon 一定失敗；請改用 Docker 的資料庫或 Neon SQL Editor。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = ROOT / "schema.sql"


def conninfo() -> str:
    url = os.environ.get("MIGRATE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL 沒有設定。用 `python scripts/migrate.py`（.env.local 会被 app.config 讀，"
            "或直接把 DATABASE_URL 放環境變數）"
        )
    mode = os.environ.get("DB_SSLMODE")
    if mode and "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode={mode}"
    return url


def statements() -> list[str]:
    raw = SCHEMA.read_text(encoding="utf-8")
    kept = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in kept.split(";") if s.strip()]


def run() -> None:
    # 先試著把資料庫建起來（本機 postgres17 或 RDS 都可用 CREATE DATABASE 權限的帳號）
    target = psycopg.connect(conninfo(), connect_timeout=10, autocommit=True)
    try:
        for sql in statements():
            target.execute(sql)
            print("✔ " + " ".join(sql.split())[:60])
    finally:
        target.close()

    check = psycopg.connect(conninfo(), connect_timeout=10, autocommit=True)
    try:
        row = check.execute(
            "select current_database() as db, count(*) filter (where table_name = 'items') as items_table "
            "from information_schema.tables where table_schema = 'public'"
        ).fetchone()
        print(f"資料庫 {row[0]}｜items 表存在={bool(row[1])}")
    finally:
        check.close()


if __name__ == "__main__":
    try:
        # app.config 的 dotenv 只在 app 路徑有；這裡自己讀 .env.local
        dotenv_path = ROOT / ".env.local"
        if dotenv_path.exists():
            from dotenv import load_dotenv

            load_dotenv(dotenv_path)
        run()
    except Exception as err:
        print("❌ migrate 失敗：" + str(err), file=sys.stderr)
        sys.exit(1)
