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
# AWS 一鍵部署（三步，第一次）
python aws/bootstrap.py; python aws/secret.py; python aws/deploy.py
```

```bat
# 以後每次發佈（build + push + register + update service + 輪詢 rollout）
deploy.bat
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

### `.aws-state/` 狀態檔

所有 AWS 腳本都讀 `.aws-state/config.json`（`bootstrap.py` / `deploy.py` / `status.py` / `run_once.py` / `netcheck.py` 會把讀到的資源 id 寫回去）。
這個資料夾是 gitignored（可能含連線字串），repo 裡只有欄位範本 `aws-state.example.json`。

換機器或新 clone 時，兩種做法：

1. **直接把整個 `.aws-state/` 複製過去**（最快，連 `neon.json` 一起帶走）。
2. **重跑一次讓腳本自己寫回來**（`bootstrap.py` 是冪等的：已存在的資源會走 `AlreadyExists` / `Duplicate` 分支，把 id 重新寫進 state）：

   ```powershell
   python aws/bootstrap.py     # cluster / roles / SG / ALB / target group / listener → config.json
   python aws/neon.py          # 重建 neon.json（需要 NEON_API_KEY）
   python aws/secret.py        # 重建 dbSecretArn
   ```

`aws-state.example.json` 的欄位就是腳本會寫入的鍵（值都是佔位字串，不用照抄）；腳本缺哪個鍵會自己補，所以它只是對照表。
`aws/rds.py`（備選的 RDS 路）另外會寫 `rdsSgId` / `rdsEndpoint` / `rdsDbName` / `rdsUser`。

`.aws-state/neon.json` 的形狀：`project` / `projectId` / `branch` / `branchId` / `database` / `region` / `connectionUrl`（含密碼，所以只在本地）。

### 正式機資料庫

Neon 專案 `luca-python`（獨立 compute，資料庫同名 `awsLucaPysn`）：

```powershell
python aws/neon.py            # 印出 Neon 連線資訊（需要 NEON_API_KEY）
python aws/secret.py          # 連線字串存進 Secrets Manager
```

`NEON_API_KEY` 取得方式：Neon Console → 左上 **Settings** → **API Keys** → **Add API key**
（選 Personal key 即可；組織 key 會限定 org）。拿到後寫進 gitignored 的 `.env`：

```
NEON_API_KEY=napi_xxx...
```

只有這支腳本會用到 key；app 執行期不需要（執行期只讀 Secrets Manager 的 `DATABASE_URL`）。
若 `.aws-state/neon.json` 已有紀錄，沒有 key 時 `neon.py` 會直接把現存紀錄印出來，不會崩潰。

`aws/rds.py` 是「自己管 RDS」的備選方案（與 Neon 二選一，不要同時用）。

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

## 4. 以後怎麼發佈（deploy.bat）

`deploy.bat` 只做分派（全 ASCII、1024 bytes 以內），真正的動作在 `aws/ops.py`：

| 指令 | 動作 |
|---|---|
| `deploy.bat` | `aws/deploy.py`：docker build → push ECR → register revision → update service（輪詢到 `COMPLETED`） |
| `deploy.bat health` | 打 ALB 的 `/api/health` |
| `deploy.bat smoke` | `scripts/smoke_test.py` 打 ALB |
| `deploy.bat logs [n]` | 最近 n 條 CloudWatch events（預設 20） |
| `deploy.bat migrate` | 在 VPC 內的一-shot 任務跑 `scripts/migrate.py`（一定附 `--stop`） |
| `deploy.bat status` | 資源狀態 |
| `deploy.bat data` | 本機資料 → 冪等 upsert SQL（`scripts/dump_data.py`） |
| `deploy.bat usage` | 說明 |

標準流程：

1. 改程式 → `gitGithubPush.bat`（會先 push `LucaPython`，再 push 父倉的 gitlink）
2. `deploy.bat`（image 是从目前工作樹 build 的，所以要先把要上的內容 commit/確認）
3. `deploy.bat health` 或 `deploy.bat smoke` 驗收

> `.bat` 保持全 ASCII 且 ≤1024 bytes：cmd 讀 .bat 是一塊 1024 bytes，行被切開會報 `not recognized`；
> 中文訊息由 `aws/ops.py`（Python 印 UTF-8）負責。

---

## 5. PostgreSQL 手動同步

`schema.sql` 是唯一的 schema 來源，`scripts/migrate.py` 是冪等（`IF NOT EXISTS`），重複跑不會壞資料。

