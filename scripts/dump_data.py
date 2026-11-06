r"""dump_data.py — 把本機（postgres17）的資料印成「可貼到 Neon SQL Editor」的 SQL

為什麼要這樣設計：本機 outbound 5432 連不到 Neon（實測 timeout），所以「本機連 Neon 直接寫入」這條路走不通；
實務做法是本地讀資料 → 產生冪等 SQL → 到 Neon SQL Editor（或 VPC 內的一-shot 任務）貼上執行。

產生的 SQL 是 upsert（ON CONFLICT (id) DO UPDATE），重複貼不會产生重複資料；
`--mode=insert` 則改成 DO NOTHING（只補缺漏，不覆蓋目標端已有的編輯）。

執行：
  python scripts/dump_data.py                            # 印到 stdout（讀 .env.local 的本機 DB）
  python scripts/dump_data.py --out=sync.sql             # 寫檔
  python scripts/dump_data.py --table=items --mode=insert
  python scripts/dump_data.py --DATABASE_URL='postgresql://admin:%40pw@localhost:5432/awsLucaPysn'

注意：本機從 host 跑要用 `localhost`（`postgres17` 這個名字只在 docker 的 shared-network 內解析得到）；
`ON CONFLICT (id)` 需要該表有 id 主鍵（items 符合）。

拿到 SQL 後：Neon Console → SQL Editor → 貼上 → Run（或 python aws/run_once.py "python scripts/migrate.py" 那種 VPC 內路徑）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql as psql

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

argv = sys.argv[1:]
table = next((a.split("=", 1)[1] for a in argv if a.startswith("--table=")), "items")
out = next((a.split("=", 1)[1] for a in argv if a.startswith("--out=")), "")
mode = next((a.split("=", 1)[1] for a in argv if a.startswith("--mode=")), "upsert")
url_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--DATABASE_URL=")), "")
limit = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--limit=")), "0"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 和 scripts/migrate.py 一樣：本機開發的連線字串放在 .env.local（不進倉庫）
_dotenv = ROOT / ".env.local"
if _dotenv.exists():
    from dotenv import load_dotenv

    load_dotenv(_dotenv)


def conninfo() -> str:
    url = url_arg or os.environ.get("MIGRATE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL 沒有設定（讀 .env.local 或加 --DATABASE_URL='postgresql://...'）"
        )
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=disable"      # 本機 docker 不需要 SSL；連外部時由連線字串自己決定
    return url


def main() -> int:
    try:
        conn = psycopg.connect(conninfo(), connect_timeout=10, autocommit=True)
    except psycopg.OperationalError as err:
        # .env.local 寫的是 docker network 的别名 postgres17；從 host 跑解析不到，自動退回 localhost
        url = conninfo()
        if "postgres17" in url:
            try:
                conn = psycopg.connect(url.replace("postgres17", "localhost"),
                                       connect_timeout=10, autocommit=True)
                print("（postgres17 解析失敗，改用 localhost）", file=sys.stderr)
            except psycopg.OperationalError:
                print(f"❌ 連不到資料庫：{err}", file=sys.stderr)
                print("   本機從 host 跑請用 localhost（postgres17 只在 docker network 內解析得到）", file=sys.stderr)
                return 1
        else:
            print(f"❌ 連不到資料庫：{err}", file=sys.stderr)
            return 1
    try:
        cols = [r[0] for r in conn.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' and table_name = %s order by ordinal_position",
            (table,),
        ).fetchall()]
        if not cols:
            raise SystemExit(f"找不到表 {table}（這個 DB 裡沒有）")

        query = f"select {', '.join(cols)} from {table} order by id"
        if limit:
            query += f" limit {limit}"
        rows = conn.execute(query).fetchall()

        header = [
            f"-- {table} 資料同步（來源 DB：{conn.info.dbname} @ {conn.info.host}）",
            f"-- 產生時間：{datetime.now(timezone.utc).isoformat()}｜{len(rows)} 列｜mode={mode}",
            f"-- 適用：Neon（正式區 awsLucaPysn）SQL Editor 或 VPC 內 one-shot task",
            "BEGIN;",
        ]
        on_conflict = (
            "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
            if mode == "upsert" else "DO NOTHING"
        )
        lines = list(header)
        for row in rows:
            values = ", ".join(psql.Literal(v).as_string(conn) for v in row)
            lines.append(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({values}) "
                f"ON CONFLICT (id) {on_conflict};"
            )
        lines.append("COMMIT;")
        text = "\n".join(lines) + "\n"

        if out:
            Path(out).write_text(text, encoding="utf-8")
            print(f"✔ 寫入 {out}（{len(rows)} 列）")
        else:
            print(text, end="")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
