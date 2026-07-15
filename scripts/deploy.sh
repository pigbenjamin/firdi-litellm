#!/usr/bin/env bash
# 快速部署腳本：將 firdi-litellm 所有 K8s 資源部署到 ai-platform namespace
#
# 用法：
#   ./scripts/deploy.sh              # 部署全部（storage → vllm → litellm）
#   ./scripts/deploy.sh storage      # 只建立 PV/PVC 與 users.db
#   ./scripts/deploy.sh gemma-4-31b  # 只部署 gemma-4-31b-vllm（思考型）
#   ./scripts/deploy.sh gemma-4-26b  # 只部署 gemma-4-26b-vllm（快捷型）
#   ./scripts/deploy.sh light-models # 只部署 light-models（embedding + marker，同一張 GPU）
#   ./scripts/deploy.sh litellm      # 只部署 litellm（含 ConfigMap）
#   ./scripts/deploy.sh admin-api    # 只部署 admin-api
#   ./scripts/deploy.sh secrets      # 只建立 Secrets
#   ./scripts/deploy.sh status       # 顯示所有 Pod/Service 狀態

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NS="ai-platform"

# ── 顏色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

# ── 載入 .env ─────────────────────────────────────────────────────────────────
load_env() {
    local env_file="$REPO_ROOT/.env"
    [[ -f "$env_file" ]] || die ".env 不存在，請先複製 .env.example：cp .env.example .env"
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    ok "載入 .env"
}

# ── Secrets ───────────────────────────────────────────────────────────────────
deploy_secrets() {
    info "建立 Secrets..."

    [[ -z "${OPENWEBUI_URL:-}" ]]         && warn "OPENWEBUI_URL 未設定"
    [[ -z "${OPENWEBUI_ADMIN_KEY:-}" ]]   && warn "OPENWEBUI_ADMIN_KEY 未設定"
    [[ -z "${OPENWEBUI_SERVICE_KEY:-}" ]] && warn "OPENWEBUI_SERVICE_KEY 未設定"

    kubectl create secret generic litellm-secrets \
        --from-literal=master-key="${LITELLM_MASTER_KEY:-sk-firdi-master-change-me}" \
        --from-literal=openwebui-url="${OPENWEBUI_URL:-}" \
        --from-literal=openwebui-admin-key="${OPENWEBUI_ADMIN_KEY:-}" \
        --from-literal=openwebui-service-key="${OPENWEBUI_SERVICE_KEY:-}" \
        --from-literal=langfuse-public-key="${LANGFUSE_PUBLIC_KEY:-}" \
        --from-literal=langfuse-secret-key="${LANGFUSE_SECRET_KEY:-}" \
        --from-literal=langfuse-host="${LANGFUSE_HOST:-}" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    if [[ -n "${HF_TOKEN:-}" ]]; then
        kubectl create secret generic hf-token \
            --from-literal=token="$HF_TOKEN" \
            --namespace="$NS" \
            --dry-run=client -o yaml | kubectl apply -f -
        ok "hf-token secret 已建立"
    else
        warn "HF_TOKEN 未設定，跳過 hf-token secret（模型已在本機 cache 則無影響）"
    fi

    kubectl create secret generic admin-api-secrets \
        --from-literal=api-key="${ADMIN_API_KEY:-sk-admin-change-me}" \
        --from-literal=webhook-secret="${WEBHOOK_SECRET:-change-me-webhook-secret}" \
        --from-literal=keycloak-url="${KEYCLOAK_URL:-}" \
        --from-literal=keycloak-realm="${KEYCLOAK_REALM:-}" \
        --from-literal=keycloak-client-id="${KEYCLOAK_CLIENT_ID:-user-sync-service}" \
        --from-literal=keycloak-client-secret="${KEYCLOAK_CLIENT_SECRET:-}" \
        --from-literal=keycloak-ssl-verify="${KEYCLOAK_SSL_VERIFY:-false}" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    ok "Secrets 完成"
}

