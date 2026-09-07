#!/usr/bin/env bash
# 模型名稱遷移範本：換模型後，同步 firdi_users（Postgres）裡部門/使用者權限名單中
# 的模型名稱。下面留著上一次實際跑過的例子（Qwen → Gemma）當範本，之後要換新的
# 模型名稱對照時，直接改 sed 那段的字串取代規則即可。
#
#   reasoning-qwen → gemma-4-31B-it
#   fast-qwen      → gemma-4-26B-A4B-it
#   embed-qwen     → embeddinggemma-300m
#   rerank-qwen    → （移除；獨立 rerank 服務已下線，需要時改用 LLM rerank）
#
# 用法：
#   ./scripts/migrate_model_names.sh                          # 透過 kubectl exec 進 postgres Pod 動手
#   USER_AUTH_DATABASE_URL=postgresql://... ./scripts/migrate_model_names.sh   # 改用本機 psql 連指定的 Postgres
#
# 注意：請與新版 litellm_config ConfigMap 一起上線；custom_auth 依 db_version
# 版本戳記失效快取，遷移後 30 秒內全部生效。執行前會自動 pg_dump 備份。
#
# 2026-09 起 users-db-pvc(SQLite) 已遷到 Postgres（見 k8s/postgres/、
# admin-api/database.py），不再是「kubectl cp 出檔案改完再 cp 回去」，改成直接對
# Postgres 下 SQL；過程中 litellm/admin-api 仍在跑，避免跟 Admin API 的寫入操作
# 同時進行。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NS="ai-platform"
DB_NAME="firdi_users"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

run_psql() {
    if [[ -n "${USER_AUTH_DATABASE_URL:-}" ]]; then
        psql "$USER_AUTH_DATABASE_URL" -v ON_ERROR_STOP=1 "$@"
    else
        kubectl exec -i -n "$NS" deploy/postgres -- psql -U litellm -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"
    fi
}

run_pg_dump() {
    if [[ -n "${USER_AUTH_DATABASE_URL:-}" ]]; then
        pg_dump "$USER_AUTH_DATABASE_URL"
    else
        kubectl exec -n "$NS" deploy/postgres -- pg_dump -U litellm -d "$DB_NAME"
    fi
}

BACKUP_DIR="$REPO_ROOT/data"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/firdi_users.pg_dump-$(date +%Y%m%d-%H%M%S).sql"
info "備份 $DB_NAME..."
run_pg_dump > "$BACKUP"
ok "已備份：$BACKUP"

# 先改名，再把 rerank-qwen 從 JSON 陣列移除（依序處理「中/尾、頭、單獨」三種位置與有無空格）
run_psql <<'SQL'
BEGIN;
UPDATE departments SET
    allowed_models = replace(replace(replace(replace(replace(replace(replace(allowed_models,
        '"reasoning-qwen"', '"gemma-4-31B-it"'),
        '"fast-qwen"', '"gemma-4-26B-A4B-it"'),
        '"embed-qwen"', '"embeddinggemma-300m"'),
        ', "rerank-qwen"', ''),
        ',"rerank-qwen"', ''),
        '"rerank-qwen", ', ''),
        '"rerank-qwen"', ''),
    updated_at = now()::text
WHERE allowed_models LIKE '%reasoning-qwen%' OR allowed_models LIKE '%fast-qwen%'
   OR allowed_models LIKE '%embed-qwen%' OR allowed_models LIKE '%rerank-qwen%';

UPDATE users SET
    models = replace(replace(replace(replace(replace(replace(replace(models,
        '"reasoning-qwen"', '"gemma-4-31B-it"'),
        '"fast-qwen"', '"gemma-4-26B-A4B-it"'),
        '"embed-qwen"', '"embeddinggemma-300m"'),
        ', "rerank-qwen"', ''),
        ',"rerank-qwen"', ''),
        '"rerank-qwen", ', ''),
        '"rerank-qwen"', ''),
    updated_at = now()::text
WHERE models LIKE '%reasoning-qwen%' OR models LIKE '%fast-qwen%'
   OR models LIKE '%embed-qwen%' OR models LIKE '%rerank-qwen%';

UPDATE db_version SET version = version + 1 WHERE id = 1;
COMMIT;
SQL

ok "遷移完成。目前部門權限："
run_psql -t -c "SELECT '  ' || dept_id || ' → ' || allowed_models FROM departments;"
ok "db_version = $(run_psql -t -A -c 'SELECT version FROM db_version;')"
ok "custom_auth 30 秒內偵測到 db_version 變化並重載快取"
