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
