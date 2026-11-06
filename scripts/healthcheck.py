#!/usr/bin/env python3
"""健康檢查用的探測器。

python:3.12-slim 裡沒有 wget/curl，所以 Dockerfile 的 HEALTHCHECK 與 ECS 的 healthCheck
都改成叫這個腳本（不用另裝工具，也不會因為缺 wget 而永遠 unhealthy）。
"""
import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
url = f"http://127.0.0.1:{port}/api/health"

try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = resp.read().decode("utf-8", "replace")
        if resp.status == 200:
            print(f"healthy ({url}): {body}")
            sys.exit(0)
        print(f"unhealthy: HTTP {resp.status}", file=sys.stderr)
        sys.exit(1)
except Exception as err:
    print(f"unhealthy: {err}", file=sys.stderr)
    sys.exit(1)
