#!/usr/bin/env bash
# 查看目前 DB（部門、使用者、版本戳記）
# 用法：
#   ./scripts/show_db.sh              # 從 K8s users-db-pvc（kubectl cp 出 litellm Pod 的 users.db）讀取
#   ./scripts/show_db.sh PM           # 只看某部門的使用者（來源同上）
#   USER_AUTH_DB_PATH=/path/users.db ./scripts/show_db.sh   # 改讀指定的本機 DB 檔案
set -euo pipefail

NS="ai-platform"
TMP_DB=""
cleanup() { [[ -n "$TMP_DB" ]] && rm -f "$TMP_DB"; }
trap cleanup EXIT

if [[ -n "${USER_AUTH_DB_PATH:-}" ]]; then
    DB="$USER_AUTH_DB_PATH"
    [[ -f "$DB" ]] || { echo "找不到 DB：$DB"; exit 1; }
else
    POD=$(kubectl get pod -n "$NS" -l app=litellm -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    [[ -n "$POD" ]] || { echo "找不到 litellm Pod，無法從 users-db-pvc 讀取 users.db（也可以設 USER_AUTH_DB_PATH 指到本機 DB 檔案）"; exit 1; }
    TMP_DB="$(mktemp --suffix=.db)"
    kubectl cp "$NS/$POD:/app/data/users.db" "$TMP_DB" >/dev/null 2>&1 || { echo "kubectl cp 失敗，讀不到 users.db"; exit 1; }
    DB="$TMP_DB"
fi

DEPT_FILTER="${1:-}"

echo "DB：$DB"
echo ""
echo "════════ 部門（含模型權限、rate limit）════════"
sqlite3 -header -column "$DB" "
  SELECT d.dept_id,
         d.allowed_models,
         (SELECT COUNT(*) FROM users u WHERE u.dept_id=d.dept_id AND u.blocked=0) AS active_users,
         d.dept_rpm_limit AS rpm,
         d.dept_tpm_limit AS tpm
  FROM departments d ORDER BY d.dept_id"

echo ""
if [[ -n "$DEPT_FILTER" ]]; then
  echo "════════ 使用者（dept_id = $DEPT_FILTER）════════"
  sqlite3 -header -column "$DB" "
    SELECT user_id, user_email, models, blocked, account_type
    FROM users WHERE dept_id='$DEPT_FILTER' ORDER BY user_email"
else
  echo "════════ 使用者（全部）════════"
  sqlite3 -header -column "$DB" "
    SELECT user_id, user_email, dept_id, models, blocked, account_type
    FROM users ORDER BY dept_id, user_email"
fi

echo ""
echo "════════ 版本戳記（每次權限變更 +1，custom_auth 據此刷新快取）════════"
sqlite3 -column "$DB" "SELECT 'version = ' || version FROM db_version WHERE id=1"
