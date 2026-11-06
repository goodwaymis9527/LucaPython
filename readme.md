# LucaPython

LucaAWS（Node.js）的 Python 版本：**Docker + FastAPI + PostgreSQL**。
本機用 Docker 跑，正式機一鍵部署到 AWS（ECR → ECS Fargate → ALB）。

完整設計與里程碑：`../20261106_LucaPython架構_OpenCode/架構開發文件.md`

---

## 0. 一行指令速查（PowerShell 直接跑）

```powershell
cd D:\goodwaymis9527\LucaPython
```

```powershell
# 建專案 + 裝依賴
pip install -r requirements.txt
```

```powershell
# 建資料庫 awsLucaPysn（container postgres17）
docker exec postgres17 psql -U admin -d postgres -c "CREATE DATABASE \"awsLucaPysn\" -O admin"
```

```powershell
# 建表
$env:DATABASE_URL='postgresql://admin:%40<password>@postgres17:5432/awsLucaPysn'; python scripts/migrate.py
```

```powershell
# 本機開發（連 postgres17 容器，http://localhost:8090）
docker compose --profile external-db up -d --build
```

```powershell
# 只跑容器（驗證 HEALTHCHECK）
docker build -t lucapython:dev .; docker run --rm --network shared-network -p 8090:8000 -e DATABASE_URL='postgresql://admin:%40<password>@postgres17:5432/awsLucaPysn' lucapython:dev
```

```powershell
# 冒煙測試 / 健康檢查（本機對應是 8090；容器內跑則用 localhost:8000）
python scripts/smoke_test.py --base=http://127.0.0.1:8090
docker exec luca-python python scripts/smoke_test.py
```

```powershell
# AWS 一鍵部署（三步）
python aws/bootstrap.py; python aws/secret.py; python aws/deploy.py
```

```powershell
# 服務狀態與任務日誌
python aws/status.py --logs 30
```

```powershell
# 在任務內執行指令（例如正式機建表）
python aws/exec.py "python scripts/migrate.py"
```

```powershell
# 整批：環境檢查 + build + 健康檢查
.\deploy.ps1
```

---

## 1. 環境變數

| 變數 | 說明 | 範例 |
|---|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串（唯一資料來源，優先讀 `.env.local`） | `postgresql://admin:%40<password>@postgres17:5432/awsLucaPysn` |
| `PORT` | 監聽端口（容器內固定 8000） | `8000` |

> URI 裡的 `@` 必須寫成 `%40`。

`.env.local`（本機開發用，已列入 `.gitignore`）：

```
DATABASE_URL=postgresql://admin:%40<password>@postgres17:5432/awsLucaPysn
PORT=8000
```

`docker compose` 另外讀同目錄的 `.env`（同樣不進倉庫），`docker-compose.yml` 裡的密碼是用變數插值：

```
DB_USER=admin
DB_PASSWORD=%40<password>      # 真正的密碼在这里（檔案不入倉庫）
```

正式機不需要 `.env.local`：`aws/secret.py` 把同一份連線字串存進 Secrets Manager
（`luca-python/database`），任務透過 `secrets` 注入，機密不會進環境變數也不會進 image。

---

## 2. 本機跑法

1. 確認 `postgres17` 容器在跑（`shared-network` 由 compose 自動建立）。
2. `docker compose up -d --build` → 打開 <http://localhost:8090>
   - `/` → Jinja2 首頁
   - `/api/health` → `{"ok":true,"db":{"driver":"docker","region":"local","env":"development","host":"postgres17","database":"awsLucaPysn","pool":{"min":1,"max":5},"prepare":false},"error":null}`
     （`.ps1 -Step health` 會同時打 ALB 與本機 8090 兩邊，看 `env` 就知道連的是哪一邊）
   - `/api/items` → 列表與 POST 新增

`docker-compose.yml` 兩個 service：
- `app`：連本機 `postgres17`（`profiles: [local-db]`）
- `app-external-db`：同一個 image 連外部資料庫（`profiles: [external-db]`）
  要連 Neon 時用 `docker compose --profile external-db up`

本機端口 `8090:8000`（8000 已被 `ai-service` 占用）。

---

## 3. AWS 部署

### 前置（一次性）

