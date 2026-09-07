#!/usr/bin/env bash
# 查看目前 DB（部門、使用者、版本戳記）
# 2026-09 起 users-db-pvc(SQLite) 已遷到 Postgres 的 firdi_users database（見
# k8s/postgres/、admin-api/database.py），改用 psql 查。
# 用法：
#   ./scripts/show_db.sh              # 透過 kubectl exec 進 postgres Pod 查
#   ./scripts/show_db.sh PM           # 只看某部門的使用者（來源同上）
#   USER_AUTH_DATABASE_URL=postgresql://... ./scripts/show_db.sh   # 改用本機 psql 連指定的 Postgres
set -euo pipefail

NS="ai-platform"
DEPT_FILTER="${1:-}"

run_psql() {
    if [[ -n "${USER_AUTH_DATABASE_URL:-}" ]]; then
        psql "$USER_AUTH_DATABASE_URL" -v ON_ERROR_STOP=1 -c "$1"
    else
        kubectl exec -n "$NS" deploy/postgres -- psql -U litellm -d firdi_users -v ON_ERROR_STOP=1 -c "$1"
    fi
}

echo "════════ 部門（含模型權限）════════"
run_psql "
  SELECT d.dept_id,
         d.allowed_models,
         (SELECT COUNT(*) FROM users u WHERE u.dept_id=d.dept_id AND u.blocked=0) AS active_users
  FROM departments d ORDER BY d.dept_id"

echo ""
if [[ -n "$DEPT_FILTER" ]]; then
  echo "════════ 使用者（dept_id = $DEPT_FILTER）════════"
  run_psql "
    SELECT user_id, user_email, models, blocked, account_type
    FROM users WHERE dept_id='$DEPT_FILTER' ORDER BY user_email"
else
  echo "════════ 使用者（全部）════════"
  run_psql "
    SELECT user_id, user_email, dept_id, models, blocked, account_type
    FROM users ORDER BY dept_id, user_email"
fi

echo ""
echo "════════ 版本戳記（每次權限變更 +1，custom_auth 據此刷新快取）════════"
run_psql "SELECT 'version = ' || version FROM db_version WHERE id=1"
