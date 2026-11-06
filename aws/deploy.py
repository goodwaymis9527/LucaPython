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
  python aws/deploy.py --no-wait            （不輪詢 rollout，update-service 後就返回）
  python aws/deploy.py --skip-health         （不打 ALB 健康檢查）

結束會印「=== 發佈結果 ===」：exit code 0 = 成功、1 = 失敗（rollout 沒到 COMPLETED 或健康檢查不過），
deploy.bat / aws/ops.py 就是靠這個 exit code 顯示 ✔/✘。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
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

# rolling 部署是非同步的：新任務 healthy 後才停舊任務，所以要輪詢到 COMPLETED 才算真的上線
# （Fargate 起任務約 30–60 秒，這裡最多等 5 分鐘；--no-wait 可跳過）
ok = False
rollout = ""
waited = "--no-wait" not in argv
if not waited:
    print("=== 5) 跳過輪詢（--no-wait）：只送 update-service，結果自行確認 ===")
else:
    print("=== 5) wait for the rolling deployment ===")
    for attempt in range(30):
        svc = ecs.describe_services(cluster=state["cluster"], services=[state["service"]])["services"][0]
        deps = [(d["rolloutState"], d["taskDefinition"].rsplit("/", 1)[-1],
                 d["runningCount"], d["desiredCount"]) for d in svc["deployments"]]
        print(f"  {attempt + 1:2d} {deps}")
        if len(deps) == 1 and deps[0][0] == "COMPLETED" and deps[0][2] == deps[0][3]:
            ok, rollout = True, deps[0][0]
            break
        time.sleep(10)
    else:
        print("  ✘ 5 分鐘內沒有 COMPLETED（可能新任務不 healthy 或舊任務還在）")

# 6) rollout COMPLETED 只表示「任務換掉了」，應用真的起來要打 ALB 才算數
health = None
if ok and state.get("albDns") and "--skip-health" not in argv:
    print("=== 6) health check ===")
    url = f"http://{state['albDns']}/api/health"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            health = resp.status == 200 and bool(body.get("ok"))
            if health:
                print(f"  ✔ {url} -> {resp.status} ok={body.get('ok')} env={body.get('db', {}).get('env')}")
                break
            print(f"  ✘ {url} -> {resp.status} error={body.get('error')}")
        except Exception as err:
            print(f"  ✘ {url} 打不通（{err}）")
        if attempt < 2:
            time.sleep(5)   # 新任務剛起、ALB 目標群可能還在 registration，重試一次

state["lastDeployOk"] = None if not waited else ok
state["lastDeployHealth"] = health
save_state(state)

print("")
print("=== 發佈結果 ===")
if not waited:
    print("  － 未輪詢（--no-wait）：update-service 已送出，結果自行確認")
else:
    print(f"  {'✔ 成功（rollout COMPLETED）' if ok else '✘ 失敗（rollout ' + (rollout or 'TIMEOUT') + '）'}")
print(f"  task definition: {td_ref}")
print(f"  image:           {state['lastImage']}")
if health is not None:
    print(f"  health check:    {'✔ 通過' if health else '✘ 不通過'}")
if state.get("albDns"):
    print(f"  看健康狀態：http://{state['albDns']}/api/health")
if not ok or health is False:
    print("  看為什麼失敗：python aws/status.py --logs 20")

# exit code 給 aws/ops.py / deploy.bat 判斷成功與否：0 = 成功、1 = 失敗
raise SystemExit(1 if (waited and not ok) or health is False else 0)
