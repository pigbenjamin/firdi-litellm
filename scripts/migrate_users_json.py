#!/usr/bin/env python3
"""
一次性將 users.json 匯入 SQLite DB。

用法：
  python scripts/migrate_users_json.py \
    --json config/users.json \
    --db /tmp/users.db
"""
import argparse
import json
import os
import sqlite3

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


def migrate(json_path: str, db_path: str) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(CREATE_TABLES_SQL)

    departments = data.get("departments", [])
    for dept in departments:
        conn.execute(
            """INSERT OR REPLACE INTO departments
               (dept_id, dept_name, openrouter_api_key, allowed_models, dept_rpm_limit, dept_tpm_limit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                dept["dept_id"],
                dept["dept_name"],
                dept.get("openrouter_api_key", ""),
                json.dumps(dept.get("allowed_models", []), ensure_ascii=False),
                dept.get("dept_rpm_limit"),
                dept.get("dept_tpm_limit"),
            ),
        )

    users = data.get("users", [])
    for user in users:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (api_key, key_name, user_id, user_email, dept_id, models,
                rpm_limit, tpm_limit, aliases, metadata, blocked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user["api_key"],
                user.get("key_name", ""),
                user["user_id"],
                user.get("user_email"),
                user["dept_id"],
                json.dumps(user.get("models", []), ensure_ascii=False),
                user.get("rpm_limit"),
                user.get("tpm_limit"),
                json.dumps(user.get("aliases", {}), ensure_ascii=False),
                json.dumps(user.get("metadata", {}), ensure_ascii=False),
                int(user.get("blocked", False)),
            ),
        )

    conn.execute("UPDATE db_version SET version = version + 1 WHERE id = 1")
    conn.commit()
    conn.close()

    print(f"遷移完成：{len(departments)} 部門，{len(users)} 使用者 → {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate users.json to SQLite")
    parser.add_argument("--json", required=True, help="Path to users.json")
    parser.add_argument("--db", required=True, help="Output SQLite DB path")
    args = parser.parse_args()
    migrate(args.json, args.db)