```powershell
python aws/netcheck.py        # VPC / IGW / 0.0.0.0/0 路由檢查
python aws/bootstrap.py       # ECR + IAM roles + CloudWatch + ALB + target group + listener
```

> `bootstrap.py` 需要 IAM 權限，policy 見
> `../20261106_LucaPython架構_OpenCode/lucaaws-iam-policy.json`。

### 正式機資料庫

Neon 專案 `luca-python`（獨立 compute，資料庫同名 `awsLucaPysn`）：

```powershell
python aws/rds.py             # 印出 Neon 連線資訊
python aws/secret.py          # 連線字串存進 Secrets Manager
```

本機直連 Neon 會 timeout（5432 被擋），所以正式機資料庫只能從 VPC 內存取；
本機開發繼續用 `postgres17`。

### 一鍵部署

```powershell
python aws/deploy.py
```

流程：`docker build` → `docker push` → `register_task_definition`（只換 image）→
`update_service` → 輪詢部署穩定。

驗證：

```powershell
python aws/status.py --logs 20
Invoke-RestMethod http://<ALB-DNS>/api/health
python aws/exec.py "python scripts/migrate.py"
```

---

## 4. `.aws-state/`（狀態檔）

- `config.json`：AWS 資源 ID（`bootstrap.py` 寫入）＋ `db_secret_name` / `db_secret_arn`
- `neon.json`：Neon 專案、分支、連線字串

`aws/*.py` 只認這份檔，不硬碼資源 ID；換機器時把 `.aws-state/` 一起帶過去就能重現。

---

## 5. 目錄結構

```
LucaPython/
├─ app/                 # FastAPI 應用程式
│  ├─ main.py           # 進入點
│  ├─ config.py         # 環境變數（環境變數 > .env.local）
│  ├─ db.py             # psycopg v3 連線池 + query/list_items/add_item/ping_db/db_info
│  ├─ models.py         # pydantic（Item / ItemCreate，datetime → ISO）
│  ├─ routers/api.py    # /api/health、GET/POST /api/items
│  ├─ routers/pages.py  # /（Jinja2）、/readyz
│  ├─ templates/index.html
│  └─ static/style.css
├─ scripts/
│  ├─ migrate.py        # 逐句執行 schema.sql
│  ├─ healthcheck.py    # 容器 HEALTHCHECK（slim image 沒有 curl）
│  └─ smoke_test.py     # 冒煙測試：health / 寫入 / 讀取 / 頁面 / 303
├─ aws/                 # 部署工具（boto3）
│  ├─ aws_common.py     # 狀態檔、AWS API、task definition 組合
│  ├─ bootstrap.py      # ECR + IAM roles + CloudWatch + SG + ALB + cluster + service
│  ├─ deploy.py         # 一鍵部署（build / push / revision / update）
│  ├─ secret.py         # DATABASE_URL → Secrets Manager
│  ├─ neon.py           # Neon 專案與連線字串（與狀態檔比對）
│  ├─ status.py         # service / task / 最近日誌
│  ├─ exec.py           # SSE 任務內執行（需 ssmcli）
│  ├─ rds.py            # 備選路：改走 RDS 時用
│  ├─ netcheck.py       # VPC / IGW / 0.0.0.0/0 路由檢查
│  └─ task-definition.json
├─ schema.sql
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt / requirements-dev.txt
├─ .env.example（.env.local 是本機版，不 commit）
├─ deploy.ps1
└─ readme.md
```

---

## 6. 里程碑現況

| 里程碑 | 狀態 |
|---|---|
| M0 資料庫 `awsLucaPysn` | ✅ 完成（postgres17 本地 + Neon 同名） |
| M1 骨架 | ✅ 完成 |
| M2 資料層（schema / migrate） | ✅ 完成 |
| M3 健康檢查 | ✅ 完成（冒煙測試通過） |
| M4 CRUD + Jinja2 | ✅ 完成（冒煙測試通過） |
| M5 Docker | ✅ 完成（HEALTHCHECK healthy） |
| M6 AWS 腳本（含 IAM policy） | ✅ 寫完、語法檢查通過（policy 尚未掛到 `lucaaws`） |
| M7 正式上線 | ◐ Neon 專案已建；`bootstrap.py` 跑到第 3 步（ECR）被 IAM 權限擋下 |
