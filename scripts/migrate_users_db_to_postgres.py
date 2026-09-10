#!/usr/bin/env python3
"""
一次性將 users-db-pvc(SQLite) 的六張表搬進 firdi_users(Postgres)。

不搬 departments.dept_rpm_limit / dept_tpm_limit——部門層 RPM/TPM 限流已經整個
移除（2026-09，見 config/custom_auth.py），目標 schema 根本沒有這兩個欄位。

用法：
  # 1. 先把正式環境的 users.db 從 litellm Pod 複製出來
  kubectl cp ai-platform/<litellm-pod>:/app/data/users.db /tmp/users.db

  # 2. 跑遷移（USER_AUTH_DATABASE_URL 指向 firdi_users，schema 要先存在——
  #    跑一次 admin-api 的 init_db() 或直接啟動一次 admin-api 就會自動建好）
  USER_AUTH_DATABASE_URL=postgresql://litellm:xxx@postgres-service:5432/firdi_users \
    python3 scripts/migrate_users_db_to_postgres.py --sqlite /tmp/users.db

可重複執行（每張表用主鍵 UPSERT），核對完 row count 才會印「遷移完成」。
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "admin-api"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from database import CREATE_TABLES_SQL, DATABASE_URL  # noqa: E402

# 表格搬遷順序：departments 要先於 users（users.dept_id 有 FK constraint）。
# 每張表列出 (SQLite 來源欄位, Postgres upsert 用的主鍵欄位)。
TABLES = [
    (
        "departments",
        ["dept_id", "dept_name", "openrouter_api_key", "allowed_models",
         "provider_keys", "created_at", "updated_at"],
        ["dept_id"],
    ),
    (
        "users",
        ["api_key", "key_name", "user_id", "user_email", "dept_id", "models",
         "rpm_limit", "tpm_limit", "aliases", "metadata", "blocked",
         "account_type", "created_at", "updated_at"],
        ["api_key"],
    ),
    ("model_key_policies", ["model_name", "key_policy"], ["model_name"]),
    (
        "model_metadata",
        ["model_name", "display_name", "model_type", "cost_center",
         "budget_limit_usd", "budget_enforce", "budget_period",
         "points_per_1k_prompt", "points_per_1k_completion", "notes",
         "status", "upstream", "litellm_model", "api_base", "api_key",
         "last_test_ok", "last_test_at", "last_test_result",
         "created_at", "updated_at"],
        ["model_name"],
    ),
    (
        # dept_id（2026-09 新增，見 admin-api/database.py）不存在於舊版 SQLite
        # schema——這裡舊資料一律搬進 dept_id=''（未分類）這個桶，不是猜的，
        # 是「這批資料本來就沒有部門脈絡」的誠實表達。
        "model_spend",
        ["model_name", "period", "spend_usd", "calls", "updated_at"],
        ["model_name", "dept_id", "period"],
        {"dept_id": ""},
    ),
    (
        "model_presets",
        ["preset_name", "payload", "created_at", "updated_at"],
        ["preset_name"],
    ),
]


def migrate_table(
    sqlite_conn, pg_conn, table: str, columns: list[str], pk: list[str],
    extra: dict[str, object] | None = None,
) -> tuple[int, int]:
    """extra：目標表有、但來源 SQLite 沒有的欄位，一律填同一個固定值（不是從
    SQLite 讀出來的）——目前只有 model_spend 的 dept_id 用得到，見上面 TABLES。
    """
    extra = extra or {}
    src_cols = ", ".join(columns)
    rows = sqlite_conn.execute(f"SELECT {src_cols} FROM {table}").fetchall()

    dest_cols = columns + list(extra.keys())
    dest_cols_sql = ", ".join(dest_cols)
    placeholders = ", ".join(["%s"] * len(dest_cols))
    update_cols = [c for c in dest_cols if c not in pk]
    conflict_target = ", ".join(pk)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    if set_clause:
        upsert_sql = (
            f"INSERT INTO {table} ({dest_cols_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) DO UPDATE SET {set_clause}"
        )
    else:
        upsert_sql = (
            f"INSERT INTO {table} ({dest_cols_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) DO NOTHING"
        )

    extra_values = tuple(extra.values())
    with pg_conn.cursor() as cur:
        for row in rows:
            cur.execute(upsert_sql, tuple(row) + extra_values)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        pg_count = cur.fetchone()[0]
    return len(rows), pg_count


def migrate(sqlite_path: str, postgres_url: str) -> None:
    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(postgres_url)
    with pg_conn.cursor() as cur:
        cur.execute(CREATE_TABLES_SQL)
    pg_conn.commit()

    print(f"來源：{sqlite_path}")
    print(f"目標：{postgres_url.split('@')[-1] if '@' in postgres_url else postgres_url}")
    print()

    mismatches = []
    for entry in TABLES:
        table, columns, pk = entry[0], entry[1], entry[2]
        extra = entry[3] if len(entry) > 3 else None
        src_count, pg_count = migrate_table(sqlite_conn, pg_conn, table, columns, pk, extra)
        status = "OK" if src_count == pg_count else "MISMATCH"
        if status == "MISMATCH":
            mismatches.append(table)
        print(f"  {table:<24} sqlite={src_count:<6} postgres={pg_count:<6} {status}")

    # db_version：延續原本的版本號，不歸零（純粹接續計數，不影響任何邏輯）。
    version_row = sqlite_conn.execute("SELECT version FROM db_version WHERE id=1").fetchone()
    if version_row is not None:
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE db_version SET version = %s WHERE id = 1", (version_row["version"],))
        pg_conn.commit()

    sqlite_conn.close()
    pg_conn.close()

    print()
    if mismatches:
        print(f"遷移完成，但以下表格 row count 對不上，請檢查：{', '.join(mismatches)}")
        sys.exit(1)
    print("遷移完成，六張表 row count 全部核對通過。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate users-db-pvc(SQLite) to firdi_users(Postgres)")
    parser.add_argument("--sqlite", required=True, help="Path to the SQLite users.db file")
    parser.add_argument(
        "--postgres-url",
        default=os.getenv("USER_AUTH_DATABASE_URL", DATABASE_URL),
        help="Target Postgres DSN（預設讀 USER_AUTH_DATABASE_URL 環境變數）",
    )
    args = parser.parse_args()
    migrate(args.sqlite, args.postgres_url)
