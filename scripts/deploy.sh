#!/usr/bin/env bash
# 快速部署腳本：將 firdi-litellm 所有 K8s 資源部署到 ai-platform namespace
#
# 用法：
#   ./scripts/deploy.sh              # 部署全部（storage → vllm → litellm → admin-api）
#   ./scripts/deploy.sh storage      # 只建立 users-db-pvc / litellm-logs-pvc
#   ./scripts/deploy.sh users-db     # 只檢查/初始化 users.db（灌進 users-db-pvc）
#   ./scripts/deploy.sh gemma-4-31b  # 只部署 gemma-4-31b-vllm（思考型）
#   ./scripts/deploy.sh gemma-4-26b  # 只部署 gemma-4-26b-vllm（快捷型）
#   ./scripts/deploy.sh light-models # 只部署 light-models（embedding + marker，同一張 GPU）
#   ./scripts/deploy.sh litellm      # 只部署 litellm（含 ConfigMap）
#   ./scripts/deploy.sh admin-api    # 只部署 admin-api
#   ./scripts/deploy.sh secrets      # 只建立 Secrets
#   ./scripts/deploy.sh status       # 顯示所有 Pod/Service 狀態
#
# users-db-pvc / litellm-logs-pvc 走 storageClassName 動態佈建（.env 的
# K8S_PVC_STORAGE_CLASS，單節點 k3s 預設 local-path，公司 Ceph 叢集設
# rook-ceph-block），不再是 hostPath，admin-api 用 podAffinity 釘住 litellm 所在
# 節點才能兩者都掛上同一顆 RWO PVC。hf-cache / marker-ingest 仍是 hostPath，多節點
# 叢集：.env 設定 REGISTRY（推送 image 用）與 K8S_GPU_NODE_HOSTNAME（釘選這兩個
# hostPath 相關 Pod 的節點），單節點 k3s 可全部留空。

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

    if [[ -n "${K8S_IMAGE_PULL_SECRET:-}" ]]; then
        if [[ -z "${REGISTRY:-}" || -z "${REGISTRY_USERNAME:-}" || -z "${REGISTRY_PASSWORD:-}" ]]; then
            warn "K8S_IMAGE_PULL_SECRET 已設定，但 REGISTRY / REGISTRY_USERNAME / REGISTRY_PASSWORD 有缺，跳過建立 imagePullSecret"
        else
            kubectl create secret docker-registry "$K8S_IMAGE_PULL_SECRET" \
                --docker-server="${REGISTRY%%/*}" \
                --docker-username="$REGISTRY_USERNAME" \
                --docker-password="$REGISTRY_PASSWORD" \
                --namespace="$NS" \
                --dry-run=client -o yaml | kubectl apply -f -
            ok "imagePullSecret「$K8S_IMAGE_PULL_SECRET」已建立"
        fi
    fi

    ok "Secrets 完成"
}

# 若設定 K8S_IMAGE_PULL_SECRET，掛到指定 Deployment 的 imagePullSecrets（私有 registry
# 才需要）。用 kubectl patch 而非模板變數：envsubst 沒辦法在留空時讓整段 imagePullSecrets
# 乾淨消失，patch 則單純不執行，deployment.yaml 本身完全不受影響。
apply_image_pull_secret() {
    local deploy_name="$1"
    [[ -n "${K8S_IMAGE_PULL_SECRET:-}" ]] || return 0
    kubectl patch deployment "$deploy_name" -n "$NS" --type=strategic \
        -p "{\"spec\":{\"template\":{\"spec\":{\"imagePullSecrets\":[{\"name\":\"${K8S_IMAGE_PULL_SECRET}\"}]}}}}"
}

