#!/usr/bin/env bash
# 模型名稱遷移：Qwen → Gemma 4（fast/reasoning 換模型後，同步 users.db 的權限名單）
#
#   reasoning-qwen → gemma-4-31B-it
#   fast-qwen      → gemma-4-26B-A4B-it
#
# 用法：
#   ./scripts/migrate_model_names.sh                  # 用 .env 的 K8S_DATA_HOST_PATH/users.db
#   ./scripts/migrate_model_names.sh /path/users.db   # 指定 DB 路徑
#
# 注意：請與新版 litellm_config ConfigMap 一起上線；custom_auth 依 db_version
# 版本戳記失效快取，遷移後 30 秒內全部生效。執行前會自動備份 DB。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

if [[ $# -ge 1 ]]; then
    DB="$1"
else
    if [[ -f "$REPO_ROOT/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "$REPO_ROOT/.env"
        set +a
    fi
    DB="${K8S_DATA_HOST_PATH:-$REPO_ROOT/data}/users.db"
fi

[[ -f "$DB" ]] || die "找不到 DB：$DB"
command -v sqlite3 >/dev/null || die "需要 sqlite3"

BACKUP="${DB}.bak-$(date +%Y%m%d-%H%M%S)"
cp "$DB" "$BACKUP"
info "已備份：$BACKUP"

sqlite3 "$DB" <<'SQL'
BEGIN;
UPDATE departments SET
    allowed_models = replace(replace(allowed_models,
        '"reasoning-qwen"', '"gemma-4-31B-it"'),
        '"fast-qwen"', '"gemma-4-26B-A4B-it"'),
    updated_at = datetime('now')
WHERE allowed_models LIKE '%reasoning-qwen%' OR allowed_models LIKE '%fast-qwen%';

UPDATE users SET
    models = replace(replace(models,
        '"reasoning-qwen"', '"gemma-4-31B-it"'),
        '"fast-qwen"', '"gemma-4-26B-A4B-it"'),
    updated_at = datetime('now')
WHERE models LIKE '%reasoning-qwen%' OR models LIKE '%fast-qwen%';

UPDATE db_version SET version = version + 1 WHERE id = 1;
COMMIT;
SQL

ok "遷移完成。目前部門權限："
sqlite3 "$DB" "SELECT '  ' || dept_id || ' → ' || allowed_models FROM departments;"
ok "db_version = $(sqlite3 "$DB" 'SELECT version FROM db_version;')"
