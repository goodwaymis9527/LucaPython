r"""apply_sql.py — 執行 SQL（單句、或整份 .sql）

連到哪裡由 DATABASE_URL 決定：本機 → postgres17；容器內（VPC）→ Secrets Manager 的 Neon。
本機連不到 Neon 的 5432（實測 timeout），所以「套到正式區」要透過 VPC 內的一-shot 任務：

  python scripts/apply_sql.py --sql="SELECT count(*) AS n FROM items"        # 本機（讀 .env.local）
  python aws/run_once.py "python scripts/apply_sql.py --sql=..." --stop        # 正式區（容器內 → Neon）
  python aws/run_once.py "python scripts/apply_sql.py --b64=<base64>" --stop   # 整份檔（檔不在 image 內，所以用 base64 帶進去）

生成的 SQL 來自 scripts/dump_data.py（冪等 upsert），所以重複跑不會产生重複資料。
單句/逐句執行：Neon 的 pooler 是 transaction mode，逐句在一個交易內最穩。
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

argv = sys.argv[1:]
sql_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--sql=")), "")
b64_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--b64=")), "")
file_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--file=")), "")


def conninfo() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        dotenv = ROOT / ".env.local"
        if dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("#"):
                    continue
                name, _, val = line.partition("=")
                if name.strip() == "DATABASE_URL":
                    url = val.strip().strip('"').strip("'")
                    break
    if not url:
        raise SystemExit("DATABASE_URL 沒有設定（.env.local 或環境變數）")
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=disable"
    return url


def statements() -> list[str]:
    raw = ""
    if b64_arg:
        raw = base64.b64decode(b64_arg).decode("utf-8")
    elif file_arg:
        raw = Path(file_arg).read_text(encoding="utf-8")
    elif sql_arg:
        raw = sql_arg
    else:
        raise SystemExit('要用 --sql="..."、--b64=<base64> 或 --file=<path>')

    kept = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in kept.split(";") if s.strip() and s.strip().upper() not in ("BEGIN", "COMMIT")]


def connect() -> psycopg.Connection:
    """一個交易內跑完，成功才 commit。本機從 host 跑時 postgres17 這個名字解析不到，自動退回 localhost。"""
    try:
        return psycopg.connect(conninfo(), connect_timeout=10)
    except psycopg.OperationalError as err:
        url = conninfo()
        if "postgres17" in url:
            try:
                print("（postgres17 解析失敗，改用 localhost）", file=sys.stderr)
                return psycopg.connect(url.replace("postgres17", "localhost"), connect_timeout=10)
            except psycopg.OperationalError:
                raise err
        raise


def main() -> int:
    stmts = statements()
    conn = connect()
    try:
        print(f"target: {conn.info.dbname} @ {conn.info.host}｜{len(stmts)} 句")
        for sql in stmts:
            cur = conn.execute(sql)
            print(f"  ✔ {cur.rowcount} 列｜{' '.join(sql.split())[:70]}")
        conn.commit()
        print("✔ commit 完成")
        return 0
    except Exception as err:
        conn.rollback()
        print(f"❌  rollback（沒有 commit）：{err}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
