#!/usr/bin/env python3
"""neon.py — 正式區資料庫（Neon）對應表

設計決定（與本機 Docker 的 awsLucaPysn 同名，方便兩邊比對）：
- 本機：postgres17 容器，資料庫 awsLucaPysn
- 正式：獨立 Neon 專案 luca-python，資料庫也叫 awsLucaPysn
  （不共用 luca-web-vercel：共用 compute 會互相影響 autoscaling / 連線數 / Quota）

需要 NEON_API_KEY（環境變數或 gitignored 的 `.env`；Console → Settings → API Keys → Add API key）。
本機連 Neon 的 5432 被封，所以這裡只走 API（443），不會實際連 DB。

執行：
  $env:NEON_API_KEY="..."        # 或 .env 裡寫 NEON_API_KEY=...
  python aws/neon.py                      # 列出專案 / 資料庫 / 連線字串
  python aws/neon.py --project=luca-python --db=awsLucaPysn
寫完後跑 python aws/secret.py 把連線字串放進 Secrets Manager。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_FILE = ROOT / ".aws-state" / "config.json"
NEON_FILE = ROOT / ".aws-state" / "neon.json"

argv = sys.argv[1:]
project_name = next((a.split("=", 1)[1] for a in argv if a.startswith("--project=")), "luca-python")
db_name = next((a.split("=", 1)[1] for a in argv if a.startswith("--db=")), "awsLucaPysn")

def env_or_dotenv(key: str) -> str:
    """環境變數優先，否則讀 gitignored 的 .env（NEON_API_KEY 屬秘密，不能進 repo）。

    和 app/config.py 的順序一致：真的環境變數 > .env > 沒有。
    """
    value = os.environ.get(key, "")
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue
            name, _, val = line.partition("=")
            if name.strip() == key:
                return val.strip().strip('"').strip("'")
    return ""


api_key = env_or_dotenv("NEON_API_KEY")
if not api_key:
    print("NEON_API_KEY 沒有設定（環境變數或 .env 都要）。")
    print("  1) Neon Console → Settings → API Keys → Add API key")
    print("  2) 寫進 gitignored 的 .env（NEON_API_KEY=...）或 $env:NEON_API_KEY='...' 再跑 python aws/neon.py")
    print("  注意：key 只給這支腳本用（app 執行期不需要，執行期只有 Secrets Manager 的 DATABASE_URL）")
    if NEON_FILE.exists():
        print("\n（讀 .aws-state/neon.json 現存的紀錄）")
        print(json.dumps(json.loads(NEON_FILE.read_text(encoding="utf-8")), indent=2, ensure_ascii=False))
    raise SystemExit(0)


def api(path: str) -> dict:
    req = urllib.request.Request(
        f"https://console.neon.tech/api/v2{path}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


projects = api("/projects").get("projects", [])
match = next((p for p in projects if p["name"] == project_name), None)
if not match:
    print(f"Neon 裡沒有專案「{project_name}」。現有專案：")
    for p in projects:
        print(f"  - {p['name']} ({p['id']}) region={p['region_id']}")
    print(f"要先建專案：Neon Console → New Project → 名稱 {project_name}，資料庫 {db_name}")
    raise SystemExit(1)

print(f"Neon 專案 {match['name']}（{match['id']}）region={match['region_id']} pg={match['pg_version']}")

branches = api(f"/projects/{match['id']}/branches").get("branches", [])
for branch in branches:
    dbs = api(f"/projects/{match['id']}/branches/{branch['id']}/database").get("databases", [])
    names = [d["name"] for d in dbs]
    print(f"  branch {branch['name']}（{branch['id']}）databases={names}")
    if db_name not in names:
        print(f"    ⚠ 這個 branch 沒有 {db_name}；用 Neon Console 或 API 建：owner = neondb_owner")
        continue
    conn = api(f"/projects/{match['id']}/branches/{branch['id']}/connection_string?dbname={db_name}")
    url = conn.get("connection_string", "")
    print(f"    connection string: {url}")

    record = {
        "project": match["name"],
        "projectId": match["id"],
        "branch": branch["name"],
        "branchId": branch["id"],
        "database": db_name,
        "region": match["region_id"],
        "connectionUrl": url,
        "note": "正式區 task 在 VPC 內可出 5432；本機 5432 被封，所以本機測 Neon 一定失敗",
    }
    ROOT.joinpath(".aws-state").mkdir(parents=True, exist_ok=True)
    NEON_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"    寫入 {NEON_FILE}")

print("")
print("下一步：python aws/secret.py")
