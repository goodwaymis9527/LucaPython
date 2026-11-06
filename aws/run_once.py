r"""run_once.py — 用同一個 task definition 跑一個「一次性」任務，跑完就停，輸出從 CloudWatch logs 讀

為什麼需要：ECS Exec（aws/exec.py）的 boto3 只支援 interactive，輸出要靠 Session Manager plugin；
本機沒裝 plugin，所以改用 RunTask + containerOverrides 這條路：輸出直接進 log group，用 boto3 就能讀。

需要 IAM：ecs:RunTask（policy 已補）、ecs:DescribeTasks、logs:DescribeLogStreams、logs:GetLogEvents

用法：
  python aws/run_once.py "python scripts/migrate.py"
  python aws/run_once.py "python -c \"import os; print(os.environ.get('DATABASE_URL'))\""
  python aws/run_once.py "python scripts/healthcheck.py" --stop   # 跑完把任務停掉
"""
import shlex
import sys
import time

from aws_common import call, get_name, load_state, print_c, print_h

POLL_SECONDS = 5
MAX_POLLS = 60  # 30 分鐘上限（Fargate 起任務約 30–60 秒）


def print_logs(state: dict, stream_name: str, tail: int) -> None:
    group = state.get("logGroup") or f"/ecs/{get_name(state)}"
    events = call("logs", "get_log_events", logGroupName=group, logStreamName=stream_name, limit=tail).get("events", [])
    print_h(f"CloudWatch logs（stream {stream_name}）")
    for e in events:
        print_c(f"  {e.get('message', '').rstrip()}")


def main() -> int:
    state = load_state()
    args = [a for a in sys.argv[1:] if a not in ("--stop",)]
    command = " ".join(args)
    if not command:
        print_c('usage: python aws/run_once.py "<command>" [--stop]')
        return 1

    if not state.get("taskFamily"):
        print_c("state 裡沒有 taskFamily；先跑 python aws/bootstrap.py")
        return 1

    task = call("ecs", "run_task",
                cluster=state["cluster"],
                taskDefinition=f"{state['taskFamily']}:{state['taskRevision']}",
                launchType="FARGATE",
                overrides={"containerOverrides": [{
                    "name": get_name(state),
                    # shlex 才能處理 `python -c "import os; print(...)"` 這種帶引號的參數
                    "command": shlex.split(command),
                }]},
                networkConfiguration={"awsvpcConfiguration": {
                    "subnets": state["subnets"],
                    "securityGroups": [state["taskSgId"]],
                    "assignPublicIp": "ENABLED",
                }},
                )["task"]
    print_c(f"task: {task['taskArn']}")

    last = None
    for _ in range(MAX_POLLS):
        t = call("ecs", "describe_tasks", cluster=state["cluster"], tasks=[task["taskArn"]])["tasks"][0]
        last = t
        if t["lastStatus"] in ("STOPPED", "STOPPED_STAGE", "FAILED"):
            break
        time.sleep(POLL_SECONDS)

    print_h("Result")
    print_c(f"  status   : {last['lastStatus']}")
    if last.get("stopCode"):
        print_c(f"  stopCode : {last['stopCode']}")
    container = (last.get("containers") or [{}])[0]
    if container.get("exitCode") is not None:   # exitCode 在 containers[] 上，Task 本身沒有這個欄位
        print_c(f"  exitCode : {container['exitCode']}")

    stream = container.get("logStreamName")
    if stream:
        print_logs(state, stream, 100)
    else:
        print_c("  （這個任務還沒產生 log stream）")

    code = container.get("exitCode")
    if "--stop" in sys.argv and last["lastStatus"] not in ("STOPPED", "FAILED"):
        call("ecs", "stop_task", cluster=state["cluster"], task=last["taskArn"])
        print_c("  （任務已停）")
    else:
        print_c("提示：一次性任務會計費到被停掉為止（Fargate 以 15 分鐘為一個計費單位）；"
               "跑完请加 --stop 或 python aws/run_once.py 後手動 stop_task")

    return 0 if code in (0, None) else 1


if __name__ == "__main__":
    sys.exit(main())
