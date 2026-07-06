#!/usr/bin/env bash
# test_admin.sh — Admin API + Model 權限整合測試
#
# 涵蓋：部門 CRUD (D1-D7)、使用者 CRUD (U1-U14)
#       Model 權限驗證 (M1-M8)、邊界情況 (E1-E5)
#
# 用法：
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --suite dept
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --suite user
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --suite perms
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --suite edge
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --host localhost:4000 --admin-host localhost:8080
#   ADMIN_API_KEY=xxx ./scripts/test_admin.sh --no-cleanup

set -euo pipefail

# ── 顏色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0
LAST_RESPONSE=""  # call_admin 執行後保存最後一次 response body

pass()    { echo -e "${GREEN}  ✓ PASS${NC} $*"; ((++PASS)); }
fail()    { echo -e "${RED}  ✗ FAIL${NC} $*"; ((++FAIL)); }
section() { echo -e "\n${CYAN}${BOLD}── $* ──────────────────────────────────${NC}"; }
info()    { echo -e "${YELLOW}  →${NC} $*"; }

# ── 預設值 ────────────────────────────────────────────────────────────────────
HOST=""
ADMIN_HOST=""
ADMIN_KEY="${ADMIN_API_KEY:-}"
SUITE="all"
DO_CLEANUP=true
LLM_ALIVE=false

# ── 解析參數 ──────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)        HOST="$2";       shift 2 ;;
        --admin-host)  ADMIN_HOST="$2"; shift 2 ;;
        --admin-key)   ADMIN_KEY="$2";  shift 2 ;;
        --suite)       SUITE="$2";      shift 2 ;;
        --no-cleanup)  DO_CLEANUP=false; shift ;;
        *) echo "未知參數: $1"; exit 1 ;;
    esac
done

