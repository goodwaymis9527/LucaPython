r"""exec.py — 在執行中的 Fargate 任務內跑一次命令（Fargate 沒有 shell，這是唯一能「看進容器內部」的方法）

需要：
  1) service 的 enableExecuteCommand = true（bootstrap.py 已開啟）
  2) task role 有 ssmmessages:CreateControlChannel / CreateDataChannel / OpenControlChannel / OpenDataChannel
  3) 執行這支腳本的 IAM 使用者有 ecs:ExecuteCommand

用法：
  python aws/exec.py "python scripts/healthcheck.py"

限制（實測）：boto3 的 ExecuteCommand 只支援 interactive=True
  （interactive=False 會被拒：InvalidParameterException: Interactive is the only mode supported currently.）
  所以 boto3 只能「建立 session」，stdout 要由 Session Manager plugin 串起來：
    aws ecs execute-command --cluster luca-python --task <taskArn> --container luca-python \
      --command "python scripts/healthcheck.py" --interactive
  本機沒裝 plugin，也没 AWS CLI v2 → 要用這段輸出請改用 aws/run_once.py（RunTask + CloudWatch logs）。
"""
import sys

from aws_common import call, get_name, load_state, print_c, print_h


def main() -> int:
    state = load_state()
    command = " ".join(sys.argv[1:])
    if not command:
        print_c('usage: python aws/exec.py "<command to run inside the container>"')
        return 1

    arns = call("ecs", "list_tasks", cluster=state["cluster"], serviceName=get_name()).get("taskArns") or []
    if not arns:
        print_c("no task under the service")
        return 1

    task = call("ecs", "describe_tasks", cluster=state["cluster"], tasks=arns)["tasks"][0]
    print_c(f"task: {task['taskArn']}")

    out = call("ecs", "execute_command",
               cluster=state["cluster"], task=task["taskArn"], command=command, interactive=True)
    session = out.get("session") or {}
    print_h("Session created")
    print_c(f"sessionId : {session.get('sessionId')}")
    print_c(f"container : {out.get('containerName')}")

    print_h("How to see the output")
    print_c("boto3 這邊只拿到 session，不會拿到 stdout。要用輸出請：")
    print_c(f"  aws ecs execute-command --cluster {state['cluster']} --task {task['taskArn']} \\")
    print_c(f"    --container {out.get('containerName')} --command \"{command}\" --interactive")
    print_c("（需要 AWS CLI v2 + session-manager-plugin；本機目前都沒有）")
    print_c("不想裝 plugin：python aws/run_once.py \"python scripts/healthcheck.py\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
