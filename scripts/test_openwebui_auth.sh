#!/usr/bin/env bash
# Test LiteLLM OpenWebUI dual-mode authentication
# 執行前請先啟動：python3 scripts/mock_openwebui.py
# 以及：docker compose -f docker-compose/docker-compose.yml up -d litellm --no-deps

set -euo pipefail

ENV_FILE="$(dirname "$0")/../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
fi

LITELLM_URL="${LITELLM_URL:-http://localhost:30400}"
SERVICE_KEY="${OPENWEBUI_SERVICE_KEY:-firdi-openwebui-service-key}"
TEST_API_KEY="${TEST_API_KEY:?請在 .env 設定 TEST_API_KEY（dept A 測試使用者 key，見 users.json/DB）}"

PASS=0; FAIL=0
pass() { echo "  ✓ $1"; ((PASS++)) || true; }
fail() { echo "  ✗ $1"; ((FAIL++)) || true; }
section() { echo ""; echo "=== $1 ==="; }

check_status() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$label (HTTP $actual)"
    else
        fail "$label — expected $expected, got $actual"
    fi
}

# ── Wait for litellm ────────────────────────────────────────────────────────
echo "Waiting for LiteLLM at $LITELLM_URL ..."
for i in {1..20}; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/health" 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then break; fi
    echo "  ($i) status=$code, retrying in 3s..."
    sleep 3
done

section "路線一：api_key 模式"

# 有效 api_key（c31f90f3, dept A, fast-qwen, unblocked）
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $TEST_API_KEY")
check_status "有效 api_key → /models" "200" "$code"

# 無效 api_key
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer sk-invalid-key")
check_status "無效 api_key → 401" "401" "$code"

section "路線二：OpenWebUI 模式（SERVICE_KEY + X-OpenWebUI-User-Id）"

# 正常：service key + 有效 openwebui_id（對應 dept A，有 fast-qwen 權限）
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: openwebui-test-user-aaa")
check_status "service key + 有效 openwebui_id → 200" "200" "$code"

# 缺少 X-OpenWebUI-User-Id header
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $SERVICE_KEY")
check_status "service key 但缺少 openwebui header → 401" "401" "$code"

# openwebui_id 不存在於 OpenWebUI mock
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: no-such-openwebui-user")
check_status "openwebui_id 不在 mock → 401" "401" "$code"

# openwebui_id 存在於 OpenWebUI 但無 keycloak sub
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: openwebui-unknown-user")
check_status "openwebui 有 user 但無 oidc.sub → 401" "401" "$code"

# openwebui_id 對應 dept B（allowed_models=[]）的使用者
code=$(curl -s -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: openwebui-test-user-bbb")
check_status "dept B 使用者（無 models）→ /models 仍應通過 auth" "200" "$code"

section "路線二：模型權限驗證"

# dept A 使用者請求 fast-qwen → 應允許（實際轉發失敗也 OK，auth 層通過即可）
resp=$(curl -s -w "\n%{http_code}" "$LITELLM_URL/v1/chat/completions" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: openwebui-test-user-aaa" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast-qwen","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)
code=$(echo "$resp" | tail -1)
# auth 通過後轉發可能 502/500（vllm 沒啟動），但不應是 401/403
if [[ "$code" != "401" && "$code" != "403" ]]; then
    pass "dept A + fast-qwen → auth 通過 (HTTP $code，非 401/403)"
else
    fail "dept A + fast-qwen → auth 拒絕 (HTTP $code)"
fi

# dept A 使用者請求不在清單的模型
resp=$(curl -s -w "\n%{http_code}" "$LITELLM_URL/v1/chat/completions" \
  -H "Authorization: Bearer $SERVICE_KEY" \
  -H "X-OpenWebUI-User-Id: openwebui-test-user-aaa" \
  -H "Content-Type: application/json" \
  -d '{"model":"reasoning-qwen","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)
code=$(echo "$resp" | tail -1)
check_status "dept A 請求 reasoning-qwen（不在 user.models）→ 403" "403" "$code"

# ── 結果 ────────────────────────────────────────────────────────────────────
echo ""
echo "結果：PASS=$PASS  FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] && echo "全部通過 ✓" || { echo "有失敗測試，請檢查 LiteLLM 日誌"; exit 1; }