# ── Image 建置與發佈 ───────────────────────────────────────────────────────────
# 本機 build 好的 image 要讓目標節點的 kubelet 抓得到：
#   - 設定 REGISTRY：tag + push 到該 registry（多節點必須，任何節點都能 pull）
#   - 未設定 REGISTRY 但本機是 k3s：直接 import 進 k3s 內建 containerd（僅限單節點，
#     image 只會在這台機器上，pod 排到別的節點會抓不到）
#   - 都不是：只在本機 docker，僅供單節點測試
# 本機沒有 docker（例如純 worker 節點）但有設定 REGISTRY 時：跳過 build+push，直接
# 沿用其他機器先前已經 push 上去的同名 image（同一個 REGISTRY + local_tag），只做
# kubectl 部署。沒有 docker 又沒設 REGISTRY 就真的無法取得 image，直接報錯。
# 結果透過全域變數 IMAGE_REF / IMAGE_PULL_POLICY 回傳（不用 command substitution，
# 避免 info/warn 訊息被一起截進回傳值）。
IMAGE_REF=""
IMAGE_PULL_POLICY=""
build_and_publish_image() {
    local local_tag="$1"        # 例如 firdi-admin-api:latest
    local build_context="$2"
    local dockerfile="${3:-}"   # 可選；預設用 build_context 下的 Dockerfile

    if ! command -v docker &>/dev/null; then
        [[ -n "${REGISTRY:-}" ]] || die "本機沒有 docker，且未設定 REGISTRY，無法取得 image（請安裝 docker，或在 .env 設定 REGISTRY 並先在其他機器 build+push 過同一個 tag）"
        warn "本機沒有 docker，跳過 build+push，直接沿用 registry 上既有的 image（需已由其他機器 build+push 過同一個 tag）"
        IMAGE_REF="${REGISTRY}/${local_tag}"
        IMAGE_PULL_POLICY="Always"
        return 0
    fi

    if [[ -n "$dockerfile" ]]; then
        docker build -f "$dockerfile" -t "$local_tag" "$build_context"
    else
        docker build -t "$local_tag" "$build_context"
    fi

    if [[ -n "${REGISTRY:-}" ]]; then
        if [[ -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
            # docker-server 只取 host 部分（REGISTRY 可能是 "ghcr.io/org" 這種含路徑的形式）
            echo "$REGISTRY_PASSWORD" | docker login "${REGISTRY%%/*}" -u "$REGISTRY_USERNAME" --password-stdin
        fi
        IMAGE_REF="${REGISTRY}/${local_tag}"
        info "推送 image 到 registry: $IMAGE_REF"
        docker tag "$local_tag" "$IMAGE_REF"
        docker push "$IMAGE_REF"
        IMAGE_PULL_POLICY="Always"
    elif command -v k3s &>/dev/null; then
        info "匯入 image 到 k3s containerd..."
        # 不能用 `docker save | sudo ...` 管線：sudo 在管線裡拿不到終端機控制權要密碼，
        # 會整條卡死（docker save 也會因為 pipe buffer 滿了被塞住）。改成先存檔再 import。
        local tmp_tar
        tmp_tar="$(mktemp --suffix=.tar)"
        docker save "$local_tag" -o "$tmp_tar"
        sudo k3s ctr images import "$tmp_tar"
        rm -f "$tmp_tar"
        ok "Image 匯入完成"
        IMAGE_REF="$local_tag"
        IMAGE_PULL_POLICY="IfNotPresent"
    else
        warn "非 k3s 且未設定 REGISTRY：image 只在本機 docker，多節點叢集請在 .env 設定 REGISTRY"
        IMAGE_REF="$local_tag"
        IMAGE_PULL_POLICY="IfNotPresent"
    fi
}

# ── Storage（PVC）─────────────────────────────────────────────────────────────
deploy_storage() {
    info "建立 PVC..."
    export K8S_PVC_STORAGE_CLASS="${K8S_PVC_STORAGE_CLASS:-local-path}"
    envsubst < "$REPO_ROOT/k8s/shared-storage/pvc.yaml" | kubectl apply -f -
    ok "PVC 完成（storageClassName: $K8S_PVC_STORAGE_CLASS）"
}

# users-db-pvc 是動態佈建（Ceph/local-path），host 端不再能像過去 hostPath 那樣
# 直接寫檔案進 PV 內容，改成本機準備一份 users.db，用 kubectl cp 灌進已經掛載
# users-db-pvc 的 litellm Pod。只在 PVC 內還沒有 users.db 時才動手，避免每次重跑
# deploy.sh 都拿 config/users.json 這份範本資料蓋掉正式環境已經在跑的使用者資料
# （users.db 是活資料庫，換模型名稱請用 scripts/migrate_model_names.sh，不要直接改）。
seed_users_db() {
    info "檢查 users.db..."
    local pod
    pod=$(kubectl get pod -n "$NS" -l app=litellm -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "$pod" ]]; then
        warn "找不到 litellm Pod，略過 users.db 初始化（litellm 部署好之後執行 ./scripts/deploy.sh users-db 手動補上）"
        return 0
    fi
    if ! kubectl wait -n "$NS" "pod/$pod" --for=condition=Ready --timeout=120s >/dev/null 2>&1; then
        warn "litellm Pod 尚未 Ready，略過 users.db 初始化（稍後執行 ./scripts/deploy.sh users-db 手動補上）"
        return 0
    fi

    if kubectl exec -n "$NS" "$pod" -- test -f /app/data/users.db 2>/dev/null; then
        ok "users.db 已存在於 PVC，略過初始化"
        return 0
    fi

    local tmp_db legacy_db
    tmp_db="$(mktemp --suffix=.db)"
    legacy_db="${K8S_DATA_HOST_PATH:-}/users.db"
    if [[ -n "${K8S_DATA_HOST_PATH:-}" && -f "$legacy_db" ]]; then
        info "偵測到舊 hostPath 遺留的 users.db（$legacy_db），搬進 PVC..."
        cp "$legacy_db" "$tmp_db"
    else
        info "PVC 內尚無 users.db，且無舊資料可搬，用 config/users.json 範本產生..."
        python3 "$REPO_ROOT/scripts/migrate_users_json.py" \
            --json "$REPO_ROOT/config/users.json" \
            --db "$tmp_db"
    fi

    kubectl cp "$tmp_db" "$NS/$pod:/app/data/users.db"
    rm -f "$tmp_db"
    ok "users.db 已寫入 PVC"
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

    export K8S_GPU_NODE_HOSTNAME="${K8S_GPU_NODE_HOSTNAME:-$(hostname)}"

    if [[ "$name" == "light-models" ]]; then
        info "Build light-models combo image..."
        build_and_publish_image "firdi-light-models:latest" "$REPO_ROOT" "$REPO_ROOT/marker-service/Dockerfile"
        export LIGHT_MODELS_IMAGE="$IMAGE_REF"
        export LIGHT_MODELS_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY"

        info "套用 marker 共用 ingest-data PV/PVC..."
        export K8S_MARKER_INGEST_HOST_PATH="${K8S_MARKER_INGEST_HOST_PATH:-/home/ai-x/data/docblock/ingest}"
        envsubst < "$REPO_ROOT/k8s/shared-storage/marker-ingest-pvc.yaml" | kubectl apply -f -
    fi

    info "部署 $name vLLM（GPU node: $K8S_GPU_NODE_HOSTNAME）..."
    export K8S_HF_CACHE_HOST_PATH="${K8S_HF_CACHE_HOST_PATH:-/opt/firdi/hf-cache}"
    for f in "$dir"/*.yaml; do
        envsubst < "$f" | kubectl apply -f -
    done
    if [[ "$name" == "light-models" ]]; then
        apply_image_pull_secret light-models-vllm
        kubectl rollout restart deployment/light-models-vllm -n "$NS"
    fi
    ok "$name vLLM 套用完成（HF cache hostPath: $K8S_HF_CACHE_HOST_PATH）"
}

# ── LiteLLM ───────────────────────────────────────────────────────────────────
deploy_litellm() {
    deploy_litellm_configmaps
    info "部署 LiteLLM..."
    envsubst < "$REPO_ROOT/k8s/litellm/deployment.yaml" | kubectl apply -f -
    kubectl apply -f "$REPO_ROOT/k8s/litellm/service.yaml"
    ok "LiteLLM 套用完成"
}

# ── Admin API ─────────────────────────────────────────────────────────────────
deploy_admin_api() {
    info "部署 Admin API..."
    info "Build admin-api image..."
    build_and_publish_image "firdi-admin-api:latest" "$REPO_ROOT/admin-api"
    export ADMIN_API_IMAGE="$IMAGE_REF"
    export ADMIN_API_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY"

    envsubst < "$REPO_ROOT/k8s/admin-api/deployment.yaml" | kubectl apply -f -
    kubectl apply -f "$REPO_ROOT/k8s/admin-api/service.yaml"
    apply_image_pull_secret admin-api
    kubectl rollout restart deployment/admin-api -n "$NS"
    kubectl rollout status deployment/admin-api -n "$NS" --timeout=90s
    ok "Admin API 部署完成（image: $ADMIN_API_IMAGE）"
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
    seed_users_db
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
        users-db)     seed_users_db ;;
        secrets)      deploy_secrets ;;
        gemma-4-31b)  deploy_vllm gemma-4-31b ;;
        gemma-4-26b)  deploy_vllm gemma-4-26b ;;
        light-models) deploy_vllm light-models ;;
        litellm)      deploy_litellm ;;
        admin-api)    deploy_admin_api ;;
        status)       show_status ;;
        *)
            echo "用法: $0 [all|storage|users-db|secrets|gemma-4-31b|gemma-4-26b|light-models|litellm|admin-api|status]"
            exit 1
            ;;
    esac
}

main "$@"
