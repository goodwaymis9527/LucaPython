r"""aws_common.py — 部署腳本共用的基礎（boto3 版，對應 Node 版 aws/aws-common.js）

憑據來源（boto3 的預設解析順序）：
  1) 環境變數 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY（可加 AWS_PROFILE）
  2) C:\Users\g01943\.aws\credentials（ini）與 C:\Users\g01943\.aws\config
     [default]
     aws_access_key_id = ...
     aws_secret_access_key = ...
     region = ap-southeast-2

帳號預設 927678863269（環境變數 LUCA_PYTHON_ACCOUNT 可覆蓋）；whoami() 會比對憑據。
State 檔與 Node 版同格式，但獨立放在 LucaPython\.aws-state\，不會與 ..\LucaAWS 互相覆蓋。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
PROJECT = os.environ.get("LUCA_PYTHON_PROJECT", "luca-python")
ACCOUNT = os.environ.get("LUCA_PYTHON_ACCOUNT", "927678863269")
PORT = int(os.environ.get("PORT", "8000"))

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DIR = ROOT / ".aws-state"
STATE_FILE = STATE_DIR / "config.json"


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def client(service: str):
    return boto3.client(service, region_name=REGION)


def is_err_type(err, code: str) -> bool:
    """boto3 的異常碼在 err.response['Error']['Code']。

    兩邊都要正規化：
      - EC2 寫法是 InvalidGroup.Duplicate（有句點）
      - IAM 的碼是 NoSuchEntity / EntityAlreadyExists，但例外「類別名」是 ...Exception
    """
    def norm(value: str) -> str:
        value = value.replace(".", "")
        if value.endswith("Exception"):
            value = value[:-9]
        return value

    try:
        got = (err.response.get("Error", {}) or {}).get("Code", "") or ""
    except AttributeError:
        got = getattr(err, "code", "") or ""
    return norm(got) == norm(code)


def arn(kind: str, resource: str, account: str | None = None) -> str:
    acct = account or os.environ.get("LUCA_PYTHON_ACCOUNT") or ACCOUNT
    return f"arn:aws:{kind}:{REGION}:{acct}:{resource}"


def whoami() -> str:
    ident = client("sts").get_caller_identity()
    acct = ident["Account"]
    if acct != ACCOUNT:
        print(f"注意：憑據屬於帳號 {acct}，腳本預設 ACCOUNT = {ACCOUNT}")
        print(f"      要改預設值請編輯 aws/aws_common.py（或設環境變數 LUCA_PYTHON_ACCOUNT）")
    else:
        print(f"憑據帳號 {acct} / 區域 {REGION} / 專案 {PROJECT}")
    print(f"      userId: {ident['UserId']}")
    return acct


def new_state() -> dict:
    ensure_state_dir()
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("account", ACCOUNT)
        return state
    return {"region": REGION, "project": PROJECT, "account": ACCOUNT}


def read_state() -> dict:
    ensure_state_dir()
    if not STATE_FILE.exists():
        raise SystemExit(".aws-state/config.json 不存在。先跑一次 python aws/bootstrap.py（或 aws/rds.py）")
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    ensure_state_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"寫入 {STATE_FILE}")


def build_task_definition(state: dict, image_uri: str) -> dict:
    """task definition 模板：JSON 欄位直接對應 boto3 register_task_definition 的輸入。"""
    tpl = json.loads((HERE / "task-definition.json").read_text(encoding="utf-8"))
    container = tpl["containerDefinitions"][0]

    container["image"] = image_uri
    tpl["executionRoleArn"] = state.get("executionRoleArn")
    tpl["taskRoleArn"] = state.get("taskRoleArn")

    # DATABASE_URL 的兩種給法：
    #   1) dbSecretArn（推薦）→ Secrets Manager，task definition 內只寫 ARN，不含密碼
    #   2) databaseUrl（不推薦）→ 明文寫在 environment；只在私人 repo / 短期測試用
    if state.get("dbSecretArn"):
        container["secrets"] = [{"name": "DATABASE_URL", "valueFrom": state["dbSecretArn"]}]
    elif state.get("databaseUrl"):
        print("警告：DATABASE_URL 明文寫在 task definition。正式區建議改用 dbSecretArn（Secrets Manager）")
        container["environment"].append({"name": "DATABASE_URL", "value": state["databaseUrl"]})
    else:
        print("警告：state 裡沒有 dbSecretArn / databaseUrl，任務啟動時 DATABASE_URL 會缺失")

    return tpl


def call(svc: str, action: str, **params):
    """client(service).action(**params) 的短寫法，讓 status/exec 這類腳本比较短。

    第一個參數改名 svc：否則 `call("ecs", "list_tasks", service=...)` 會和它撞名
    （TypeError: call() got multiple values for argument 'service'）。
    """
    return getattr(client(svc), action)(**params)


def print_c(msg: str) -> None:
    print(msg)


def print_h(msg: str) -> None:
    print(f"\n{msg}")


def get_name(state: dict | None = None) -> str:
    return (state or read_state()).get("project") or PROJECT


# 習慣上叫 load_state()（對應 Node 版的 readState()）
load_state = read_state
