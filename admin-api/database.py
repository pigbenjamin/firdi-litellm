import json
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

# 2026-09 從 users-db-pvc(SQLite) 遷移到 Postgres（沿用 firdi-postgres,獨立的
# firdi_users database,跟 store_model_in_db 那顆 litellm database 無關,見
# k8s/postgres/deployment.yaml）。預設值只給本機開發用,正式環境一律由
# USER_AUTH_DATABASE_URL secret 注入。
DATABASE_URL = os.getenv(
    "USER_AUTH_DATABASE_URL",
    "postgresql://litellm:litellm@localhost:5432/firdi_users",
)

# schema 從 SQLite 1:1 照搬,刻意不順便升級型別（JSON 繼續存 TEXT、布林繼續用
# INTEGER 0/1）——避免在同一次改動裡疊加「換儲存引擎」+「換型別設計」兩種風險,
# 且現在的讀法都是整包撈出來在 Python 裡比對,SQL 層查詢 JSON 內容拿不到實際好處。
#
# 部門層 RPM/TPM 限流（dept_rpm_limit/dept_tpm_limit）整個移除,不只是不呼叫——
# 這個 in-memory 計數器本來就寫明「單 replica 適用」,多副本下會失真,而且 TPM
# 那半邊其實是死碼（呼叫點沒傳 token_estimate,判斷式永遠 False）。users 表另外
# 有一組同名但不同機制的 rpm_limit/tpm_limit（使用者層級,直接餵給 LiteLLM 原生
# 的 UserAPIKeyAuth）,不在移除範圍內,不要混淆。
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS departments (
    dept_id            TEXT PRIMARY KEY,
    dept_name          TEXT NOT NULL,
    openrouter_api_key TEXT NOT NULL DEFAULT '',
    allowed_models     TEXT NOT NULL DEFAULT '[]',
    -- 決策 E（見 docs/admin-web-plan.md）：openrouter_api_key 升級成
    -- provider_keys（JSON,key 為 provider 名稱）。舊欄位保留不刪,新舊由
    -- service 層保持同步（見 services/departments_service.py）。
    provider_keys      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL DEFAULT (now()::text),
    updated_at         TEXT NOT NULL DEFAULT (now()::text)
);

-- 決策 E：模型的上游（litellm_params.model）與 key 從哪來（部門 provider key 或
-- 模型自帶）解耦。key_policy 是 "model"（用模型自己定義的 key）或
-- "dept:<provider>"（用部門 provider_keys 裡該 provider 的 key,例如
-- "dept:openai"）。用 model_name（呼叫者請求時填的名字）當主鍵,因為 custom_auth
-- 熱路徑上只看得到這個字串,看不到 LiteLLM 內部的 deployment id。沒有紀錄的
-- model_name 由 custom_auth 自行推導預設值（openrouter/ 開頭 → dept:openrouter,
-- 其餘 → model）,不需要為既有模型補資料。
CREATE TABLE IF NOT EXISTS model_key_policies (
    model_name TEXT PRIMARY KEY,
    key_policy TEXT NOT NULL
);

-- 模型的管理面欄位（WP1/WP2）。刻意不塞進 LiteLLM 的 model_info,理由跟上面
-- model_key_policies 一樣：custom_auth 在每個請求的熱路徑上讀的是這顆 DB,讀
-- LiteLLM 的 model_info 等於在熱路徑多一個依賴。
--
-- 用 model_name 當主鍵而不是 LiteLLM 的 deployment id：id 在「草稿改設定＝刪除
-- 重建」與「停用＝從 LiteLLM 刪掉、啟用＝重新註冊」之後都會換一個新的,只有
-- model_name 從頭到尾不變,而且 model_name 正是授權（allowed_models /
-- users.models）與 OpenWebUI access_grants 認的那個字串。
--
-- 沒有紀錄的 model_name 一律視為 status='published' 的既有模型（見
-- services/model_metadata_service.py 的 DEFAULTS）,既有模型不需要任何資料回填。
--
-- status：
--   draft     已註冊到 LiteLLM（才測得起來）但 custom_auth 會擋掉一般使用者,
--             routing 欄位可改（實作是刪除重建）；要 last_test_ok=1 才能發布。
--   published 使用者可用；routing 欄位鎖定,描述性欄位（顯示名稱/備註/成本歸屬/
--             額度）仍可改。
--   disabled  已從 LiteLLM 刪除（使用者打不到、OpenWebUI 清單也看不到）,但這筆
--             設定完整保留,可一鍵重新註冊。
--
-- upstream/litellm_model/api_base/api_key 是「重新註冊時要用的原始參數」——
-- LiteLLM 的 /model/info 會遮罩 api_key,撈不回來,停用後要能原樣重建就只能自己
-- 留。跟 departments.provider_keys、users.api_key 同一顆 DB、同樣是明文欄位；
-- UI 與稽核紀錄一律只顯示末四碼（見 audit.mask_key）。
CREATE TABLE IF NOT EXISTS model_metadata (
    model_name       TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    model_type       TEXT NOT NULL DEFAULT 'chat',      -- chat | embedding | rerank
    cost_center      TEXT NOT NULL DEFAULT '',          -- 成本歸屬部門 dept_id,可留空
    budget_limit_usd REAL,                              -- NULL = 沒設額度
    budget_enforce   INTEGER NOT NULL DEFAULT 0,        -- 0=只記錄不擋 1=超額真的擋下來
    budget_period    TEXT NOT NULL DEFAULT 'monthly',   -- monthly | total
    -- 點數費率（每 1K token 幾點,可填小數）。這裡**只存不算**：扣點與部門／人員
    -- 的點數上限一律由外部系統處理,本平台不累計、不檢查、不擋（config/custom_auth.py
    -- 與 config/custom_logger.py 完全不看這兩個欄位）。外部系統要算點數的話,費率從
    -- GET /api/v1/models/external 的 meta 讀,token 數從 usage.jsonl 的 prompt_tokens /
    -- completion_tokens 讀。
    -- NULL 而不是 0 表示「還沒填」——0 在外部系統眼裡是「這個模型免費」,差很多。
    points_per_1k_prompt     REAL,
    points_per_1k_completion REAL,
    notes            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'draft',
    upstream         TEXT NOT NULL DEFAULT '',          -- model_upstreams.UPSTREAMS 的 key
    litellm_model    TEXT NOT NULL DEFAULT '',          -- litellm_params.model
    api_base         TEXT,
    api_key          TEXT NOT NULL DEFAULT '',          -- 共用 key；dept:* 政策的模型這裡是空的
    last_test_ok     INTEGER,                           -- NULL = 還沒測過
    last_test_at     TEXT,
    last_test_result TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (now()::text),
    updated_at       TEXT NOT NULL DEFAULT (now()::text)
);

