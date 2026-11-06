r"""exec.py — 在執行中的 Fargate 任務內跑一次命令（Fargate 沒有 shell，這是唯一能「看進容器內部」的方法）

需要：
  1) service 的 enableExecuteCommand = true（bootstrap.py 已開啟）
  2) task role 有 ssmmessages:CreateControlChannel / CreateDataChannel / ListsChannels
  3) 執行這支腳本的 IAM 使用者有 ssm:SendCommand（ECS Exec 是建立在 SSM 上）

用法：
  python aws/exec.py "python scripts/healthcheck.py"
  python aws/exec.py "python -c \"import os; print(os.environ.get('DATABASE_URL'))\""

輸出可能含密碼：僅供一次性診斷，不要貼進倉庫。
"""
import sys

from aws_common import call, get_name, load_state, print_c, print_h


def main():
    state = load_state()
    command = " ".join(sys.argv[1:])
    if not command:
        print_c('usage: python aws/exec.py "<command to run inside the container>"')
        return 1

    arns = call("ecs", "list_tasks", cluster=state["cluster"], service=get_name()).get("taskArns") or []
    if not arns:
        print_c("no task under the service")
        return 1

    task = call("ecs", "describe_tasks", cluster=state["cluster"], tasks=arns)["tasks"][0]
    print_c(f"task: {task['taskArn']}")

    out = call("ecs", "execute_command",
               cluster=state["cluster"], task=task["taskArn"], command=command, interactive=False)["executeCommandOutput"]
    print_h("Result")
    print_c(f"exitCode: {out.get('exitCode')}")
    if out.get("reason"):
        print_c(f"reason: {out['reason']}")
    if out.get("stdout"):
        print(out["stdout"], end="")
    if out.get("stderr"):
        print(out["stderr"], end="", file=sys.stderr)
    return 0 if out.get("exitCode") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
