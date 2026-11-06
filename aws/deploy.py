#!/usr/bin/env python3
"""deploy.py — 一鍵部署（boto3 版，不需要 AWS CLI）

流程：docker build → docker push（boto3 拿 ECR token）→ register 新 revision → update-service
第一次要先跑 aws/bootstrap.py（建 ECR / cluster / service 並產生 .aws-state/config.json）。

執行：
  python aws/deploy.py
  python aws/deploy.py --skip-build            （跳過 docker build）
  python aws/deploy.py --skip-build --skip-push （只改 task definition：沿用最後一次 push 的镜像）
  python aws/deploy.py --tag=dev
  python aws/deploy.py --image=<ecr-uri>:<tag>
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aws_common import PROJECT, build_task_definition, client, new_state, read_state, save_state, whoami

ROOT = Path(__file__).resolve().parent.parent
argv = sys.argv[1:]
skip_build = "--skip-build" in argv
skip_push = "--skip-push" in argv
tag = next((a.split("=")[1] for a in argv if a.startswith("--tag=")), "latest")
image_arg = next((a.split("=")[1] for a in argv if a.startswith("--image=")), "")

# dry-run 時 state 檔可能還不存在（bootstrap 沒跑過），用 new_state() 才不會被 read_state 擋下
state = new_state() if "--dry-run" in argv else read_state()

if "--dry-run" in argv:
    # 只組裝 task definition 並印出來（不會碰 AWS、不會 register）：M6 的驗收方式
    image = next((a.split("=", 1)[1] for a in argv if a.startswith("--image=")), None) \
        or state.get("lastImage") or f"{state.get('ecrUri', '<ecr-uri>')}:{tag}"
    print("=== dry-run：task definition（boto3 register_task_definition 的輸入）===")
    print(json.dumps(build_task_definition(state, image), indent=2, ensure_ascii=False))
    raise SystemExit(0)

if not state.get("ecrUri"):
    raise SystemExit("config.json 缺少 ecrUri；先跑 python aws/bootstrap.py")
if not state.get("cluster"):
    raise SystemExit("config.json 缺少 cluster；先跑 python aws/bootstrap.py")
if not state.get("service"):
    raise SystemExit("config.json 缺少 service；先跑 python aws/deploy.py 前先 bootstrap")

if "--print-ecr-uri" in argv:
    # 給 deploy.ps1 用的純輸出（只印 URI，方便 docker login / docker tag）
    print(state["ecrUri"])
    raise SystemExit(0)

ecr = client("ecr")
ecs = client("ecs")

state["account"] = whoami()


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} 失敗（exit {proc.returncode}）")


print(f"專案 {ROOT}")
print(f"ECR    {state['ecrUri']}")

# 1) build
if skip_build:
    print("=== 1) 跳過 docker build（用現有镜像）===")
else:
    print("=== 1) docker build ===")
    run(["docker", "build", "-t", f"{PROJECT}:{tag}", "."])

# 2) push（或沿用既有镜像）
if skip_push:
    image_ref = image_arg or state.get("lastImage") or f"{state['ecrUri']}:{tag}"
    print(f"=== 2) 跳過 build/push，沿用既有镜像：{image_ref} ===")
else:
    print("=== 2) push 到 ECR ===")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    registry_host = state["ecrUri"].split("/")[0]

    auth = ecr.get_authorization_token()["authorizationData"]
    if not auth:
        raise SystemExit("拿不到 ECR authorizationToken（檢查憑據與 ecr:GetAuthorizationToken 權限）")
    decoded = base64.b64decode(auth[0]["authorizationToken"]).decode("utf-8")
    password = decoded[4:] if decoded.startswith("AWS:") else decoded

    login = subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry_host],
        input=password.encode("utf-8"),
    )
    if login.returncode != 0:
        raise SystemExit("docker login 失敗")

    for target in (f"{state['ecrUri']}:{tag}", f"{state['ecrUri']}:{stamp}"):
        run(["docker", "tag", f"{PROJECT}:{tag}", target])
        run(["docker", "push", target])

    image_ref = f"{state['ecrUri']}:{stamp}"

# 3) 新的 task definition revision
print("=== 3) register task definition revision ===")
td_input = build_task_definition(state, image_ref)
out = ecs.register_task_definition(**td_input)["taskDefinition"]
state["taskFamily"] = out["family"]
state["taskRevision"] = out["revision"]
state["lastImage"] = image_ref

# 4) service 用新 revision 並強制換任務（rolling：新任務 healthy 後才停舊任務）
print("=== 4) update service ===")
td_ref = f"{state['taskFamily']}:{state['taskRevision']}"
ecs.update_service(cluster=state["cluster"], service=state["service"], taskDefinition=td_ref, forceNewDeployment=True)
state["lastDeploymentAt"] = datetime.now(timezone.utc).isoformat()
save_state(state)

print("")
print("=== 部署完成 ===")
print(f"task definition: {td_ref}")
print(f"image:           {state['lastImage']}")
if state.get("albDns"):
    print(f"看健康狀態：http://{state['albDns']}/api/health")
print("看狀態：python aws/status.py --logs=20")
