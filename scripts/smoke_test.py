r"""smoke_test.py — 端到端跑一遍 API（只用 urllib，不依賴 requests）

用途：本機（含 Docker 內）與正式區都能用同一支腳本驗證 M3/M4 的行為。

執行：
  # 容器內（slim 鏡沒有 curl，但一定有 python）
  docker exec luca-python python scripts/smoke_test.py

  # 本機直打（compose 的 mapping 是 8090 -> 8000）
  python scripts/smoke_test.py --base=http://127.0.0.1:8090

  # 正式區（ALB）
  python scripts/smoke_test.py --base=http://<alb-dns>

環境變數 SMOKE_BASE 也可以指定 base。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Windows 控制台預設 cp950，✔/✘ 會爆炸；統一用 utf-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

argv = sys.argv[1:]
base = next((a.split("=", 1)[1] for a in argv if a.startswith("--base=")), None) \
    or os.environ.get("SMOKE_BASE") or "http://127.0.0.1:8000"

failures: list[str] = []


def _check(name: str, status: int, allowed: tuple[int, ...]) -> None:
    if status in allowed:
        print(f"  ✔ {name} -> {status}")
    else:
        print(f"  ✘ {name} -> {status}")
        failures.append(name)


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw[:200]
    except urllib.error.HTTPError as exc:
        # 把錯誤回應抓出來，才看得懂 500 的真正原因
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def main() -> int:
    print(f"base: {base}\n")

    status, health = call("/api/health")
    db = health.get("db", {}) if isinstance(health, dict) else {}
    print(f"[1] 健康檢查")
    _check("GET /api/health", status, (200,))
    print(f"    ok={health.get('ok')} host={db.get('host')} db={db.get('database')} error={db.get('error')}")
    db_ok = bool(health.get("ok"))

    print("\n[2] 寫入 + 讀取")
    status, item = call("/api/items", "POST", {"title": "smoke test", "body": "created by scripts/smoke_test.py"})
    _check("POST /api/items", status, (200, 201))
    print(f"    item: {json.dumps(item, ensure_ascii=False, default=str)}")

    status, items = call("/api/items?limit=5")
    _check("GET /api/items", status, (200,))
    print(f"    count={len(items) if isinstance(items, list) else items}")
    if isinstance(items, list):
        for row in items[:5]:
            print(f"    - {row.get('id')} {row.get('title')}")

    print("\n[3] HTML 頁面")
    status, _ = call("/")
    _check("GET /", status, (200,))

    print("\n[4] 表單送出（form-encoded POST /api/items -> 303 回 /）")
    req = urllib.request.Request(
        base + "/api/items",
        data=b"title=form+post&body=from+form",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _check("POST /api/items (form)", resp.status, (200, 201, 303))
            print(f"    location: {resp.headers.get('Location')}")
    except urllib.error.HTTPError as exc:
        _check("POST /api/items (form)", exc.code, (303,))
        print(f"    body: {exc.read().decode('utf-8', 'replace')[:200]}")

    print("")
    if failures:
        print(f"結果: ✘ 有 {len(failures)} 項失敗：{', '.join(failures)}")
        return 1
    print("結果: ✔ 全部通過" + ("" if db_ok else "（但 DB 沒連上）"))
    return 0 if db_ok else 1


if __name__ == "__main__":
    sys.exit(main())
