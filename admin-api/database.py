import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("USER_AUTH_DB_PATH", "/app/data/users.db")

CREATE_TABLES_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS departments (
    dept_id            TEXT PRIMARY KEY,
    dept_name          TEXT NOT NULL,
    openrouter_api_key TEXT NOT NULL DEFAULT '',
    allowed_models     TEXT NOT NULL DEFAULT '[]',
    dept_rpm_limit     INTEGER,
    dept_tpm_limit     INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 決策 E（見 docs/admin-web-plan.md）：模型的上游（litellm_params.model）與
-- key 從哪來（部門 provider key 或模型自帶）解耦。key_policy 是
-- "model"（用模型自己定義的 key）或 "dept:<provider>"（用部門 provider_keys
-- 裡該 provider 的 key，例如 "dept:openai"）。用 model_name（呼叫者請求時填的
-- 名字）當主鍵，因為 custom_auth 熱路徑上只看得到這個字串，看不到 LiteLLM
-- 內部的 deployment id。沒有紀錄的 model_name 由 custom_auth 自行推導預設值
-- （openrouter/ 開頭 → dept:openrouter，其餘 → model），不需要為既有模型補資料。
CREATE TABLE IF NOT EXISTS model_key_policies (
    model_name TEXT PRIMARY KEY,
    key_policy TEXT NOT NULL
);

-- 模型的管理面欄位（WP1/WP2）。刻意不塞進 LiteLLM 的 model_info，理由跟決策 E
-- 的 model_key_policies 一樣：custom_auth 在每個請求的熱路徑上讀的是這顆 SQLite，
-- 讀 LiteLLM 的 model_info 等於在熱路徑多一個 Postgres 相依。
--
-- 用 model_name 當主鍵而不是 LiteLLM 的 deployment id：id 在「草稿改設定＝刪除
-- 重建」與「停用＝從 LiteLLM 刪掉、啟用＝重新註冊」之後都會換一個新的，只有
-- model_name 從頭到尾不變，而且 model_name 正是授權（allowed_models / users.models）
-- 與 OpenWebUI access_grants 認的那個字串。
--
-- 沒有紀錄的 model_name 一律視為 status='published' 的既有模型（見
-- services/model_metadata_service.py 的 DEFAULTS），既有模型不需要任何資料回填。
--
-- status：
--   draft     已註冊到 LiteLLM（才測得起來）但 custom_auth 會擋掉一般使用者，
--             routing 欄位可改（實作是刪除重建）；要 last_test_ok=1 才能發布。
--   published 使用者可用；routing 欄位鎖定，描述性欄位（顯示名稱/備註/成本歸屬/
--             額度）仍可改。
--   disabled  已從 LiteLLM 刪除（使用者打不到、OpenWebUI 清單也看不到），但這筆
--             設定完整保留，可一鍵重新註冊。
--
-- upstream/litellm_model/api_base/api_key 是「重新註冊時要用的原始參數」——
-- LiteLLM 的 /model/info 會遮罩 api_key，撈不回來，停用後要能原樣重建就只能自己留。
-- 跟 departments.provider_keys、users.api_key 同一顆 DB、同樣是明文欄位；UI 與
-- 稽核紀錄一律只顯示末四碼（見 audit.mask_key）。
CREATE TABLE IF NOT EXISTS model_metadata (
    model_name       TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    model_type       TEXT NOT NULL DEFAULT 'chat',      -- chat | embedding | rerank
    cost_center      TEXT NOT NULL DEFAULT '',          -- 成本歸屬部門 dept_id，可留空
    budget_limit_usd REAL,                              -- NULL = 沒設額度
    budget_enforce   INTEGER NOT NULL DEFAULT 0,        -- 0=只記錄不擋 1=超額真的擋下來
    budget_period    TEXT NOT NULL DEFAULT 'monthly',   -- monthly | total
    -- 點數費率（每 1K token 幾點，可填小數）。這裡**只存不算**：扣點與部門／人員
    -- 的點數上限一律由外部系統處理，本平台不累計、不檢查、不擋（config/custom_auth.py
    -- 與 config/custom_logger.py 完全不看這兩個欄位）。外部系統要算點數的話，費率從
    -- GET /api/v1/models/external 的 meta 讀，token 數從 usage.jsonl 的 prompt_tokens /
    -- completion_tokens 讀。
    -- NULL 而不是 0 表示「還沒填」——0 在外部系統眼裡是「這個模型免費」，差很多。
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
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 用量累計（WP1 的額度強制）。LiteLLM 自己的 spend tracking 在本專案是刻意關掉的
-- （litellm_config.yaml 的 disable_spend_logs / disable_spend_updates——用量記錄
-- 走 custom_logger 的 jsonl + Langfuse，那顆 Postgres 只存模型定義），所以
-- LiteLLM 內建的 budget 機制在這裡沒有資料可用、根本不會生效。要「真的擋得下來」
-- 就只能自己累計：config/custom_logger.py 每次成功呼叫把 response_cost 加進來，
-- config/custom_auth.py 在認證時比對 model_metadata 的額度設定。
--
-- period 是 'YYYY-MM'（UTC，budget_period='monthly'）或 'total'（budget_period='total'）。
-- 兩種都會累計，換設定不會遺失歷史。
CREATE TABLE IF NOT EXISTS model_spend (
    model_name TEXT NOT NULL,
    period     TEXT NOT NULL,
    spend_usd  REAL NOT NULL DEFAULT 0,
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (model_name, period)
);

-- 上架表單的「常用範本」（WP1）：把填過一次的表單欄位存起來，下次選範本直接帶入。
CREATE TABLE IF NOT EXISTS model_presets (
    preset_name TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,   -- JSON，上架表單的欄位值
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_dept_id ON users(dept_id);

CREATE TABLE IF NOT EXISTS db_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO db_version VALUES (1, 0);
"""


def init_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(CREATE_TABLES_SQL)
    # 欄位 migration：對已存在的 DB 補上 account_type 欄位
    try:
        conn.execute("ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'human'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 欄位已存在，略過

    # 點數費率欄位（只存不算，見 CREATE TABLE 的說明）。純新增、零回填——既有模型
    # 兩個欄位都是 NULL，代表「還沒填費率」。
    for col in ("points_per_1k_prompt", "points_per_1k_completion"):
        try:
            conn.execute(f"ALTER TABLE model_metadata ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 欄位已存在，略過

    # 決策 E：departments.openrouter_api_key 升級成 provider_keys（JSON，key 為
    # provider 名稱）。純超集、零遷移——舊欄位不動、不刪，新欄位補上後從舊欄位
    # 回填一次，之後兩者由 service 層保持同步（見 services/departments_service.py）。
    try:
        conn.execute("ALTER TABLE departments ADD COLUMN provider_keys TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 欄位已存在，略過
    else:
        rows = conn.execute(
            "SELECT dept_id, openrouter_api_key FROM departments WHERE provider_keys = '{}' AND openrouter_api_key != ''"
        ).fetchall()
        for dept_id, openrouter_api_key in rows:
            conn.execute(
                "UPDATE departments SET provider_keys = ? WHERE dept_id = ?",
                (json.dumps({"openrouter": openrouter_api_key}, ensure_ascii=False), dept_id),
            )
        conn.commit()
    conn.close()


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bump_version(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE db_version SET version = version + 1 WHERE id = 1")


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def parse_json_fields(record: dict, fields: list[str]) -> dict:
    for field in fields:
        if field in record and isinstance(record[field], str):
            try:
                record[field] = json.loads(record[field])
            except (json.JSONDecodeError, TypeError):
                record[field] = []
    return record
