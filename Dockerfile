# ── 1) build：只裝執行期依賴（requirements-dev.txt 不進鏡像）──
FROM python:3.12-slim AS build
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── 2) runtime：只帶装好的套件與原始碼，鏡像小、啟動快 ──
FROM python:3.12-slim AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV GUNICORN_WORKERS=2

COPY --from=build /install /usr/local
COPY . .

# 非 root：Fargate 預設跑 root，但比照既有 container 習慣收權限
RUN useradd -m -u 10001 appuser
USER appuser

EXPOSE 8000

# migrate 要在 VPC 內跑（本機 outbound 5432 被封）：
#   aws ecs execute-command --cluster luca-python --task <taskArn> --container luca-python --command "python scripts/migrate.py"
# 所以 scripts/ 與 schema.sql 已進鏡像（COPY . . 涵蓋）。

# slim 沒有 wget/curl → 用 python 探測器
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]

# UvicornWorker：FastAPI 的 async 路。每 worker 各有一個 psycopg 連線池（DB_POOL_MAX）
# 這裡刻意用 shell 形式（不是 JSON 陣列），因為 ${PORT} / ${GUNICORN_WORKERS} 需要 shell 展開；
# docker lint 的 JSONArgsRecommended 警告就這樣留著（gunicorn 會收到 SIGHUP/SIGTERM，Fargate 停任務時正常）。
CMD gunicorn app.main:app \
  --bind 0.0.0.0:${PORT} \
  --workers ${GUNICORN_WORKERS} \
  --worker-class uvicorn.workers.UvicornWorker \
  --timeout 30 \
  --access-logfile - \
  --error-logfile -
