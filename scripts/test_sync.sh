#!/usr/bin/env bash
# test_sync.sh — Keycloak → admin-api 使用者同步整合測試
#
# 涵蓋：
#   T1  Keycloak Client Credentials 連線
#   T2  Webhook Secret 驗證（拒絕未授權請求）
#   T3  缺少 user_id 的錯誤處理
#   T4  部門不存在時略過（skipped）
#   T5  新使用者 Provision（自動產生 api_key、繼承部門 model）
#   T6  更新現有使用者（api_key 不變、資料更新）
#   T7  DELETE 事件封鎖使用者（blocked=true，資料保留）
#   T8  端對端整合（Keycloak 直接觸發 → admin-api 收到 webhook）
#
# 用法：
#   ./scripts/test_sync.sh
#   ./scripts/test_sync.sh --admin-host localhost:8080
#   ./scripts/test_sync.sh --suite unit          # T1-T3（不需要真實 Keycloak user）
#   ./scripts/test_sync.sh --suite provision     # T4-T7（需要 --user-id）
#   ./scripts/test_sync.sh --user-id <UUID>      # 指定 Keycloak user UUID
#   ./scripts/test_sync.sh --no-cleanup          # 測試後保留建立的資料

set -euo pipefail

# ── 顏色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0
LAST_RESPONSE=""

pass()    { echo -e "${GREEN}  ✓ PASS${NC} $*"; ((++PASS)); }
fail()    { echo -e "${RED}  ✗ FAIL${NC} $*"; ((++FAIL)); }
section() { echo -e "\n${CYAN}${BOLD}── $* ──────────────────────────────────${NC}"; }
info()    { echo -e "${YELLOW}  →${NC} $*"; }
skip()    { echo -e "${YELLOW}  ⊘ SKIP${NC} $*"; }

# ── 預設值 ────────────────────────────────────────────────────────────────────
ADMIN_HOST=""
SUITE="all"
TEST_USER_ID=""
DO_CLEANUP=true

# ── 讀取 .env ─────────────────────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
fi

WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
KEYCLOAK_URL="${KEYCLOAK_URL:-}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-}"
KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"
ADMIN_API_KEY="${ADMIN_API_KEY:-}"

# ── 解析參數 ──────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --admin-host) ADMIN_HOST="$2"; shift 2 ;;
        --suite)      SUITE="$2";      shift 2 ;;
        --user-id)    TEST_USER_ID="$2"; shift 2 ;;
        --no-cleanup) DO_CLEANUP=false; shift ;;
        *) echo "未知參數: $1"; exit 1 ;;
    esac
done