# ── 自動偵測 NodeIP ────────────────────────────────────────────────────────────
if [[ -z "$HOST" ]]; then
    NODE_IP=$(kubectl get nodes \
        -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' \
        2>/dev/null || true)
    if [[ -z "$NODE_IP" ]]; then
        echo -e "${RED}無法取得 NodeIP，請用 --host 指定${NC}"
        exit 1
    fi
    HOST="${NODE_IP}:30400"
    info "自動偵測 NodeIP → LiteLLM: $HOST"
fi

if [[ -z "$ADMIN_HOST" ]]; then
    BASE_IP="${HOST%%:*}"
    ADMIN_HOST="${BASE_IP}:30408"
    info "Admin API: $ADMIN_HOST"
fi

if [[ -z "$ADMIN_KEY" ]]; then
    echo -e "${RED}錯誤：請設定 ADMIN_API_KEY 環境變數或 --admin-key 參數${NC}"
    exit 1
fi

LLM_URL="http://$HOST"
ADMIN_URL="http://$ADMIN_HOST"

# ── Helpers ───────────────────────────────────────────────────────────────────

# call_admin <METHOD> <PATH> <BODY|""> <EXPECT_STATUS> <DESC>
call_admin() {
    local method="$1" path="$2" body="$3" expect="$4" desc="$5"

    local args=(-s -w "\n__STATUS__%{http_code}"
                -X "$method"
                -H "Authorization: Bearer $ADMIN_KEY"
                -H "Content-Type: application/json"
                --max-time 15)
    [[ -n "$body" ]] && args+=(-d "$body")

    local resp status body_out
    resp=$(curl "${args[@]}" "$ADMIN_URL$path" 2>/dev/null || echo "__STATUS__000")
    status=$(echo "$resp" | tail -1 | sed 's/__STATUS__//')
    body_out=$(echo "$resp" | head -n -1)

    LAST_RESPONSE="$body_out"
    if [[ "$status" == "$expect" ]]; then
        pass "$desc [HTTP $status]"
    else
        fail "$desc [期望 $expect，實際 $status]"
        echo "       $(echo "$body_out" | head -c 300)"
    fi
}

# _check_resp <desc> <python_expr>
# python_expr 讀 stdin（JSON），print "ok" 表示通過，其他字串表示失敗原因
_check_resp() {
    local desc="$1" pyexpr="$2"
    local result
    result=$(echo "$LAST_RESPONSE" | python3 -c "$pyexpr" 2>/dev/null || echo "__ERR__")
    if [[ "$result" == "ok" ]]; then
        pass "$desc"
    else
        fail "$desc [$result]"
    fi
}

# call_admin_silent <METHOD> <PATH> <BODY|""> → echoes HTTP status code only
call_admin_silent() {
    local method="$1" path="$2" body="${3:-}"
    local args=(-s -o /dev/null -w "%{http_code}"
                -X "$method"
                -H "Authorization: Bearer $ADMIN_KEY"
                -H "Content-Type: application/json"
                --max-time 15)
    [[ -n "$body" ]] && args+=(-d "$body")
    curl "${args[@]}" "$ADMIN_URL$path" 2>/dev/null || echo "000"
}

# call_llm <API_KEY> <MODEL> <EXPECT_STATUS> <DESC>
call_llm() {
    local api_key="$1" model="$2" expect="$3" desc="$4"
    local body
    body=$(printf '{"model":"%s","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' "$model")

    local resp status body_out
    resp=$(curl -s -w "\n__STATUS__%{http_code}" \
        -X POST "$LLM_URL/v1/chat/completions" \
        -H "Authorization: Bearer $api_key" \
        -H "Content-Type: application/json" \
        -d "$body" \
        --max-time 60 2>/dev/null || echo "__STATUS__000")
    status=$(echo "$resp" | tail -1 | sed 's/__STATUS__//')
    body_out=$(echo "$resp" | head -n -1)

    if [[ "$status" == "$expect" ]]; then
        pass "$desc [HTTP $status]"
    else
        fail "$desc [期望 $expect，實際 $status]"
        echo "       $(echo "$body_out" | head -c 300)"
    fi
}

# wait_cache <seconds> <reason>
wait_cache() {
    local secs="$1" reason="${2:-等待 cache 刷新}"
    info "$reason（${secs}s）"
    for ((i=secs; i>0; i--)); do
        printf "\r  ${YELLOW}→${NC} 剩餘 %2ds " "$i"
        sleep 1
    done
    printf "\r%50s\r" ""
}

# ── Cleanup ───────────────────────────────────────────────────────────────────

cleanup() {
    if [[ "$DO_CLEANUP" == false ]]; then
        info "略過 cleanup（--no-cleanup）"
        return
    fi
    section "Cleanup：刪除測試資料"
    local test_users=(
        test-user-alpha test-user-beta test-user-d6
        test-user-perms1 test-user-perms2
        test-user-empty test-user-wild test-user-gamma
    )
    local test_depts=(
        test-dept-alpha test-dept-beta
        test-dept-perms test-dept-empty
    )
    for uid in "${test_users[@]}"; do
        local code
        code=$(call_admin_silent DELETE "/api/v1/users/$uid")
        info "user $uid → HTTP $code"
    done
    for did in "${test_depts[@]}"; do
        local code
        code=$(call_admin_silent DELETE "/api/v1/departments/$did")
        info "dept $did → HTTP $code"
    done
}

trap cleanup EXIT

# ── Health Check ──────────────────────────────────────────────────────────────

check_health() {
    section "Health Check"

    local admin_status
    admin_status=$(curl -s -o /dev/null -w "%{http_code}" \
        "$ADMIN_URL/health" --max-time 10 2>/dev/null || echo "000")
    if [[ "$admin_status" == "200" ]]; then
        pass "Admin API /health → 200"
    else
        fail "Admin API 無法連線（HTTP $admin_status）"
        echo -e "${RED}中止測試：Admin API 未就緒${NC}"
        exit 1
    fi

    local llm_status
    llm_status=$(curl -s -o /dev/null -w "%{http_code}" \
        "$LLM_URL/health" --max-time 10 2>/dev/null || echo "000")
    if [[ "$llm_status" == "200" ]]; then
        pass "LiteLLM /health → 200"
        LLM_ALIVE=true
    else
        info "LiteLLM 未就緒（HTTP $llm_status）— perms/edge 測試將跳過"
        LLM_ALIVE=false
    fi
}

# ── D: 部門 CRUD (D1–D7) ──────────────────────────────────────────────────────

test_dept() {
    section "部門 CRUD（D1–D7）"

    # D1: 新增部門（正常）
    info "D1: 新增 test-dept-alpha → 201"
    call_admin POST "/api/v1/departments" \
        '{"dept_id":"test-dept-alpha","dept_name":"測試部門 Alpha","allowed_models":["reasoning-qwen"],"dept_rpm_limit":100,"dept_tpm_limit":500000}' \
        "201" "D1: 新增部門（正常）"

    # 建立 test-dept-beta 供 D7 使用
    info "前置: 建立 test-dept-beta（D7 刪除用）"
    call_admin POST "/api/v1/departments" \
        '{"dept_id":"test-dept-beta","dept_name":"測試部門 Beta","allowed_models":["fast-qwen"]}' \
        "201" "前置: 建立 test-dept-beta"

    # D2: dept_id 重複 → 409
    info "D2: dept_id 重複 → 409"
    call_admin POST "/api/v1/departments" \
        '{"dept_id":"test-dept-alpha","dept_name":"重複"}' \
        "409" "D2: dept_id 重複 → 409"

    # D3: 查詢單一部門
    info "D3: GET test-dept-alpha → 200"
    call_admin GET "/api/v1/departments/test-dept-alpha" "" \
        "200" "D3: GET 單一部門"

    # 查詢不存在的部門
    info "D3b: GET 不存在的部門 → 404"
    call_admin GET "/api/v1/departments/nonexistent-dept-xyz" "" \
        "404" "D3b: GET 不存在的部門 → 404"

    # D4: 列出所有部門
    info "D4: GET /departments → 200"
    call_admin GET "/api/v1/departments" "" \
        "200" "D4: GET 部門清單"

    # D5: PATCH 更新 allowed_models
    info "D5: PATCH allowed_models → 200"
    call_admin PATCH "/api/v1/departments/test-dept-alpha" \
        '{"allowed_models":["reasoning-qwen","fast-qwen"]}' \
        "200" "D5: PATCH 部門 allowed_models"
    _check_resp "D5b: 確認 allowed_models 已包含 fast-qwen" \
        "import json,sys; d=json.load(sys.stdin); m=d.get('allowed_models',[]); print('ok' if 'fast-qwen' in m else f'got: {m}')"

    # D6: 刪除有 user 的部門 → 409（先建一個 user）
    info "前置: 建立 test-user-d6（D6 用）"
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-d6-001","key_name":"test-d6","user_id":"test-user-d6","dept_id":"test-dept-alpha","models":["reasoning-qwen"]}' \
        "201" "前置: 建立 test-user-d6"

    info "D6: 刪除有 user 的部門 → 409"
    call_admin DELETE "/api/v1/departments/test-dept-alpha" "" \
        "409" "D6: 刪除有 user 的部門 → 409"

    # D7: 刪除空部門 → 204
    info "D7: 刪除空部門 test-dept-beta → 204"
    call_admin DELETE "/api/v1/departments/test-dept-beta" "" \
        "204" "D7: 刪除空部門 → 204"
}

# ── U: 使用者 CRUD (U1–U14) ───────────────────────────────────────────────────

test_user() {
    section "使用者 CRUD（U1–U14）"

    # 確保 test-dept-alpha 存在
    local s
    s=$(call_admin_silent GET "/api/v1/departments/test-dept-alpha")
    if [[ "$s" != "200" ]]; then
        info "前置: 建立 test-dept-alpha"
        call_admin POST "/api/v1/departments" \
            '{"dept_id":"test-dept-alpha","dept_name":"測試部門 Alpha","allowed_models":["reasoning-qwen","fast-qwen"]}' \
            "201" "前置: test-dept-alpha"
    fi
    # 確保 test-dept-beta 存在（用於 U10 轉移）
    s=$(call_admin_silent GET "/api/v1/departments/test-dept-beta")
    if [[ "$s" != "200" ]]; then
        info "前置: 建立 test-dept-beta"
        call_admin POST "/api/v1/departments" \
            '{"dept_id":"test-dept-beta","dept_name":"測試部門 Beta","allowed_models":["fast-qwen"]}' \
            "201" "前置: test-dept-beta"
    fi
    # 確保 test-user-alpha 不存在（防止上次殘留）
    call_admin_silent DELETE "/api/v1/users/test-user-alpha" > /dev/null

    # U1: 新增使用者（正常）
    info "U1: 新增 test-user-alpha → 201"
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-alpha-001","key_name":"test-alpha","user_id":"test-user-alpha","user_email":"alpha@test.com","dept_id":"test-dept-alpha","models":["reasoning-qwen"],"rpm_limit":60,"tpm_limit":100000}' \
        "201" "U1: 新增使用者（正常）"

    # U2: dept_id 不存在 → 404
    info "U2: dept_id 不存在 → 404"
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-nodept-001","key_name":"nodept","user_id":"test-user-nodept-tmp","dept_id":"nonexistent-dept-xyz","models":[]}' \
        "404" "U2: dept_id 不存在 → 404"

    # U3: api_key 重複 → 409
    info "U3: api_key 重複 → 409"
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-alpha-001","key_name":"dup-key","user_id":"test-user-dupkey-tmp","dept_id":"test-dept-alpha","models":[]}' \
        "409" "U3: api_key 重複 → 409"

    # U4: user_id 重複 → 409
    info "U4: user_id 重複 → 409"
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-alpha-999","key_name":"dup-id","user_id":"test-user-alpha","dept_id":"test-dept-alpha","models":[]}' \
        "409" "U4: user_id 重複 → 409"

    # U5: 查詢單一使用者
    info "U5: GET test-user-alpha → 200"
    call_admin GET "/api/v1/users/test-user-alpha" "" \
        "200" "U5: GET 單一使用者"

    # U6: 查詢不存在的使用者 → 404
    info "U6: GET 不存在的使用者 → 404"
    call_admin GET "/api/v1/users/nonexistent-user-xyz-999" "" \
        "404" "U6: GET 不存在的使用者 → 404"

    # U7: 列出所有使用者
    info "U7: GET /users → 200"
    call_admin GET "/api/v1/users" "" \
        "200" "U7: GET 使用者清單"

    # U8: 依 dept_id 篩選
    info "U8: GET /users?dept_id=test-dept-alpha → 200"
    call_admin GET "/api/v1/users?dept_id=test-dept-alpha" "" \
        "200" "U8: GET 使用者（dept_id 篩選）"
    _check_resp "U8b: 確認回傳 user 全屬 test-dept-alpha" \
        "import json,sys; data=json.load(sys.stdin); wrong=[u['user_id'] for u in data if u.get('dept_id')!='test-dept-alpha']; print('ok' if not wrong else f'含非 alpha user: {wrong}')"

    # U9: PATCH 更新 models
    info "U9: PATCH models → 200"
    call_admin PATCH "/api/v1/users/test-user-alpha" \
        '{"models":["reasoning-qwen","fast-qwen"]}' \
        "200" "U9: PATCH 使用者 models"
    _check_resp "U9b: 確認 models 已包含 fast-qwen" \
        "import json,sys; d=json.load(sys.stdin); m=d.get('models',[]); print('ok' if 'fast-qwen' in m else f'got: {m}')"

    # U10: PATCH 轉移 dept_id
    info "U10: PATCH dept_id 轉移 → 200"
    call_admin PATCH "/api/v1/users/test-user-alpha" \
        '{"dept_id":"test-dept-beta","models":["fast-qwen"]}' \
        "200" "U10: PATCH 轉移使用者到 test-dept-beta"
    # 轉回 alpha（後續測試需要）
    call_admin PATCH "/api/v1/users/test-user-alpha" \
        '{"dept_id":"test-dept-alpha","models":["reasoning-qwen"]}' \
        "200" "U10b: 轉回 test-dept-alpha"

    # U11: PUT 整體替換
    info "U11: PUT 整體替換 → 200"
    call_admin PUT "/api/v1/users/test-user-alpha" \
        '{"api_key":"sk-test-alpha-001","key_name":"test-alpha-v2","user_id":"test-user-alpha","user_email":"alpha-v2@test.com","dept_id":"test-dept-alpha","models":["reasoning-qwen"],"rpm_limit":30,"tpm_limit":50000,"blocked":false}' \
        "200" "U11: PUT 整體替換使用者"

    # U12: 封鎖使用者
    info "U12: 封鎖 test-user-alpha → 200 (blocked=true)"
    call_admin POST "/api/v1/users/test-user-alpha/block" "" \
        "200" "U12: 封鎖使用者"
    _check_resp "U12b: 確認 blocked=true" \
        "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('blocked')==True else f'blocked={d.get(\"blocked\")}')"

    # U13: 解除封鎖
    info "U13: 解除封鎖 test-user-alpha → 200 (blocked=false)"
    call_admin POST "/api/v1/users/test-user-alpha/unblock" "" \
        "200" "U13: 解除封鎖"
    _check_resp "U13b: 確認 blocked=false" \
        "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('blocked')==False else f'blocked={d.get(\"blocked\")}')"

    # U14: 刪除使用者
    info "U14: DELETE test-user-alpha → 204"
    call_admin DELETE "/api/v1/users/test-user-alpha" "" \
        "204" "U14: 刪除使用者"
    call_admin GET "/api/v1/users/test-user-alpha" "" \
        "404" "U14b: 確認已刪除 → 404"
}

# ── M: Model 權限驗證 (M1–M8) ─────────────────────────────────────────────────
# 利用 db_version 機制：每次 admin 寫入都會 bump version，
# LiteLLM 下一個 request 即刻重載 cache（不需等 30s TTL）。
# sleep 5s 確保 SQLite WAL commit 可見。

test_perms() {
    if [[ "$LLM_ALIVE" != "true" ]]; then
        section "Model 權限驗證（跳過：LiteLLM 未就緒）"
        return
    fi
    section "Model 權限驗證（M1–M8）"

    # M1: 現有使用者 + 有效 model → 200（不需改 DB）
    info "M1: 現有使用者 + 有效 model → 200"
    call_llm "sk-dev-eng-user-001" "reasoning-qwen" "200" \
        "M1: eng-user-001 → reasoning-qwen → 200"

    # M2: user.models 不含此 model → 403（ds user 無 claude 權限）
    info "M2: user.models 不含此 model → 403"
    call_llm "sk-dev-ds-user-001" "openrouter/anthropic/claude-sonnet-4-5" "403" \
        "M2: ds-user-001 → claude（user 無此 model）→ 403"

    # M7: 完全不存在的 api_key → 401（不需改 DB）
    info "M7: 不存在的 api_key → 401"
    call_llm "sk-nonexistent-key-xyz-999" "reasoning-qwen" "401" \
        "M7: 無效 api_key → 401"

    # ── 建立測試用 dept + users ────────────────────────────────────────────────
    info "前置: 建立 test-dept-perms（只允許 reasoning-qwen）"
    local s
    s=$(call_admin_silent GET "/api/v1/departments/test-dept-perms")
    if [[ "$s" != "200" ]]; then
        call_admin POST "/api/v1/departments" \
            '{"dept_id":"test-dept-perms","dept_name":"權限測試部門","allowed_models":["reasoning-qwen"]}' \
            "201" "前置: 建立 test-dept-perms"
    fi

    # test-user-perms1: models 有 fast-qwen，但 dept 只允許 reasoning-qwen（M3/M4/M5/M6 用）
    call_admin_silent DELETE "/api/v1/users/test-user-perms1" > /dev/null
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-perms1-001","key_name":"test-perms1","user_id":"test-user-perms1","dept_id":"test-dept-perms","models":["reasoning-qwen","fast-qwen"]}' \
        "201" "前置: 建立 test-user-perms1 (models: reasoning+fast)"

    # test-user-perms2: 正常 user（M8 用）
    call_admin_silent DELETE "/api/v1/users/test-user-perms2" > /dev/null
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-perms2-001","key_name":"test-perms2","user_id":"test-user-perms2","dept_id":"test-dept-perms","models":["reasoning-qwen"]}' \
        "201" "前置: 建立 test-user-perms2 (M8 用)"

    wait_cache 5 "等待 cache 刷新（新 user/dept 建立）"

    # M3: user 有 fast-qwen，dept 沒有 → 403（dept 層擋）
    info "M3: user 有 fast-qwen，dept 無 → 403"
    call_llm "sk-test-perms1-001" "fast-qwen" "403" \
        "M3: user 有 fast-qwen 但 dept 無 → 403（dept 層擋）"

    # M8: 新建 user 後呼叫有效 model → 200
    info "M8: 新建 user 呼叫有效 model → 200"
    call_llm "sk-test-perms2-001" "reasoning-qwen" "200" \
        "M8: 新建 test-user-perms2 → reasoning-qwen → 200"

    # M4: PATCH dept 新增 fast-qwen → 等待 → 200
    info "M4: PATCH dept 新增 fast-qwen..."
    call_admin PATCH "/api/v1/departments/test-dept-perms" \
        '{"allowed_models":["reasoning-qwen","fast-qwen"]}' \
        "200" "M4a: PATCH dept allowed_models 加入 fast-qwen"

    wait_cache 5 "等待 cache 刷新（dept 更新）"

    info "M4: dept+user 都有 fast-qwen → 200"
    call_llm "sk-test-perms1-001" "fast-qwen" "200" \
        "M4: dept 更新後 → fast-qwen → 200"

    # M5: 封鎖 user → 等待 → 401
    info "M5: 封鎖 test-user-perms1..."
    call_admin POST "/api/v1/users/test-user-perms1/block" "" \
        "200" "M5a: 封鎖 test-user-perms1"

    wait_cache 5 "等待 cache 刷新（user blocked）"

    info "M5: 封鎖後舊 api_key → 401"
    call_llm "sk-test-perms1-001" "reasoning-qwen" "401" \
        "M5: blocked user → 401"

    # M6: 刪除 user → 等待 → 401（blocked user 仍可刪除，不需先解封）
    info "M6: 刪除 test-user-perms1..."
    call_admin DELETE "/api/v1/users/test-user-perms1" "" \
        "204" "M6a: 刪除 test-user-perms1"

    wait_cache 5 "等待 cache 刷新（user deleted）"

    info "M6: 刪除後舊 api_key → 401"
    call_llm "sk-test-perms1-001" "reasoning-qwen" "401" \
        "M6: deleted user → 401"
}

# ── E: 邊界情況 (E1–E5) ───────────────────────────────────────────────────────

test_edge() {
    section "邊界情況（E1–E5）"

    # E1a: Admin API 無 Bearer token → 403
    info "E1a: 無 Bearer token → 403"
    local s
    s=$(curl -s -o /dev/null -w "%{http_code}" \
        "$ADMIN_URL/api/v1/departments" --max-time 10 2>/dev/null || echo "000")
    if [[ "$s" == "401" || "$s" == "403" || "$s" == "422" ]]; then
        pass "E1a: 無 Bearer token → $s（拒絕）"
    else
        fail "E1a: 無 Bearer token 應被拒絕，實際 HTTP $s"
    fi

    # E1b: 錯誤的 Bearer token → 401
    info "E1b: 錯誤 Bearer token → 401"
    s=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer wrong-key-xyz" \
        "$ADMIN_URL/api/v1/departments" --max-time 10 2>/dev/null || echo "000")
    if [[ "$s" == "401" ]]; then
        pass "E1b: 錯誤 Bearer token → 401"
    else
        fail "E1b: 錯誤 token 應 401，實際 HTTP $s"
    fi

    if [[ "$LLM_ALIVE" != "true" ]]; then
        info "E2–E5 需要 LiteLLM，跳過"
        return
    fi

    # 確保 test-dept-alpha 存在
    s=$(call_admin_silent GET "/api/v1/departments/test-dept-alpha")
    if [[ "$s" != "200" ]]; then
        info "前置: 建立 test-dept-alpha"
        call_admin POST "/api/v1/departments" \
            '{"dept_id":"test-dept-alpha","dept_name":"測試部門 Alpha","allowed_models":["reasoning-qwen","fast-qwen"]}' \
            "201" "前置: test-dept-alpha"
    fi
    # 確保 test-dept-empty 存在
    s=$(call_admin_silent GET "/api/v1/departments/test-dept-empty")
    if [[ "$s" != "200" ]]; then
        call_admin POST "/api/v1/departments" \
            '{"dept_id":"test-dept-empty","dept_name":"空 Model 部門","allowed_models":[]}' \
            "201" "前置: test-dept-empty"
    fi

    # E2: models=[] 的 user → 任何 model → 403
    info "E2: models=[] → 任何 model → 403"
    call_admin_silent DELETE "/api/v1/users/test-user-empty" > /dev/null
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-empty-001","key_name":"test-empty","user_id":"test-user-empty","dept_id":"test-dept-alpha","models":[]}' \
        "201" "E2a: 建立 models=[] 的 user"
    wait_cache 5 "等待 cache（E2 user 建立）"
    call_llm "sk-test-empty-001" "reasoning-qwen" "403" \
        "E2: models=[] → 403（user 層全封）"

    # E3: models=["*"] 的 user → 任意 model → 200
    info "E3: models=[\"*\"] → 任意 model → 200"
    call_admin_silent DELETE "/api/v1/users/test-user-wild" > /dev/null
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-wild-001","key_name":"test-wild","user_id":"test-user-wild","dept_id":"test-dept-alpha","models":["*"]}' \
        "201" "E3a: 建立 models=[\"*\"] 的 user"
    wait_cache 5 "等待 cache（E3 user 建立）"
    call_llm "sk-test-wild-001" "reasoning-qwen" "200" \
        "E3: models=[\"*\"] → 200（wildcard 全開）"

    # E4: dept allowed_models=[] → 該部門所有 user 呼叫 → 403
    info "E4: dept allowed_models=[] → user 呼叫 → 403"
    call_admin_silent DELETE "/api/v1/users/test-user-gamma" > /dev/null
    call_admin POST "/api/v1/users" \
        '{"api_key":"sk-test-gamma-001","key_name":"test-gamma","user_id":"test-user-gamma","dept_id":"test-dept-empty","models":["reasoning-qwen"]}' \
        "201" "E4a: 建立 user（dept 無 allowed_models）"
    wait_cache 5 "等待 cache（E4 user 建立）"
    call_llm "sk-test-gamma-001" "reasoning-qwen" "403" \
        "E4: dept allowed_models=[] → 403（dept 層全封）"

    # E5: 轉移 user 到新 dept，model 不在新 dept allowed_models → 403
    # test-user-gamma（models: reasoning-qwen）轉移到 test-dept-empty（allowed: []）
    # 實際上 test-user-gamma 已在 test-dept-empty，就直接用
    info "E5: user 已在 allowed_models=[] 的 dept → 403（同 E4，確認轉移生效）"
    call_admin PATCH "/api/v1/departments/test-dept-empty" \
        '{"allowed_models":["fast-qwen"]}' \
        "200" "E5a: PATCH test-dept-empty 只允許 fast-qwen"
    wait_cache 5 "等待 cache（E5 dept 更新）"
    call_llm "sk-test-gamma-001" "reasoning-qwen" "403" \
        "E5: user 有 reasoning-qwen，dept 只允許 fast-qwen → 403"
}

# ── Summary ───────────────────────────────────────────────────────────────────

print_summary() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════${NC}"
    local total=$((PASS + FAIL))
    if [[ "$FAIL" -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}  全部通過：$PASS / $total${NC}"
    else
        echo -e "${YELLOW}${BOLD}  通過：$PASS / $total　${RED}失敗：$FAIL / $total${NC}"
    fi
    echo -e "${BOLD}════════════════════════════════════════${NC}"
}

# ── Main ──────────────────────────────────────────────────────────────────────

echo -e "${BOLD}Admin API 整合測試套件${NC}"
echo -e "  LiteLLM  : ${CYAN}$LLM_URL${NC}"
echo -e "  Admin API: ${CYAN}$ADMIN_URL${NC}"
echo -e "  Suite    : ${CYAN}$SUITE${NC}"
echo ""

check_health

case "$SUITE" in
    all)
        test_dept
        test_user
        test_perms
        test_edge
        ;;
    dept)   test_dept  ;;
    user)   test_user  ;;
    perms)  test_perms ;;
    edge)   test_edge  ;;
    *)
        echo "未知 suite: $SUITE"
        echo "可用: all | dept | user | perms | edge"
        exit 1
        ;;
esac

print_summary