**重要限制**：本機 outbound 5432 連不到 Neon（實測 timeout），連容器內跑的 app 也連不到；
所以「本機直接寫 Neon」這條路走不通，只能走 443（Neon Console 的 SQL Editor）或 VPC 內的一-shot 任務。

### A. Schema 同步

```powershell
# 本機（postgres17）
python scripts/migrate.py

# 正式區（Neon，VPC 內 one-shot task）
deploy.bat migrate
```

### B. 資料同步（本機 → 正式區）

```powershell
# 1) 從本機 DB 產生冪等 SQL（upsert；--mode=insert 只補缺漏不覆蓋）
python scripts/dump_data.py --out=neon_sync_items.sql

# 2a) 手工：Neon Console → SQL Editor → 貼上 → Run（一次一句，或整段貼）
# 2b) 脚本：透過 VPC 內 one-shot task 執行（SQL 不在 image 內，所以用 base64 帶進去）
python scripts/apply_sql.py --file=neon_sync_items.sql          # 本機端（打本機 DB，可先演练）
python aws/run_once.py "python scripts/apply_sql.py --b64=<base64>" --stop   # 正式區（打 Neon）
```

`apply_sql.py` 在一個交易內跑完、成功才 commit，失敗 rollback，不會留半套資料。

### C. 反向（正式區 → 本機）

- Neon Console → SQL Editor → `SELECT ...` → 結果可下載 CSV，再用 `docker exec postgres17 psql -U admin -d awsLucaPysn` 匯入；
- 或在 VPC 內跑 `python scripts/apply_sql.py --b64=...`（把要插的 SQL 帶進去）。

### D. 安全守則

- 只用 `INSERT ... ON CONFLICT (id) DO UPDATE`（upsert），不要 `TRUNCATE`/`DELETE`；
- 正式區 one-shot 任務一定附 `--stop`（Fargate 以 15 分鐘為計費單位）；
- 本機開發 DB 與 Neon 正式 DB 同名 `awsLucaPysn`，打錯邊最常見 — 看 `/api/health` 的 `env`（`development` = 本機、`production` = Neon）；
- 密碼只在 gitignored 的 `.env` / `.env.local` 與 Secrets Manager，不會進 repo。

---

## 6. `.aws-state/`（狀態檔）

- `config.json`：AWS 資源 ID（`bootstrap.py` 寫入）＋ `db_secret_name` / `db_secret_arn`
- `neon.json`：Neon 專案、分支、連線字串

`aws/*.py` 只認這份檔，不硬碼資源 ID；換機器時把 `.aws-state/` 一起帶過去就能重現。

---

## 7. 目錄結構

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
│  ├─ migrate.py        # 逐句執行 schema.sql（冪等）
│  ├─ dump_data.py      # 本機資料 → 冪等 upsert SQL（給 Neon SQL Editor）
│  ├─ apply_sql.py      # 執行 SQL 檔/單句（交易內，失敗 rollback）
│  ├─ healthcheck.py    # 容器 HEALTHCHECK（slim image 沒有 curl）
│  └─ smoke_test.py     # 冒煙測試：health / 寫入 / 讀取 / 頁面 / 303
├─ aws/                 # 部署工具（boto3）
│  ├─ aws_common.py     # 狀態檔、AWS API、task definition 組合
│  ├─ ops.py            # 選單實作（deploy.bat 分派到這裡）
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
├─ deploy.bat           # 一鍵發佈的選單（ASCII、≤1024 bytes，分派給 aws/ops.py）
├─ deploy.ps1
└─ readme.md
```

---

## 8. 里程碑現況

| 里程碑 | 狀態 |
|---|---|
| M0 資料庫 `awsLucaPysn` | ✅ 完成（postgres17 本地 + Neon 同名） |
| M1 骨架 | ✅ 完成 |
| M2 資料層（schema / migrate） | ✅ 完成 |
| M3 健康檢查 | ✅ 完成（冒煙測試通過） |
| M4 CRUD + Jinja2 | ✅ 完成（冒煙測試通過） |
| M5 Docker | ✅ 完成（HEALTHCHECK healthy） |
| M6 AWS 腳本（含 IAM policy） | ✅ 完成（policy 已掛到 `lucaaws`；加 PassRole/GetUser/ListUserPolicies/RunTask/StopTask） |
| M7 正式上線 | ✅ 完成（ECR → ECS Fargate → ALB；`/api/health` 200，DB = Neon） |
| M8 收尾 | ✅ 完成（`deploy.bat` 選單、手動同步流程、readme/文件） |
