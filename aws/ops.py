r"""ops.py — deploy.bat 的選單實作（把指令分派給 aws/*.py 與 scripts/*.py）

為什麼用 Python 實作選單：cmd 讀 .bat 是 1024 bytes 一塊，行被切開就會報 "not recognized"，
所以 deploy.bat 壓到 1024 bytes 以內只做分派，中文訊息與邏輯都放在這裡（Python 印 UTF-8 沒問題）。

用法：
  python aws/ops.py                      # 等於 deploy
  python aws/ops.py deploy               # build + push + register + update service（含輪詢 rollout）
  python aws/ops.py health               # 打 ALB 的 /api/health
  python aws/ops.py smoke                # scripts/smoke_test.py 打 ALB
  python aws/ops.py logs 20              # 最近 20 條 CloudWatch events
  python aws/ops.py migrate              # VPC 內 one-shot task 跑 scripts/migrate.py（--stop）
  python aws/ops.py status               # 資源狀態
  python aws/ops.py data --out=xxx.sql   # 本機資料 → 冪等 upsert SQL（貼進 Neon SQL Editor）

剩餘參數會直接交給對應腳本，例如：
  python aws/ops.py deploy --skip-build --skip-push
  python aws/ops.py data --mode=insert --table=items

跑完會印「=== 結果：✔ 成功 / ✘ 失敗 ===」並停在畫面上等一個鍵（雙擊 .bat 時視窗不會秒閉）；
自動化/pipe 時 stdin 不是終端機就自動不暫停，或加 --no-pause、設 LUCA_NO_PAUSE=1。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ACTIONS = {
    "deploy": ("aws/deploy.py",),
    "health": None,          # 自己實作（只是一次 GET）
    "smoke": ("scripts/smoke_test.py",),
    "logs": ("aws/status.py", "--logs"),
    "migrate": ("aws/run_once.py", "python scripts/migrate.py", "--stop"),
    "status": ("aws/status.py",),
    "data": ("scripts/dump_data.py",),
}

USAGE = """usage:
  deploy.bat                 build + push + register + update service (waits for the rollout)
  deploy.bat health          GET /api/health through the ALB
  deploy.bat smoke           smoke test against the ALB
  deploy.bat logs [n]        last n CloudWatch events (default 20)
  deploy.bat migrate         apply schema.sql in a one-shot task (with --stop)
  deploy.bat status          resource status
  deploy.bat data            dump local rows to an idempotent upsert .sql (paste into Neon)

tip: commit first with gitGithubPush.bat (submodule, then the parent gitlink)
tip: the image is built from the current working tree, so deploy = whatever is on disk
tip: local outbound 5432 to Neon is blocked; data sync goes through the Neon SQL Editor
tip: the screen holds at the end so you can read the result; use --no-pause (or LUCA_NO_PAUSE=1) to skip it
"""


def state() -> dict:
    path = ROOT / ".aws-state" / "config.json"
    if not path.exists():
        raise SystemExit(".aws-state/config.json 不存在。先跑一次 python aws/bootstrap.py")
    return json.loads(path.read_text(encoding="utf-8"))


def run(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(ROOT / script)] + args
    print(" ".join(cmd))
    return subprocess.run(cmd).returncode


def pause() -> None:
    """跑完停在畫面上等一個鍵：雙擊 deploy.bat 時視窗會秒閉，結果來不及看。

    stdin 不是終端機（pipe、redirect、CI、自動化）時暫停會卡死，所以跳過；
    想手動跳過就加 --no-pause 或設 LUCA_NO_PAUSE=1。
    """
    if os.environ.get("LUCA_NO_PAUSE") == "1" or not sys.stdin.isatty():
        return
    try:
        input("\n按 Enter 結束（結果已在畫面上）...")
    except (EOFError, KeyboardInterrupt):
        print()


def dispatch(action: str, rest: list[str]) -> int:
    if action not in ACTIONS:
        print(f"[X] unknown action: {action}")
        print(USAGE)
        return 1
    if action == "health":
        return health()
    if action == "smoke":
        return run("scripts/smoke_test.py", [f"--base=http://{state()['albDns']}"] + rest)
    if action == "logs":
        return run("aws/status.py", ["--logs"] + (rest or ["20"]))
    if action == "data":
        print("local -> Neon：本機連不到 Neon 的 5432，所以這裡只產生 SQL 檔，"
              "再貼到 Neon Console 的 SQL Editor（或 VPC 內 one-shot task）執行\n")
        return run("scripts/dump_data.py", rest)
    return run(ACTIONS[action][0], list(ACTIONS[action][1:]) + rest)


def health() -> int:
    base = "http://" + state()["albDns"]
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=30) as resp:
            print(f"GET {base}/api/health -> {resp.status}")
            print(resp.read().decode("utf-8", "replace"))
    except Exception as err:
        print(f"✘ health 失敗：{err}")
        return 1
    return 0


def main() -> int:
    argv = sys.argv[1:]
    action = argv[0] if argv else "deploy"
    rest = [a for a in argv[1:] if a != "--no-pause"]
    no_pause = "--no-pause" in argv

    if action in ("usage", "-h", "--help"):
        print(USAGE)
        return 0

    try:
        code = dispatch(action, rest)
    except SystemExit as err:            # 子腳本的 raise SystemExit("...") 或 state() 缺檔
        code = 1 if err.code not in (0, None) else 0
        if code:
            print(f"✘ {err}")
    except KeyboardInterrupt:
        print("使用者中斷")
        code = 130

    # 統一在這裡顯示成功與否：deploy.py 用 exit code 表達結果（0 = rollout COMPLETED 且健康檢查通過）
    print("")
    print(f"=== 結果：{'✔ 成功' if code == 0 else '✘ 失敗（exit ' + str(code) + '）'} ===")
    if not no_pause:
        pause()
    return code


if __name__ == "__main__":
    sys.exit(main())
