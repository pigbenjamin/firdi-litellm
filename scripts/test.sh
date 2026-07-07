#!/usr/bin/env bash
# 快速測試腳本：驗證 LiteLLM auth、model 權限、rate limit
#
# 用法：
#   ./scripts/test.sh                        # 自動偵測 NodeIP，跑全部測試
#   ./scripts/test.sh --host 10.90.20.55     # 指定 host
#   ./scripts/test.sh --host localhost:4000  # 本機 port-forward 模式
#   ./scripts/test.sh --suite auth           # 只跑 auth 測試
#   ./scripts/test.sh --suite reasoning      # 只跑 reasoning 模型測試
#   ./scripts/test.sh --suite rate-limit     # 只跑 rate limit 測試
#   ./scripts/test.sh --suite all            # 全部（預設）

set -euo pipefail

# ── 顏色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}  ✓ PASS${NC} $*"; ((++PASS)); }
fail() { echo -e "${RED}  ✗ FAIL${NC} $*"; ((++FAIL)); }
section() { echo -e "\n${CYAN}${BOLD}── $* ──────────────────────────────────${NC}"; }
info() { echo -e "${YELLOW}  →${NC} $*"; }

# ── 預設值 ────────────────────────────────────────────────────────────────────
HOST=""
SUITE="all"

# ── 解析參數 ──────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)   HOST="$2"; shift 2 ;;
        --suite)  SUITE="$2"; shift 2 ;;
        *)        echo "未知參數: $1"; exit 1 ;;
    esac
done

# ── 自動偵測 NodeIP ────────────────────────────────────────────────────────────
if [[ -z "$HOST" ]]; then
    NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
    if [[ -z "$NODE_IP" ]]; then
        echo -e "${RED}無法取得 NodeIP，請用 --host 指定${NC}"
        exit 1
    fi
    HOST="${NODE_IP}:30400"
    info "自動偵測 NodeIP: $HOST"
fi

BASE_URL="http://$HOST"

# ── 測試 helper ───────────────────────────────────────────────────────────────
# call_api <描述> <期望HTTP狀態> <api_key> <model> [extra_json_fields]
call_api() {
    local desc="$1"
    local expect="$2"
    local api_key="$3"
    local model="$4"
    local extra="${5:-}"

    local body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi, reply in one word\"}],\"max_tokens\":10$( [[ -n "$extra" ]] && echo ",$extra" )}"

    local resp
    resp=$(curl -s -w "\n__HTTP_STATUS__%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Authorization: Bearer $api_key" \
        -H "Content-Type: application/json" \
        -d "$body" \
        --max-time 60 2>/dev/null)

    local http_status
    http_status=$(echo "$resp" | tail -1 | sed 's/__HTTP_STATUS__//')
    local body_out
    body_out=$(echo "$resp" | head -n -1)

    if [[ "$http_status" == "$expect" ]]; then
        pass "$desc [HTTP $http_status]"
    else
        fail "$desc [期望 $expect，實際 $http_status]"
        echo "       Response: $(echo "$body_out" | head -c 200)"
    fi
}

# ── Health Check ──────────────────────────────────────────────────────────────
test_health() {
    section "Health Check"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health" --max-time 10 2>/dev/null || echo "000")
    if [[ "$status" == "200" ]]; then
        pass "LiteLLM health endpoint 回應 200"
    else
        fail "LiteLLM 無法連線（HTTP $status）— 請確認 Pod 已啟動"
        echo -e "${RED}中止測試：LiteLLM 未就緒${NC}"
        exit 1
    fi
}

# ── Auth 測試 ─────────────────────────────────────────────────────────────────
test_auth() {
    section "Auth 測試"

    info "有效 key（工程部）→ 應 200"
    call_api "工程部 user → gemma-4-31B-it" "200" \
        "sk-dev-eng-user-001" "gemma-4-31B-it"

    info "有效 key（資料科學部）→ 應 200"
    call_api "資料科學部 user → gemma-4-31B-it" "200" \
        "sk-dev-ds-user-001" "gemma-4-31B-it"

    info "無效 key → 應 401"
    call_api "無效 API key → 401" "401" \
        "sk-invalid-key-9999" "gemma-4-31B-it"
}

# ── Model 權限測試 ────────────────────────────────────────────────────────────
test_model_permissions() {
    section "Model 權限測試"

    info "工程部 → openrouter/claude（user 有權限，dept 也有）→ 應通過 auth（200 或 upstream error）"
    # openrouter key 可能是 placeholder，所以 upstream 可能 502，但 auth 應通過（不是 403）
    local resp_code
    resp_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Authorization: Bearer sk-dev-eng-user-001" \
        -H "Content-Type: application/json" \
        -d '{"model":"openrouter/anthropic/claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}],"max_tokens":10}' \
        --max-time 30 2>/dev/null || echo "000")
    if [[ "$resp_code" != "403" && "$resp_code" != "401" ]]; then
        pass "工程部 → openrouter/claude：auth 通過（HTTP $resp_code，非 auth 拒絕）"
        ((PASS++))
    else
        fail "工程部 → openrouter/claude：auth 被拒（HTTP $resp_code）"
        ((FAIL++))
    fi

    info "資料科學部 → openrouter/claude（user 無此 model 權限）→ 應 403"
    call_api "資料科學部 user → openrouter/claude → 403" "403" \
        "sk-dev-ds-user-001" "openrouter/anthropic/claude-sonnet-4-5"

    info "工程部 → gemma-4-26B-A4B-it → 應 200"
    call_api "工程部 user → gemma-4-26B-A4B-it → 200" "200" \
        "sk-dev-eng-user-001" "gemma-4-26B-A4B-it"

    info "工程部 → embeddinggemma-300m → 應 200"
    call_api "工程部 user → embeddinggemma-300m → 200" "200" \
        "sk-dev-eng-user-001" "embeddinggemma-300m"
}

