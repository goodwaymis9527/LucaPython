#!/usr/bin/env python3
"""secret.py — 把 DATABASE_URL 放 Secrets Manager，dbSecretArn 寫進 .aws-state/config.json

來源優先序：--url=... > 環境變數 DATABASE_URL > .aws-state/neon.json > .env.local

執行：
  python aws/secret.py
  python aws/secret.py --url="postgresql://neondb_owner:***@ep-xxx-pooler.ap-southeast-2.aws.neon.tech/awsLucaPysn?sslmode=require"

跑完要再部署一次才會把 secrets[] 套進 task definition：
  python aws/deploy.py --skip-build --skip-push
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from aws_common import PROJECT, client, read_state, save_state, whoami

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = read_state()

argv = sys.argv[1:]
url = next((a.split("=", 1)[1] for a in argv if a.startswith("--url=")), "")

if not url:
    import os

    url = os.environ.get("DATABASE_URL", "")

if not url:
    neon_file = ROOT / ".aws-state" / "neon.json"
    if neon_file.exists():
        url = json.loads(neon_file.read_text(encoding="utf-8")).get("connectionUrl", "")

if not url:
    env_local = ROOT / ".env.local"
    if env_local.exists():
        for line in env_local.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
                break

if not url:
    raise SystemExit(
        "DATABASE_URL 找不到。用 --url=... 或先把 NEON_API_KEY 設好跑 python aws/neon.py"
    )

state_account = whoami()
sm = client("secretsmanager")
secret_name = f"{PROJECT}/database"

try:
    sm.create_secret(Name=secret_name, SecretString=url, description="luca-python DATABASE_URL (Neon)")
    print(f"建立 secret: {secret_name}")
except Exception as err:
    # 已存在時 boto3 丟 SecretExistsException（Code = "SecretExistsException"）
    code = ((getattr(err, "response", {}) or {}).get("Error", {}) or {}).get("Code", "")
    if code != "SecretExistsException" and "Exists" not in str(err):
        raise
    sm.put_secret_value(SecretId=secret_name, SecretString=url)
    print(f"更新 secret: {secret_name}")

arn = (sm.get_secret_value(SecretId=secret_name).get("ARN") or
       f"arn:aws:secretsmanager:{STATE['region']}:{state_account}:secret:{secret_name}")

STATE["dbSecretArn"] = arn
save_state(STATE)
print("")
print(f"dbSecretArn = {arn}")
print("下一步：python aws/deploy.py --skip-build --skip-push（把 secrets[] 套進 task definition）")
