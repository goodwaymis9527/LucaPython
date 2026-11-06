-- schema 由 scripts/migrate.py 套用，不在每次請求裡自動建表
-- 這份檔案也能直接貼到 pgAdmin、Neon SQL Editor，或 psql 對容器/RDS 執行
--
-- ⚠ 資料庫名稱 awsLucaPysn 含大寫：寫 SQL 識別字時要加引號（"awsLucaPysn"），
--   否則 PostgreSQL 會 fold 成小寫 awslucapysn。透過 DATABASE_URL 連線不受影響。

CREATE TABLE IF NOT EXISTS items (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS items_created_at_idx ON items (created_at DESC);

-- 與 Node 版共用的寫入來源標記（方便比較兩邊資料）
CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO app_meta (key, value) VALUES ('app', 'luca-python')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

-- pgvector（Qdrant 相關的預留）：Neon 預設可用，RDS 要手動開擴
-- CREATE EXTENSION IF NOT EXISTS vector;
-- CREATE TABLE IF NOT EXISTS docs (
--   id BIGSERIAL PRIMARY KEY,
--   content TEXT,
--   embedding vector(1024),
--   created_at TIMESTAMPTZ NOT NULL DEFAULT now()
-- );