# ── Storage（PV / PVC / users.db）─────────────────────────────────────────────
deploy_storage() {
    info "建立 PV / PVC..."
    local data_path="${K8S_DATA_HOST_PATH:-/opt/firdi/data}"

    if [[ ! -d "$data_path" ]]; then
        info "建立目錄 $data_path"
        mkdir -p "$data_path" || sudo mkdir -p "$data_path"
    fi

    export K8S_DATA_HOST_PATH="$data_path"
    envsubst < "$REPO_ROOT/k8s/shared-storage/pvc.yaml" | kubectl apply -f -
    ok "PV/PVC 完成（hostPath: $data_path）"

    info "Migrate users.json → users.db..."
    local db_path="$data_path/users.db"
    python3 "$REPO_ROOT/scripts/migrate_users_json.py" \
        --json "$REPO_ROOT/config/users.json" \
        --db  "$db_path"
    ok "users.db 已建立：$db_path"
}

# ── LiteLLM ConfigMaps ────────────────────────────────────────────────────────
deploy_litellm_configmaps() {
    info "建立 LiteLLM ConfigMaps..."
    local cfg="$REPO_ROOT/config"

    kubectl create configmap litellm-config \
        --from-file=litellm_config.yaml="$cfg/litellm_config.yaml" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    kubectl create configmap litellm-custom-auth \
        --from-file=custom_auth.py="$cfg/custom_auth.py" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    kubectl create configmap litellm-custom-logger \
        --from-file=custom_logger.py="$cfg/custom_logger.py" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    ok "ConfigMaps 完成"
}

