r"""run_once.py — 用同一個 task definition 跑一個「一次性」任務，跑完就停，輸出從 CloudWatch logs 讀

為什麼需要：ECS Exec（aws/exec.py）的 boto3 只支援 interactive，輸出要靠 Session Manager plugin；
本機沒裝 plugin，所以改用 RunTask + containerOverrides 這條路：輸出直接進 log group，用 boto3 就能讀。

需要 IAM：ecs:RunTask、ecs:StopTask、ecs:DescribeTasks、logs:DescribeLogStreams、logs:GetLogEvents

用法：
  python aws/run_once.py "python scripts/migrate.py"
  python aws/run_once.py "python scripts/migrate.py" --stop
  python aws/run_once.py "python -c \"import os; print(os.environ.get('APP_ENV'))\"" --stop

兩個實測到的形狀（boto3）：
  1) RunTask 的回應是 tasks[] / failures[]，沒有 `task` 鍵。
  2) Task 的 containers[] 沒有 logStreamName；stream 名稱要自己拼：
     <awslogs-stream-prefix>/<family>/<taskId>（AWS 的 awslogs 命名規則）。
"""
import shlex
import sys
import time

from aws_common import call, get_name, load_state, print_c, print_h

POLL_SECONDS = 5
MAX_POLLS = 60  # 30 分鐘上限（Fargate 起任務約 30–60 秒）
TERMINAL = ("STOPPED", "FAILED")


def resolve_log_stream(state: dict, task: dict) -> tuple[str, str]:
    td = call("ecs", "describe_task_definition", taskDefinition=task["taskDefinitionArn"])["taskDefinition"]
    opts = ((td["containerDefinitions"][0].get("logConfiguration") or {}).get("options") or {})
    group = opts.get("awslogs-group") or f"/ecs/{get_name(state)}"
    prefix = opts.get("awslogs-stream-prefix", "app")
    task_id = task["taskArn"].rsplit("/", 1)[-1]
    return group, f"{prefix}/{td['family']}/{task_id}"


def print_logs(state: dict, group: str, stream: str, tail: int) -> None:
    # 任務剛停時 log driver 可能還沒 flush 完，所以 retries 三次
    for attempt in range(3):
        try:
            events = call("logs", "get_log_events", logGroupName=group,
                          logStreamName=stream, limit=500).get("events", [])
        except Exception as err:  # ResourceNotFoundException / 尚未寫入
            if attempt == 2:
                print_c(f"  （讀 log 失敗：{type(err).__name__}: {str(err).splitlines()[0][:140]}）")
                return
            time.sleep(POLL_SECONDS)
            continue
        if events:
            break
        time.sleep(POLL_SECONDS)

    print_h(f"CloudWatch logs（stream {stream}）")
    for e in events[-tail:]:
        print_c(f"  {e.get('message', '').rstrip()}")
    if not events:
        print_c("  （這個 stream 還沒有事件）")


def main() -> int:
    state = load_state()
    args = [a for a in sys.argv[1:] if a != "--stop"]
    command = " ".join(args)
    if not command:
        print_c('usage: python aws/run_once.py "<command>" [--stop]')
        return 1

    if not state.get("taskFamily"):
        print_c("state 裡沒有 taskFamily；先跑 python aws/bootstrap.py")
        return 1

    td_ref = f"{state['taskFamily']}:{state['taskRevision']}" if state.get("taskRevision") else state["taskFamily"]
    result = call("ecs", "run_task",
                  cluster=state["cluster"],
                  taskDefinition=td_ref,
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
                  }})
    failures = result.get("failures") or []
    if failures:
        print_c(f"RunTask failure: {failures[0].get('reason')} — {failures[0].get('arn')}")
        return 1

    task = result["tasks"][0]
    print_c(f"task: {task['taskArn']}")

    last = None
    for _ in range(MAX_POLLS):
        last = call("ecs", "describe_tasks", cluster=state["cluster"], tasks=[task["taskArn"]])["tasks"][0]
        if last["lastStatus"] in TERMINAL:
            break
        time.sleep(POLL_SECONDS)

    print_h("Result")
    print_c(f"  status   : {last['lastStatus']}")
    if last.get("stopCode"):
        print_c(f"  stopCode : {last['stopCode']}")
    container = (last.get("containers") or [{}])[0]
    if container.get("exitCode") is not None:   # exitCode 在 containers[] 上，Task 本身沒有這個欄位
        print_c(f"  exitCode : {container['exitCode']}")

    group, stream = resolve_log_stream(state, last)
    print_logs(state, group, stream, 100)

    if "--stop" in sys.argv and last["lastStatus"] not in TERMINAL:
        call("ecs", "stop_task", cluster=state["cluster"], task=last["taskArn"])
        print_c("  （任務已停）")
    elif last["lastStatus"] not in TERMINAL:
        print_c("提示：一次性任務會計費到被停掉為止（Fargate 以 15 分鐘為一個計費單位）；跑完请加 --stop")

    return 0 if container.get("exitCode") in (0, None) else 1


if __name__ == "__main__":
    sys.exit(main())
