#!/usr/bin/env bash
# 模型名稱遷移：Qwen → Gemma（換模型後，同步 users.db 的權限名單）
#
#   reasoning-qwen → gemma-4-31B-it
#   fast-qwen      → gemma-4-26B-A4B-it
#   embed-qwen     → embeddinggemma-300m
#   rerank-qwen    → （移除；獨立 rerank 服務已下線，需要時改用 LLM rerank）
#
# 用法：
#   ./scripts/migrate_model_names.sh                  # 對 K8s users-db-pvc 動手（kubectl cp 出→改→回）
#   ./scripts/migrate_model_names.sh /path/users.db   # 直接對指定的本機 DB 檔案動手（docker-compose 等場景）
#
# 注意：請與新版 litellm_config ConfigMap 一起上線；custom_auth 依 db_version
# 版本戳記失效快取，遷移後 30 秒內全部生效。執行前會自動備份 DB。
#
# users-db-pvc 是 K8s 動態佈建的 PVC（不再是 hostPath），本機沒有檔案能直接改到
# 正式資料，預設模式改成：kubectl cp 把 DB 從 litellm Pod 複製出來 → 本機跑 SQL
# → 再 cp 回去。過程中 litellm/admin-api 仍在跑，屬於外部覆蓋檔案內容，跟過去改
# hostPath 檔案的風險等級相同（沒有額外鎖機制），避免跟 Admin API 的寫入操作同時進行。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NS="ai-platform"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

command -v sqlite3 >/dev/null || die "需要 sqlite3"

REMOTE_MODE=0
POD=""
if [[ $# -ge 1 ]]; then
    DB="$1"
    [[ -f "$DB" ]] || die "找不到 DB：$DB"
else
    REMOTE_MODE=1
    POD=$(kubectl get pod -n "$NS" -l app=litellm -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    [[ -n "$POD" ]] || die "找不到 litellm Pod，無法從 users-db-pvc 取得 users.db（也可以手動指定本機 DB 檔案路徑當參數）"
    DB="$(mktemp --suffix=.db)"
    info "從 litellm Pod（$POD）取出 users.db..."
    kubectl cp "$NS/$POD:/app/data/users.db" "$DB"
fi

BACKUP_DIR="$REPO_ROOT/data"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/users.db.bak-$(date +%Y%m%d-%H%M%S)"
cp "$DB" "$BACKUP"
info "已備份：$BACKUP"

sqlite3 "$DB" <<'SQL'
BEGIN;
-- 先改名，再把 rerank-qwen 從 JSON 陣列移除（依序處理「中/尾、頭、單獨」三種位置與有無空格）
UPDATE departments SET
    allowed_models = replace(replace(replace(replace(replace(replace(replace(allowed_models,
        '"reasoning-qwen"', '"gemma-4-31B-it"'),
        '"fast-qwen"', '"gemma-4-26B-A4B-it"'),
        '"embed-qwen"', '"embeddinggemma-300m"'),
        ', "rerank-qwen"', ''),
        ',"rerank-qwen"', ''),
        '"rerank-qwen", ', ''),
        '"rerank-qwen"', ''),
    updated_at = datetime('now')
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
    updated_at = datetime('now')
WHERE models LIKE '%reasoning-qwen%' OR models LIKE '%fast-qwen%'
   OR models LIKE '%embed-qwen%' OR models LIKE '%rerank-qwen%';

UPDATE db_version SET version = version + 1 WHERE id = 1;
COMMIT;
SQL

ok "遷移完成。目前部門權限："
sqlite3 "$DB" "SELECT '  ' || dept_id || ' → ' || allowed_models FROM departments;"
ok "db_version = $(sqlite3 "$DB" 'SELECT version FROM db_version;')"

if [[ "$REMOTE_MODE" -eq 1 ]]; then
    info "寫回 litellm Pod（$POD）..."
    kubectl cp "$DB" "$NS/$POD:/app/data/users.db"
    rm -f "$DB"
    ok "users-db-pvc 已更新（custom_auth 30 秒內偵測到 db_version 變化並重載快取）"
fi
