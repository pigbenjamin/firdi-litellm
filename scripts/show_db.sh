#!/usr/bin/env bash
# 查看目前 DB（部門、使用者、版本戳記）
# 用法：
#   ./scripts/show_db.sh              # 顯示全部
#   ./scripts/show_db.sh PM           # 只看某部門的使用者
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${USER_AUTH_DB_PATH:-$(dirname "$SCRIPT_DIR")/data/users.db}"

[[ -f "$DB" ]] || { echo "找不到 DB：$DB"; exit 1; }

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
