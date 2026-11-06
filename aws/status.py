r"""status.py — 顯示 luca-python 的 AWS 狀態（對應 Node 版 aws/status.ps1）

用法：
  python aws/status.py
  python aws/status.py --logs=20        # 或 --logs 20
  python aws/status.py --json           # 只印 state 檔

每一節都獨立 try：資源還沒建（例如 cluster）時不會整支掛掉，只把那節標成讀不到。
"""
import json
import sys

from aws_common import call, get_name, load_state, print_c, print_h


def section(title: str, fn) -> None:
    print_h(title)
    try:
        fn()
    except Exception as err:
        print_c(f"  （讀不到：{type(err).__name__}: {str(err).splitlines()[0][:140]}）")


def main() -> int:
    state = load_state()
    name = get_name(state)
    argv = sys.argv[1:]
    a = next((x for x in argv if x.startswith("--logs")), None)
    # 同時接受 `--logs=20` 與 `--logs 20`（deploy.ps1 用的是後者）
    tail = int(a.split("=", 1)[1]) if "=" in (a or "") else (int(argv[argv.index(a) + 1]) if a else 0)

    if "--json" in argv:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    print_c(f"Resource name: {name}")
    print_c(f"Region:        {state['region']}")

    def show_service():
        services = [state.get("serviceArn") or name]
        svc = call("ecs", "describe_services", cluster=state.get("cluster") or name, services=services)["services"][0]
        print_c(f"  Service:   {svc['serviceName']} ({svc['serviceArn']})")
        print_c(f"  Task count:{svc['desiredCount']} (running {svc['runningCount']})")
        print_c(f"  Target:    {svc['loadBalancers'][0]['targetGroupArn']}")

    def show_task_def():
        # ListTaskDefinitions 只收 familyPrefix（沒有 family），回應的鍵是 taskDefinitionArns
        tds = call("ecs", "list_task_definitions", familyPrefix=name, sort="DESC", maxResults=1).get("taskDefinitionArns") or []
        if not tds:
            print_c("  （還沒有 task definition revision）")
            return
        td = call("ecs", "describe_task_definition", taskDefinitionArn=tds[0])["taskDefinition"]
        container = td["containerDefinitions"][0]
        print_c(f"  Task def:  {td['taskDefinitionArn']}")
        print_c(f"  CPU/Mem:   {td['cpu']}/{td['memory']} | {td['networkMode']}")
        print_c(f"  Image:     {container['image']}")
        print_c(f"  Env:       {json.dumps(container.get('environment', []), ensure_ascii=False)}")
        print_c(f"  Secrets:   {json.dumps(container.get('secrets', []), ensure_ascii=False)}")

    def show_ecr():
        repo = call("ecr", "describe_repositories", repositoryNames=[name])["repositories"][0]
        print_c(f"  {repo['repositoryUri']} (created {repo['createdAt']})")

    def show_alb():
        lb = call("elbv2", "describe_load_balancers", Names=[name])["LoadBalancers"][0]
        url = f"http://{lb['DNSName']}" if lb["Type"] == "application" else f"https://{lb['DNSName']}"
        print_c(f"  {lb['LoadBalancerArn']}")
        print_c(f"  DNS:       {url}")
        print_c(f"  Scheme:    {lb['Scheme']} | Type: {lb['Type']}")
        tg = call("elbv2", "describe_target_groups", Names=[name])["TargetGroups"][0]
        print_c(f"  Target group: {tg['TargetGroupArn']} (port {tg['Port']}, health_path {tg['HealthCheckPath']})")

    def show_secret():
        # secret 名稱是 luca-python/database，不是 luca-python
        sm = call("secretsmanager", "get_secret_value", SecretId=state.get("dbSecretArn") or f"{name}/database")
        print_c(f"  {sm['ARN']} (length {len(sm['SecretString'])})")

    def show_logs():
        logs = call("logs", "describe_log_groups", logGroupNamePrefix=f"/ecs/{name}")
        if not logs.get("logGroups"):
            print_c("  （no log group yet — created on first run）")
            return
        for g in logs["logGroups"]:
            print_c(f"  {g['logGroupName']}")
            if not tail:
                continue
            streams = call("logs", "describe_log_streams", logGroupName=g["logGroupName"], limit=5).get("logStreams", [])
            for s in streams:
                events = call("logs", "get_log_events", logGroupName=g["logGroupName"],
                              logStreamName=s["logStreamName"], limit=tail).get("events", [])
                print_c(f"  stream {s['logStreamName']}（最後 {len(events)} 行）")
                for e in events:
                    print_c(f"    {e.get('message', '').rstrip()}")

    def show_role():
        role = call("iam", "get_role", RoleName=f"{name}-execution")["Role"]
        print_c(f"  {role['Arn']}")

    section("ECS Service", show_service)
    section("Task definition", show_task_def)
    section("ECR", show_ecr)
    section("ALB", show_alb)
    section("Secrets Manager", show_secret)
    section("CloudWatch Logs", show_logs)
    section("IAM Role", show_role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