# ── Rate Limit 測試 ───────────────────────────────────────────────────────────
test_rate_limit() {
    section "Rate Limit 測試（rpm_limit=2）"
    info "連打 3 次，第 3 次應 429..."

    local results=()
    for i in 1 2 3; do
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "$BASE_URL/v1/chat/completions" \
            -H "Authorization: Bearer sk-rate-limit-test-001" \
            -H "Content-Type: application/json" \
            -d '{"model":"gemma-4-31B-it","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
            --max-time 30 2>/dev/null || echo "000")
        results+=("$code")
        info "Request $i → HTTP $code"
    done

    # 前兩次應成功（200），第三次應 429
    if [[ "${results[2]}" == "429" ]]; then
        pass "第 3 次請求觸發 rate limit (429)"
    else
        fail "第 3 次請求應為 429，實際為 ${results[2]}"
    fi
}

# ── Reasoning 功能測試 ────────────────────────────────────────────────────────
test_reasoning() {
    section "Reasoning 功能測試"
    info "發送需要推理的問題，檢查回應..."

    local resp
    resp=$(curl -s -w "\n__HTTP_STATUS__%{http_code}" \
        -X POST "$BASE_URL/v1/chat/completions" \
        -H "Authorization: Bearer sk-dev-eng-user-001" \
        -H "Content-Type: application/json" \
        -d '{
          "model": "gemma-4-31B-it",
          "messages": [{"role": "user", "content": "What is 15 * 7? Think step by step."}],
          "max_tokens": 512
        }' \
        --max-time 120 2>/dev/null)

    local http_status body_out
    http_status=$(echo "$resp" | tail -1 | sed 's/__HTTP_STATUS__//')
    body_out=$(echo "$resp" | head -n -1)

    if [[ "$http_status" == "200" ]]; then
        pass "gemma-4-31B-it 回應正常 (HTTP 200)"
        # 顯示思考內容（若有；vLLM gemma4 parser 放在 message.reasoning）
        local reasoning
        reasoning=$(echo "$body_out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    msg = d['choices'][0]['message']
    rc = msg.get('reasoning') or msg.get('reasoning_content') or msg.get('thinking') or ''
    content = msg.get('content','')
    if rc:
        print('  [reasoning] ' + rc[:150] + ('...' if len(rc)>150 else ''))
    print('  [answer]    ' + content[:150])
except:
    pass
" 2>/dev/null || true)
        [[ -n "$reasoning" ]] && echo "$reasoning"
    else
        fail "gemma-4-31B-it 回應失敗 (HTTP $http_status)"
        echo "       Response: $(echo "$body_out" | head -c 300)"
    fi
}

# ── 模型列表確認 ──────────────────────────────────────────────────────────────
test_models_list() {
    section "可用模型列表"
    local resp
    resp=$(curl -s \
        "$BASE_URL/v1/models" \
        -H "Authorization: Bearer sk-dev-eng-user-001" \
        --max-time 10 2>/dev/null)

    local models
    models=$(echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for m in d.get('data', []):
        print('  -', m['id'])
except:
    print('  (解析失敗)')
" 2>/dev/null || echo "  (無法取得)")
    echo "$models"
}

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════${NC}"
    local total=$((PASS + FAIL))
    if [[ "$FAIL" -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}  全部通過：$PASS/$total${NC}"
    else
        echo -e "${YELLOW}${BOLD}  通過：$PASS/$total　失敗：${RED}$FAIL/$total${NC}"
    fi
    echo -e "${BOLD}════════════════════════════════════════${NC}"
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo -e "${BOLD}LiteLLM 測試套件 → $BASE_URL${NC}"
echo -e "Suite: ${CYAN}$SUITE${NC}"

test_health  # 一定跑

case "$SUITE" in
    all)
        test_models_list
        test_auth
        test_model_permissions
        test_reasoning
        test_rate_limit
        ;;
    auth)
        test_auth
        ;;
    reasoning)
        test_reasoning
        ;;
    rate-limit)
        test_rate_limit
        ;;
    permissions)
        test_model_permissions
        ;;
    *)
        echo "未知 suite: $SUITE"
        echo "可用: all | auth | reasoning | rate-limit | permissions"
        exit 1
        ;;
esac

print_summary
