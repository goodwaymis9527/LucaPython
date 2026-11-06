r"""status.py — 顯示 luca-python 的 AWS 狀態（對應 Node 版 aws/status.ps1）

用法：
  python aws/status.py
"""
import json
import sys

from aws_common import call, get_name, load_state, print_c, print_h


def main():
    state = load_state()
    name = get_name(state)
    argv = sys.argv[1:]
    a = next((x for x in argv if x.startswith("--logs=")), None)
    tail = int(a.split("=", 1)[1]) if a else 0
    print_c(f"Resource name: {name}")
    print_c(f"Region:        {state['region']}")

    print_h("ECS Service")
    svc = call("ecs", "describe_service", serviceName=name)
    print_c(f"  Service:   {svc['serviceName']} ({svc['serviceArn']})")
    print_c(f"  Task count:{svc['desiredCount']} (running {svc['runningCount']})")
    print_c(f"  Target:    {svc['loadBalancers'][0]['targetGroupArn']}")

    tds = call("ecs", "list_task_definitions", family=name, maxResults=1)
    td = call("ecs", "describe_task_definition", taskDefinitionArn=tds["taskDefinitions"][0]["taskDefinitionArn"])
    print_c(f"  Task def:  {td['taskDefinitionArn']}")
    print_c(f"  CPU/Mem:   {td['cpu']}/{td['memory']} | {td['networkMode']}")
    print_c(f"  Image:     {td['containerDefinitions'][0]['image']}")
    print_c(f"  Env:       {json.dumps(td['containerDefinitions'][0].get('environment', {}), ensure_ascii=False)}")
    print_c(f"  Secrets:   {json.dumps(td['containerDefinitions'][0].get('secrets', []), ensure_ascii=False)}")

    print_h("ECR")
    repo = call("ecr", "describe_repositories", repositoryNames=[name])["repositories"][0]
    print_c(f"  {repo['repositoryUri']} (created {repo['createdAt']})")

    print_h("ALB")
    lb = call("elbv2", "describe_load_balancers", Names=[name])["LoadBalancers"][0]
    url = (f"http://{lb['DNSName']}" if lb["Type"] == "application"
           else f"https://{lb['DNSName']}")
    print_c(f"  {lb['LoadBalancerArn']}")
    print_c(f"  DNS:       {url}")
    print_c(f"  Scheme:    {lb['Scheme']} | Type: {lb['Type']}")

    tg = call("elbv2", "describe_target_groups", Names=[name])["TargetGroups"][0]
    print_c(f"  Target group: {tg['TargetGroupArn']} (port {tg['Port']}, health_path {tg['HealthCheckPath']})")

    print_h("Secrets Manager")
    sm = call("secretsmanager", "get_secret_value", SecretId=name)
    print_c(f"  {sm['ARN']} (length {len(sm['SecretString'])})")

    print_h("CloudWatch Logs")
    logs = call("logs", "describe_log_groups", logGroupNamePrefix=f"/ecs/{name}")
    if not logs.get("logGroups"):
        print_c("  (no log group yet — created on first run)")
    else:
        for g in logs["logGroups"]:
            print_c(f"  {g['logGroupName']}")
            if tail:
                streams = call("logs", "describe_log_streams", logGroupName=g["logGroupName"], limit=5).get("logStreams", [])
                for s in streams:
                    events = call("logs", "get_log_events", logGroupName=g["logGroupName"],
                                  logStreamName=s["logStreamName"], limit=tail).get("events", [])
                    print_c(f"  stream {s['logStreamName']}（最後 {len(events)} 行）")
                    for e in events:
                        print_c(f"    {e.get('message', '').rstrip()}")

    print_h("IAM Role")
    role = call("iam", "get_role", RoleName=f"{name}-execution")
    print_c(f"  {role['Role']['Arn']}")


if __name__ == "__main__":
    sys.exit(main())
