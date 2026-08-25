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