# ── 自動偵測 admin-api host ────────────────────────────────────────────────────
if [[ -z "$ADMIN_HOST" ]]; then
    NODE_IP=$(kubectl get nodes \
        -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' \
        2>/dev/null || true)
    if [[ -n "$NODE_IP" ]]; then
        ADMIN_HOST="${NODE_IP}:30408"
        info "自動偵測 NodeIP → Admin API: $ADMIN_HOST"
    else
        ADMIN_HOST="localhost:8080"
        info "使用預設 Admin API: $ADMIN_HOST"
    fi
fi

ADMIN_URL="http://${ADMIN_HOST}"
SYNC_URL="${ADMIN_URL}/api/v1/sync/keycloak"

# ── HTTP 工具函式 ──────────────────────────────────────────────────────────────
call_sync() {
    local secret="$1"
    local body="$2"
    local header_arg=""
    [[ -n "$secret" ]] && header_arg="-H \"X-Webhook-Secret: ${secret}\""

    LAST_RESPONSE=$(eval curl -s -o /tmp/sync_resp -w "%{http_code}" \
        -X POST "${SYNC_URL}" \
        -H "Content-Type: application/json" \
        ${header_arg} \
        -d "'${body}'" 2>/dev/null)
    LAST_BODY=$(cat /tmp/sync_resp 2>/dev/null || echo "")
}

call_admin() {
    local method="$1"; local path="$2"; local body="${3:-}"
    local data_arg=""
    [[ -n "$body" ]] && data_arg="-d '${body}'"

    LAST_RESPONSE=$(eval curl -s -o /tmp/admin_resp -w "%{http_code}" \
        -X "${method}" "${ADMIN_URL}${path}" \
        -H "Authorization: Bearer ${ADMIN_API_KEY}" \
        -H "Content-Type: application/json" \
        ${data_arg} 2>/dev/null)
    LAST_BODY=$(cat /tmp/admin_resp 2>/dev/null || echo "")
}

assert_status() {
    local expected="$1"; local label="$2"
    if [[ "$LAST_RESPONSE" == "$expected" ]]; then
        pass "$label (HTTP $expected)"
    else
        fail "$label — 期待 HTTP $expected，實際 HTTP $LAST_RESPONSE"
        info "body: $LAST_BODY"
    fi
}

assert_json_field() {
    local field="$1"; local expected="$2"; local label="$3"
    local actual
    actual=$(echo "$LAST_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null || echo "")
    if [[ "$actual" == "$expected" ]]; then
        pass "$label (.${field}=\"${expected}\")"
    else
        fail "$label — 期待 .${field}=\"${expected}\"，實際 \"${actual}\""
        info "body: $LAST_BODY"
    fi
}

assert_json_field_nonempty() {
    local field="$1"; local label="$2"
    local actual
    actual=$(echo "$LAST_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null || echo "")
    if [[ -n "$actual" && "$actual" != "None" ]]; then
        pass "$label (.${field} 不為空: \"${actual}\")"
    else
        fail "$label — .${field} 為空"
        info "body: $LAST_BODY"
    fi
}

# ── 測試部門 ID（用於 provision 測試）────────────────────────────────────────
TEST_DEPT_ID="test-sync-dept-$$"
TEST_DEPT_CREATED=false

setup_test_dept() {
    call_admin POST "/api/v1/departments" \
        "{\"dept_id\":\"${TEST_DEPT_ID}\",\"dept_name\":\"Sync Test Dept\",\"allowed_models\":[\"fast-qwen\"],\"dept_rpm_limit\":60,\"dept_tpm_limit\":100000}"
    if [[ "$LAST_RESPONSE" == "201" ]]; then
        TEST_DEPT_CREATED=true
        info "建立測試部門 ${TEST_DEPT_ID}"
    else
        info "警告：無法建立測試部門（${LAST_RESPONSE}），T4/T5/T6/T7 可能失敗"
    fi
}

cleanup() {
    if [[ "$DO_CLEANUP" == "true" && "$TEST_DEPT_CREATED" == "true" ]]; then
        info "清理測試部門 ${TEST_DEPT_ID}..."
        call_admin DELETE "/api/v1/departments/${TEST_DEPT_ID}" || true
    fi
}
trap cleanup EXIT

# ═══════════════════════════════════════════════════════════════════════════════
echo -e "\n${BOLD}Keycloak → admin-api 使用者同步測試${NC}"
echo -e "  Admin API : ${ADMIN_URL}"
echo -e "  Suite     : ${SUITE}"
[[ -n "$TEST_USER_ID" ]] && echo -e "  User ID   : ${TEST_USER_ID}"

# ──────────────────────────────────────────────────────────────────────────────
# T1：Keycloak Client Credentials 連線
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$SUITE" == "all" || "$SUITE" == "unit" || "$SUITE" == "keycloak" ]]; then
    section "T1  Keycloak Client Credentials 連線"

    if [[ -z "$KEYCLOAK_URL" || -z "$KEYCLOAK_CLIENT_SECRET" ]]; then
        skip "T1 — 未設定 KEYCLOAK_URL / KEYCLOAK_CLIENT_SECRET，略過"
    else
        TOKEN_RESP=$(curl -sk -o /tmp/kc_token -w "%{http_code}" \
            -X POST "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            -d "grant_type=client_credentials" \
            -d "client_id=${KEYCLOAK_CLIENT_ID}" \
            -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" 2>/dev/null)
        TOKEN_BODY=$(cat /tmp/kc_token 2>/dev/null || echo "")

        if [[ "$TOKEN_RESP" == "200" ]]; then
            ACCESS_TOKEN=$(echo "$TOKEN_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token','')[:20])" 2>/dev/null || echo "")
            pass "T1-1 取得 access_token（開頭：${ACCESS_TOKEN}...）"
        else
            fail "T1-1 取得 access_token — HTTP ${TOKEN_RESP}"
            info "body: $TOKEN_BODY"
        fi
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# T2：Webhook Secret 驗證
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$SUITE" == "all" || "$SUITE" == "unit" ]]; then
    section "T2  Webhook Secret 驗證"

    call_sync "" '{"user_id":"test","event_type":"UPDATE"}'
    assert_status "401" "T2-1 無 secret 應回 401"

    call_sync "wrong-secret" '{"user_id":"test","event_type":"UPDATE"}'
    assert_status "401" "T2-2 錯誤 secret 應回 401"

    info "T2-3 正確 secret 測試（若 Keycloak 無法連線則 502）"
    call_sync "$WEBHOOK_SECRET" '{"user_id":"00000000-0000-0000-0000-000000000000","event_type":"UPDATE"}'
    if [[ "$LAST_RESPONSE" == "200" || "$LAST_RESPONSE" == "502" ]]; then
        pass "T2-3 正確 secret 通過驗證（HTTP ${LAST_RESPONSE}）"
    else
        fail "T2-3 正確 secret 應回 200 或 502，實際 ${LAST_RESPONSE}"
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# T3：缺少 user_id 的錯誤處理
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$SUITE" == "all" || "$SUITE" == "unit" ]]; then
    section "T3  缺少 user_id 的錯誤處理"

    call_sync "$WEBHOOK_SECRET" '{"event_type":"UPDATE"}'
    assert_status "422" "T3-1 缺少 user_id 應回 422"

    call_sync "$WEBHOOK_SECRET" '{"user_id":"","event_type":"UPDATE"}'
    assert_status "422" "T3-2 空字串 user_id 應回 422"
fi

# ──────────────────────────────────────────────────────────────────────────────
# T4-T7：需要真實 Keycloak user UUID
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$SUITE" == "all" || "$SUITE" == "provision" ]]; then
    if [[ -z "$TEST_USER_ID" ]]; then
        section "T4-T7  Provision / Update / Delete 測試"
        skip "需要 --user-id <Keycloak UUID>，略過 T4-T7"
    else
        setup_test_dept

        # ── T4：部門不存在時略過 ──────────────────────────────────────────────
        section "T4  部門不存在時略過"
        call_sync "$WEBHOOK_SECRET" \
            "{\"user_id\":\"${TEST_USER_ID}\",\"event_type\":\"UPDATE\",\"source\":\"admin_event\"}"

        # 使用者所屬部門可能存在也可能不存在，只驗證不會 crash
        if [[ "$LAST_RESPONSE" == "200" ]]; then
            STATUS=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            if [[ "$STATUS" == "skipped" ]]; then
                pass "T4-1 部門不存在時回傳 skipped"
            elif [[ "$STATUS" == "created" || "$STATUS" == "updated" ]]; then
                info "T4-1 使用者所屬部門已存在，跳過 skipped 驗證（status=${STATUS}）"
            else
                fail "T4-1 期待 status=skipped 或 created，實際 ${STATUS}"
            fi
        else
            fail "T4-1 應回 HTTP 200，實際 ${LAST_RESPONSE}"
            info "body: $LAST_BODY"
        fi

        # ── T5：新使用者 Provision ────────────────────────────────────────────
        section "T5  新使用者 Provision"

        # 先確保使用者不存在（可能已從 T4 建立，先刪掉重來）
        call_admin GET "/api/v1/users/${TEST_USER_ID}" || true
        if [[ "$LAST_RESPONSE" == "200" ]]; then
            call_admin DELETE "/api/v1/users/${TEST_USER_ID}" || true
            info "清除已存在的測試使用者"
        fi

        call_sync "$WEBHOOK_SECRET" \
            "{\"user_id\":\"${TEST_USER_ID}\",\"event_type\":\"CREATE\",\"source\":\"admin_event\"}"

        if [[ "$LAST_RESPONSE" == "200" ]]; then
            STATUS=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            if [[ "$STATUS" == "created" ]]; then
                pass "T5-1 新使用者建立成功"
                assert_json_field_nonempty "api_key" "T5-2 自動產生 api_key"

                # 從 admin-api 驗證使用者資料
                CREATED_USER=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null || echo "")
                call_admin GET "/api/v1/users/${CREATED_USER}"
                if [[ "$LAST_RESPONSE" == "200" ]]; then
                    MODELS=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('models',''))" 2>/dev/null || echo "")
                    pass "T5-3 使用者已存入 SQLite"
                    info "models: ${MODELS}"
                else
                    fail "T5-3 查詢使用者失敗 HTTP ${LAST_RESPONSE}"
                fi
            elif [[ "$STATUS" == "skipped" ]]; then
                REASON=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || echo "")
                skip "T5 — ${REASON}"
            else
                fail "T5-1 期待 status=created，實際 ${STATUS}"
                info "body: $LAST_BODY"
            fi
        else
            fail "T5 — HTTP ${LAST_RESPONSE}"
            info "body: $LAST_BODY"
        fi

        # ── T6：更新現有使用者 ────────────────────────────────────────────────
        section "T6  更新現有使用者（api_key 不變）"

        # 先取得原本的 api_key
        call_admin GET "/api/v1/users/${TEST_USER_ID}"
        ORIGINAL_KEY=""
        if [[ "$LAST_RESPONSE" == "200" ]]; then
            ORIGINAL_KEY=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || echo "")
            info "原始 api_key: ${ORIGINAL_KEY}"
        fi

        call_sync "$WEBHOOK_SECRET" \
            "{\"user_id\":\"${TEST_USER_ID}\",\"event_type\":\"UPDATE\",\"source\":\"admin_event\"}"
        assert_status "200" "T6-1 再次 sync 應回 200"
        assert_json_field "status" "updated" "T6-2 status 應為 updated"

        if [[ -n "$ORIGINAL_KEY" ]]; then
            call_admin GET "/api/v1/users/${TEST_USER_ID}"
            NEW_KEY=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || echo "")
            if [[ "$ORIGINAL_KEY" == "$NEW_KEY" ]]; then
                pass "T6-3 api_key 未改變"
            else
                fail "T6-3 api_key 不應改變（原：${ORIGINAL_KEY}，現：${NEW_KEY}）"
            fi
        fi

        # ── T7：DELETE 事件封鎖使用者 ─────────────────────────────────────────
        section "T7  DELETE 事件封鎖使用者"

        call_sync "$WEBHOOK_SECRET" \
            "{\"user_id\":\"${TEST_USER_ID}\",\"event_type\":\"DELETE\",\"source\":\"admin_event\"}"
        assert_status "200" "T7-1 DELETE 事件應回 200"
        assert_json_field "status" "blocked" "T7-2 status 應為 blocked"

        call_admin GET "/api/v1/users/${TEST_USER_ID}"
        if [[ "$LAST_RESPONSE" == "200" ]]; then
            BLOCKED=$(echo "$LAST_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('blocked',''))" 2>/dev/null || echo "")
            if [[ "$BLOCKED" == "True" ]]; then
                pass "T7-3 使用者已標記為 blocked=true"
            else
                fail "T7-3 blocked 應為 true，實際 ${BLOCKED}"
            fi
        else
            fail "T7-3 使用者應仍存在（軟刪除），HTTP ${LAST_RESPONSE}"
        fi

        # 清理：刪除測試使用者
        if [[ "$DO_CLEANUP" == "true" ]]; then
            call_admin DELETE "/api/v1/users/${TEST_USER_ID}" || true
            info "清理測試使用者 ${TEST_USER_ID}"
        fi
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# T8：端對端整合提示
# ──────────────────────────────────────────────────────────────────────────────
if [[ "$SUITE" == "all" || "$SUITE" == "e2e" ]]; then
    section "T8  端對端整合測試（手動步驟）"
    echo -e "  1. 在 Keycloak Admin Console 修改一個使用者（例如改 email）"
    echo -e "  2. 觀察 admin-api log："
    echo -e "     ${CYAN}kubectl logs -f deployment/admin-api -n ai-platform | grep sync${NC}"
    echo -e "  3. 查詢使用者確認資料已更新："
    echo -e "     ${CYAN}curl -s ${ADMIN_URL}/api/v1/users/<user_id> -H 'Authorization: Bearer ${ADMIN_API_KEY}' | python3 -m json.tool${NC}"
fi

# ── 結果 ──────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}────────────────────────────────────────${NC}"
echo -e "  ${GREEN}PASS: ${PASS}${NC}   ${RED}FAIL: ${FAIL}${NC}"
echo -e "${BOLD}────────────────────────────────────────${NC}\n"

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
