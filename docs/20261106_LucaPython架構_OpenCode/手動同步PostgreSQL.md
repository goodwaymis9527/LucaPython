---
標題: LucaPython 以後怎麼發佈 + PostgreSQL 手動同步
作者: Luca
建立: 2026-11-06
更新: 2026-11-06
標籤:
  - LucaPython
  - AWS
  - ECS-Fargate
  - Neon
  - PostgreSQL
  - 工作流
狀態: 可執行（已實測）
語言: zh-TW
---

# 1. 以後要正式發佈到 AWS：`deploy.bat`

`LucaPython\deploy.bat` 只做分派，邏輯在 `LucaPython\aws\ops.py`。

```
標準流程
  1) 改程式 → D:\goodwaymis9527\gitGithubPush.bat   （先 push LucaPython，再 push 父倉的 gitlink）
  2) deploy.bat                                     （build → push ECR → register revision → update service → 輪詢到 COMPLETED）
  3) deploy.bat health 或 deploy.bat smoke          （驗收 ALB）
```

`deploy.bat` 的動作：

| 指令 | 做什麼 |
|---|---|
| `deploy.bat`（無參數） | 完整發佈：docker build → push → 新 revision → update service，輪詢 rollout（最多 5 分鐘） |
| `deploy.bat health` | `GET http://<ALB>/api/health` |
| `deploy.bat smoke` | `scripts/smoke_test.py --base=http://<ALB>` |
| `deploy.bat logs [n]` | 最近 n 條 CloudWatch events（預設 20） |
| `deploy.bat migrate` | VPC 內 one-shot task 跑 `scripts/migrate.py`（一定附 `--stop`） |
| `deploy.bat status` | 資源狀態（service / task def / ECR / ALB / secret / log group / role） |
| `deploy.bat data` | 本機 `awsLucaPysn` 的列 → 冪等 upsert SQL 檔 |
| `deploy.bat usage` | 說明 |

為什麼 `.bat` 壓成 1024 bytes 以內、且全 ASCII：
cmd 讀 .bat 是 1024 bytes 一塊，若邊界落在多字节（UTF-8）字元中間、或行跨越邊界，该行會被切開而報 `not recognized`。
實測：`gitGithubPush.bat` 因為新增一行把位移動而觸發；所以中文訊息改由 `aws/ops.py`（Python 印 UTF-8）輸出。
目前 `deploy.bat` = 499 bytes、0 個非 ASCII 字元。

## 實測記錄

| 動作 | 結果 |
|---|---|
| `deploy.bat` | image tag `20261106080908` → revision 7 → rollout `COMPLETED`（1/1） |
| `deploy.bat usage` | 印選單，exit 0 |
| `deploy.bat health` | HTTP 200，`ok:true`，`db.host = ep-calm-glitter-...`（Neon） |
| `deploy.bat status` | 資源清單正常 |
| `deploy.bat data` | 產生 16 列 upsert SQL |
| `deploy.bat migrate` | one-shot task exitCode 0，CloudWatch log 印出 4 句 schema + `items 表存在=True` |

目前線上：revision 7，image `20261106080908`，
網頁 `http://luca-python-1645072623.ap-southeast-2.elb.amazonaws.com/`。

---

# 2. PostgreSQL 手動同步

## 前提（這條路走不通）

本機 outbound 5432 連不到 Neon（實測 TCP timeout），連本機跑起來的 app 容器也連不到：

```powershell
docker exec luca-python python -c "import socket; socket.create_connection(('ep-calm-glitter-...',5432), 8)"
# → 失敗
```

所以「本機直接寫 Neon」不可行。能用的只有兩條：

1. **443**：Neon Console 的 SQL Editor（瀏覽器）／Neon API（管理类 route）
2. **VPC 內**：one-shot Fargate 任務（`aws/run_once.py`），任務內 `DATABASE_URL` 来自 Secrets Manager → 連得到 Neon

本機開發 DB 仍是 `postgres17` 容器的 `awsLucaPysn`，與 Neon 的 `awsLucaPysn` 同名，方便比對。

## A. Schema 同步（冪等）

`schema.sql` 是唯一來源，`scripts/migrate.py` 全部用 `IF NOT EXISTS`，重複跑不會壞資料。

```powershell
# 本機
python scripts/migrate.py

# 正式區（VPC 內 one-shot）
deploy.bat migrate
```

## B. 資料同步（本機 → 正式區）

```powershell
# 1) 產生冪等 SQL（預設 upsert；--mode=insert 只補缺漏、不覆蓋目標端已有內容）
python scripts/dump_data.py --out=neon_sync_items.sql

# 2a) 手工：Neon Console → SQL Editor → 貼上 → Run
# 2b) 脚本：把 SQL 用 base64 帶進 VPC 內的一次性任務執行（檔不在 image 內，所以用 --b64）
python scripts/apply_sql.py --file=neon_sync_items.sql                        # 本機端演练
python aws/run_once.py "python scripts/apply_sql.py --b64=<base64>" --stop    # 正式區（Neon）
```

`apply_sql.py` 的行為：在**一個交易**內跑完、成功才 commit，失敗 rollback（不會留半套資料），
輸出每句的影響列數。實測（VPC → Neon，讀取）：`target: awsLucaPysn @ ep-calm-glitter-...`、1 列、commit 完成。

注意：`scripts/*.py` 是 build 當下複製進 image 的，所以新腳本要用一次 `deploy.bat` 才會出現在任務裡（這次就是這樣把 `apply_sql.py` 上線）。

## C. 反向（正式區 → 本機）

- Neon Console → SQL Editor 跑 `SELECT`，結果下載 CSV，再匯入本機：
  ```powershell
  docker exec -i postgres17 psql -U admin -d awsLucaPysn -f 從CSV產生的.sql
  ```
- 或在 VPC 內跑 `apply_sql.py --b64=...` 產生資料、再用 SQL Editor 讀。

## D. 守則

1. 只用 `INSERT ... ON CONFLICT (id) DO UPDATE`（upsert），不要 `TRUNCATE` / `DELETE`。
2. one-shot 任務一定附 `--stop`（Fargate 以 15 分鐘為計費單位）。
3. 分清楚打哪一邊：`/api/health` 的 `env` = `development`（本機）／`production`（Neon）。
4. 密碼只在 gitignored 的 `.env` / `.env.local` 與 Secrets Manager。
5. Neon 的 pooler 是 transaction mode：`SET`／prepared statements 這類 session 級行為不支援，
   所以 `migrate.py` 要嘛用直接 endpoint，要嘛走 SQL Editor。

---

# 3. 未解項目（可選）

- 本機連 Neon 的 5432 被擋 → 若要「本機直接同步」，需要能把 SQL 走 443 的官方 route；
  目前用 personal API key 打 `console.neon.tech/api/v2` 的 SQL route 會 404（`this route does not exist`），
  實測只有 Neon MCP 的 `run_sql`（走它自家的 SQL service）能用 → 所以「我幫你執行」可以，但你自己跑的脚本要走 SQL Editor 或 VPC。
- HTTPS：ACM 不能為 `*.elb.amazonaws.com` 簽證照，要有自己的網域 CNAME 到 ALB + 443 listener。
- pytest、GitHub Actions OIDC push 到 ECR（尚未做）。