-- 用量累計（WP1 的額度強制）。LiteLLM 自己的 spend tracking 在本專案是刻意關掉的
-- （litellm_config.yaml 的 disable_spend_logs / disable_spend_updates——用量記錄
-- 走 custom_logger 的 jsonl + Langfuse,那顆 Postgres 只存模型定義）,所以
-- LiteLLM 內建的 budget 機制在這裡沒有資料可用、根本不會生效。要「真的擋得下來」
-- 就只能自己累計：config/custom_logger.py 每次成功呼叫把 response_cost 加進來,
-- config/custom_auth.py 在認證時比對 model_metadata 的額度設定。
--
-- period 是 'YYYY-MM'（UTC,budget_period='monthly'）或 'total'（budget_period='total'）。
-- 兩種都會累計,換設定不會遺失歷史。
CREATE TABLE IF NOT EXISTS model_spend (
    model_name TEXT NOT NULL,
    period     TEXT NOT NULL,
    spend_usd  REAL NOT NULL DEFAULT 0,
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (now()::text),
    PRIMARY KEY (model_name, period)
);

-- 上架表單的「常用範本」（WP1）：把填過一次的表單欄位存起來,下次選範本直接帶入。
CREATE TABLE IF NOT EXISTS model_presets (
    preset_name TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,   -- JSON,上架表單的欄位值
    created_at  TEXT NOT NULL DEFAULT (now()::text),
    updated_at  TEXT NOT NULL DEFAULT (now()::text)
);

CREATE TABLE IF NOT EXISTS users (
    api_key         TEXT PRIMARY KEY,
    key_name        TEXT NOT NULL,
    user_id         TEXT NOT NULL UNIQUE,
    user_email      TEXT,
    dept_id         TEXT NOT NULL REFERENCES departments(dept_id),
    models          TEXT NOT NULL DEFAULT '[]',
    rpm_limit       INTEGER,
    tpm_limit       INTEGER,
    aliases         TEXT NOT NULL DEFAULT '{}',
    metadata        TEXT NOT NULL DEFAULT '{}',
    blocked         INTEGER NOT NULL DEFAULT 0,
    account_type    TEXT NOT NULL DEFAULT 'human',
    created_at      TEXT NOT NULL DEFAULT (now()::text),
    updated_at      TEXT NOT NULL DEFAULT (now()::text)
);

CREATE INDEX IF NOT EXISTS idx_users_dept_id ON users(dept_id);

CREATE TABLE IF NOT EXISTS db_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO db_version (id, version) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
"""


def init_db(dsn: str = DATABASE_URL) -> None:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLES_SQL)
        conn.commit()
    finally:
        conn.close()


_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    # 延遲建立：admin-api 的路由是同步 def（FastAPI 丟進 threadpool 執行）,
    # ThreadedConnectionPool 本身就是為這種多執行緒併發存取設計的。單副本
    # 部署,不需要外部連線池（pgbouncer 之類）。
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
    return _pool


class _ConnWrapper:
    """包一層讓呼叫端維持 sqlite3.Connection 的 `conn.execute(...).fetchone()/
    .fetchall()` 寫法,呼叫端（routers/services 47 處）只需要把 SQL 裡的 `?`
    換成 `%s`,不用照著 psycopg2 的「先建 cursor 再 execute」寫法整個重寫。"""

    def __init__(self, conn: "psycopg2.extensions.connection"):
        self._conn = conn

    def execute(self, sql: str, params=()) -> "psycopg2.extras.RealDictCursor":
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


@contextmanager
def get_conn(dsn: str = DATABASE_URL):
    pool = _get_pool()
    raw_conn = pool.getconn()
    conn = _ConnWrapper(raw_conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(raw_conn)


def bump_version(conn: _ConnWrapper) -> None:
    conn.execute("UPDATE db_version SET version = version + 1 WHERE id = 1")


def row_to_dict(row) -> dict:
    return dict(row)


def parse_json_fields(record: dict, fields: list[str]) -> dict:
    for field in fields:
        if field in record and isinstance(record[field], str):
            try:
                record[field] = json.loads(record[field])
            except (json.JSONDecodeError, TypeError):
                record[field] = []
    return record