# ── vLLM 部署 helper ──────────────────────────────────────────────────────────
deploy_vllm() {
    local name="$1"   # gemma-4-31b / gemma-4-26b / light-models
    local dir="$REPO_ROOT/k8s/vllm/$name"

    if [[ "$name" == "light-models" ]]; then
        info "Build light-models combo image (firdi-light-models:latest)..."
        docker build -f "$REPO_ROOT/marker-service/Dockerfile" -t firdi-light-models:latest "$REPO_ROOT"
        if command -v k3s &>/dev/null; then
            info "匯入 image 到 k3s containerd..."
            # 不能用 `docker save | sudo ...` 管線：sudo 在管線裡拿不到終端機控制權要密碼，
            # 會整條卡死（docker save 也會因為 pipe buffer 滿了被塞住）。改成先存檔再 import。
            TMP_IMAGE_TAR="$(mktemp --suffix=.tar)"
            docker save firdi-light-models:latest -o "$TMP_IMAGE_TAR"
            sudo k3s ctr images import "$TMP_IMAGE_TAR"
            rm -f "$TMP_IMAGE_TAR"
            ok "Image 匯入完成"
        fi
        info "套用 marker 共用 ingest-data PV/PVC..."
        kubectl apply -f "$REPO_ROOT/k8s/shared-storage/marker-ingest-pvc.yaml"
    fi

    info "部署 $name vLLM..."
    export K8S_HF_CACHE_HOST_PATH="${K8S_HF_CACHE_HOST_PATH:-/opt/firdi/hf-cache}"
    for f in "$dir"/*.yaml; do
        envsubst < "$f" | kubectl apply -f -
    done
    if [[ "$name" == "light-models" ]]; then
        kubectl rollout restart deployment/light-models-vllm -n "$NS"
    fi
    ok "$name vLLM 套用完成（HF cache hostPath: $K8S_HF_CACHE_HOST_PATH）"
}

# ── LiteLLM ───────────────────────────────────────────────────────────────────
deploy_litellm() {
    deploy_litellm_configmaps
    info "部署 LiteLLM..."
    export K8S_LOGS_HOST_PATH="${K8S_LOGS_HOST_PATH:-/opt/firdi/logs}"
    envsubst < "$REPO_ROOT/k8s/litellm/deployment.yaml" | kubectl apply -f -
    kubectl apply -f "$REPO_ROOT/k8s/litellm/service.yaml"
    ok "LiteLLM 套用完成（logs hostPath: $K8S_LOGS_HOST_PATH）"
}

# ── Admin API ─────────────────────────────────────────────────────────────────
deploy_admin_api() {
    info "部署 Admin API..."
    local img
    img=$(grep 'image:' "$REPO_ROOT/k8s/admin-api/deployment.yaml" | awk '{print $2}')
    if [[ "$img" == *"<registry>"* ]]; then
        warn "admin-api image 尚未設定（仍為 <registry> placeholder），跳過部署"
        warn "請先 build image 並更新 k8s/admin-api/deployment.yaml"
        return 0
    fi

    info "Build admin-api image: $img"
    docker build -t "$img" "$REPO_ROOT/admin-api"

    # k3s 用 containerd，需要將 docker image 導入才能讓 pod 使用
    if command -v k3s &>/dev/null; then
        info "匯入 image 到 k3s containerd..."
        # 不能用 `docker save | sudo ...` 管線：sudo 在管線裡拿不到終端機控制權要密碼，
        # 會整條卡死。改成先存檔再 import。
        TMP_IMAGE_TAR="$(mktemp --suffix=.tar)"
        docker save "$img" -o "$TMP_IMAGE_TAR"
        sudo k3s ctr images import "$TMP_IMAGE_TAR"
        rm -f "$TMP_IMAGE_TAR"
        ok "Image 匯入完成"
    fi

    kubectl apply -f "$REPO_ROOT/k8s/admin-api/deployment.yaml"
    kubectl apply -f "$REPO_ROOT/k8s/admin-api/service.yaml"
    kubectl rollout restart deployment/admin-api -n "$NS"
    kubectl rollout status deployment/admin-api -n "$NS" --timeout=90s
    ok "Admin API 部署完成"
}

# ── Status ────────────────────────────────────────────────────────────────────
show_status() {
    echo ""
    echo -e "${CYAN}═══ Pods ════════════════════════════════════════${NC}"
    kubectl get pods -n "$NS" -o wide 2>/dev/null || true
    echo ""
    echo -e "${CYAN}═══ Services ════════════════════════════════════${NC}"
    kubectl get svc -n "$NS" 2>/dev/null || true
    echo ""
    # 找 NodeIP
    local node_ip
    node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
    if [[ -n "$node_ip" ]]; then
        echo -e "${GREEN}LiteLLM endpoint :${NC} http://$node_ip:30400"
        echo -e "${GREEN}Admin API endpoint:${NC} http://$node_ip:30408"
    fi
}

# ── 全部部署 ──────────────────────────────────────────────────────────────────
deploy_all() {
    deploy_secrets
    deploy_storage
    deploy_vllm gemma-4-31b
    deploy_vllm gemma-4-26b
    deploy_vllm light-models
    deploy_litellm
    deploy_admin_api
    echo ""
    ok "=== 全部部署完成 ==="
    show_status
    echo ""
    warn "vLLM 首次啟動需下載模型 + torch.compile 編譯（Gemma 4 約 15~25 分鐘；有編譯快取的暖重啟約 2~4 分鐘），監看狀態："
    echo "  kubectl get pods -n $NS -w"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    local cmd="${1:-all}"

    # 確認 namespace 存在
    kubectl apply -f "$REPO_ROOT/k8s/namespace.yaml" > /dev/null

    load_env

    case "$cmd" in
        all)          deploy_all ;;
        storage)      deploy_storage ;;
        secrets)      deploy_secrets ;;
        gemma-4-31b)  deploy_vllm gemma-4-31b ;;
        gemma-4-26b)  deploy_vllm gemma-4-26b ;;
        light-models) deploy_vllm light-models ;;
        litellm)      deploy_litellm ;;
        admin-api)    deploy_admin_api ;;
        status)       show_status ;;
        *)
            echo "用法: $0 [all|storage|secrets|gemma-4-31b|gemma-4-26b|light-models|litellm|admin-api|status]"
            exit 1
            ;;
    esac
}

main "$@"
