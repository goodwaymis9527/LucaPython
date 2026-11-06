"""測試：本機能不能連到 Neon（5432）。

對應「本機出站 5432 被封」的判斷：如果這裡失敗，表示本機開發只能走 Docker 的 postgres17，
正式區 task 在 VPC 內才有 5432 出路。執行：
  python 20261106_LucaPython架構_OpenCode/test_neon_conn.py
"""
import os
import sys

import psycopg

URL = os.environ.get("NEON_URL", "postgresql://neondb_owner:npg_Ee6Ib2XMNydL@ep-calm-glitter-a787dvai-pooler.ap-southeast-2.aws.neon.tech/awsLucaPysn?channel_binding=require&sslmode=require")

try:
    # prepare_threshold=0：不產生 prepared statement（Neon 的 transaction pooler 不支援）
    with psycopg.connect(URL, connect_timeout=10, prepare_threshold=0) as conn:
        row = conn.execute("SELECT current_database(), version()").fetchone()
        print("✔ 本機可連 Neon")
        print("  database:", row[0])
        print("  server  :", row[1])
except Exception as err:
    print("✘ 本機連不上 Neon（預期：5432 被封）")
    print(f"  {type(err).__name__}: {err}")
    sys.exit(1)
