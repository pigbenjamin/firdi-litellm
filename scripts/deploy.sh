#!/usr/bin/env bash
# 快速部署腳本：將 firdi-litellm 所有 K8s 資源部署到 ai-platform namespace
#
# 用法：
#   ./scripts/deploy.sh              # 部署全部（storage → postgres → internal-lb → vllm → litellm → admin-api）
#   ./scripts/deploy.sh storage      # 只建立 users-db-pvc / litellm-logs-pvc
#   ./scripts/deploy.sh postgres     # 只部署 Postgres（LiteLLM store_model_in_db 專用，見 docs/external-models-ops.md「路線 C」）
#   ./scripts/deploy.sh users-db     # 只檢查/初始化 users.db（灌進 users-db-pvc）
#   ./scripts/deploy.sh service-accounts  # 收斂 config/service_accounts.json 定義的固定服務帳號
#   ./scripts/deploy.sh gemma-4-31b  # 只部署 gemma-4-31b-vllm（思考型）
#   ./scripts/deploy.sh gemma-4-26b  # 只部署 gemma-4-26b-vllm（快捷型）
#   ./scripts/deploy.sh light-models # 只部署 light-models（embedding + marker，同一張 GPU）
#   ./scripts/deploy.sh litellm      # 只部署 litellm（含 ConfigMap）
#   ./scripts/deploy.sh admin-api    # 只部署 admin-api
#   ./scripts/deploy.sh secrets      # 只建立 Secrets
#   ./scripts/deploy.sh status       # 顯示所有 Pod/Service 狀態
#   ./scripts/deploy.sh openwebui-functions  # 只套用 OpenWebUI Functions（思考模式按鈕等）
#   ./scripts/deploy.sh priorityclasses  # 只套用浮動池 PriorityClass（gpu-priority-high/medium/low）
#   ./scripts/deploy.sh monitoring    # 只套用 Prometheus + node-exporter（精簡版，選配）
#   ./scripts/deploy.sh keda         # 只套用 KEDA ScaledObject（KEDA operator 需先用 Helm 裝好）
#   ./scripts/deploy.sh internal-lb  # 只套用內部 Traefik p2c 負載平衡（all 已包含；gemma-4-31b/26b 唯一入口，不論副本數）
#
# users-db-pvc / litellm-logs-pvc 走 storageClassName 動態佈建（.env 的
# K8S_PVC_STORAGE_CLASS，單節點 k3s 預設 local-path，公司 Ceph 叢集設
# rook-ceph-block），不再是 hostPath，admin-api 用 podAffinity 釘住 litellm 所在
# 節點才能兩者都掛上同一顆 RWO PVC。hf-cache / marker-ingest 仍是 hostPath，多節點
# 叢集：.env 設定 REGISTRY（推送 image 用）與 K8S_GPU_NODE_HOSTNAME /
# K8S_MARKER_INGEST_HOST_PATH（釘選 light-models / marker-ingest 這組 hostPath 相關
# Pod 的節點與路徑）。單節點 k3s 可留空自動推導；**多節點叢集留空會直接報錯中止**，
# 因為推導出來的本機 hostname 與開發機路徑在多節點上是無聲錯誤（詳見
# resolve_node_pinning_vars 的註解）。
# gemma-4-31b / gemma-4-26b 改走浮動 GPU 池（nodeSelector: gpu-pool=shared +
# PriorityClass 搶佔），部署前記得先對每個 GPU 節點跑過一次
# `./scripts/label-nodes.sh`，否則 pod 會卡 Pending。

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
    [[ -z "${POSTGRES_PASSWORD:-}" ]]     && warn "POSTGRES_PASSWORD 未設定，將使用預設密碼（僅供本機測試，正式環境請在 .env 設定）"

    # store_model_in_db 用（外部模型自助上架，見 docs/external-models-ops.md
    # 「路線 C」與 k8s/postgres/）。DATABASE_URL 組成的 host 固定指向
    # k8s/postgres/service.yaml 的 postgres-service，密碼取自下面的 postgres-secrets。
    local database_url="postgresql://litellm:${POSTGRES_PASSWORD:-change-me-postgres-password}@postgres-service.${NS}.svc.cluster.local:5432/litellm"

    kubectl create secret generic litellm-secrets \
        --from-literal=master-key="${LITELLM_MASTER_KEY:-sk-firdi-master-change-me}" \
        --from-literal=openwebui-url="${OPENWEBUI_URL:-}" \
        --from-literal=openwebui-admin-key="${OPENWEBUI_ADMIN_KEY:-}" \
        --from-literal=openwebui-service-key="${OPENWEBUI_SERVICE_KEY:-}" \
        --from-literal=openwebui-url-b="${OPENWEBUI_URL_B:-}" \
        --from-literal=openwebui-admin-key-b="${OPENWEBUI_ADMIN_KEY_B:-}" \
        --from-literal=openwebui-service-key-b="${OPENWEBUI_SERVICE_KEY_B:-}" \
        --from-literal=langfuse-public-key="${LANGFUSE_PUBLIC_KEY:-}" \
        --from-literal=langfuse-secret-key="${LANGFUSE_SECRET_KEY:-}" \
        --from-literal=langfuse-host="${LANGFUSE_HOST:-}" \
        --from-literal=database-url="$database_url" \
        --namespace="$NS" \
        --dry-run=client -o yaml | kubectl apply -f -

    # postgres 官方 image 只在「PVC 內第一次 initdb」時套用 POSTGRES_PASSWORD，之後
    # 每次重啟都不會重新套用——事後改這裡的 POSTGRES_PASSWORD 只會讓 litellm 這邊
    # 的連線字串跟 Postgres 裡實際的密碼對不上（litellm 開始連不上、日誌出現
    # authentication failed）。真的要輪替密碼，要另外 kubectl exec 進 postgres pod
    # 跑 `ALTER USER litellm WITH PASSWORD '...'`，這裡的 secret 只是負責讓 litellm
    # 端的 DATABASE_URL 跟著更新，不會反向改動 Postgres 本身。
    kubectl create secret generic postgres-secrets \
        --from-literal=postgres-password="${POSTGRES_PASSWORD:-change-me-postgres-password}" \
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
        --from-literal=keycloak-selfservice-client-id="${KEYCLOAK_SELFSERVICE_CLIENT_ID:-}" \
        --from-literal=keycloak-selfservice-client-secret="${KEYCLOAK_SELFSERVICE_CLIENT_SECRET:-}" \
        --from-literal=admin-api-public-url="${ADMIN_API_PUBLIC_URL:-}" \
        --from-literal=keycloak-browser-url="${KEYCLOAK_BROWSER_URL:-}" \
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

    # Secret 內容更新後，已經在跑的 pod 不會自動吃到新值（env var 只在容器啟動時
    # 讀取一次）——這造成過好幾次「明明改了 .env/Secret 怎麼還是舊行為」的誤判
    # （2026-08-05：Keycloak client secret、OpenWebUI admin key 都中過招）。這裡
    # 只在對應 Deployment 已存在時才重啟，全新環境第一次跑不會有任何影響。
    for d in litellm admin-api; do
        if kubectl get deployment "$d" -n "$NS" &>/dev/null; then
            info "重啟 $d 讓它讀取最新的 Secret 內容..."
            kubectl rollout restart deployment/"$d" -n "$NS"
        fi
    done
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
    # local-path 是單節點 k3s 內建的預設值；公司多節點 kubeadm 叢集通常沒有這個
    # StorageClass（見 kubectl get storageclass），沒檢查的話 PVC 會卡 Pending
    # 卻不會有任何明顯錯誤，直到 litellm/admin-api 也跟著卡 Pending 才會發現。
    if ! kubectl get storageclass "$K8S_PVC_STORAGE_CLASS" &>/dev/null; then
        die "StorageClass「$K8S_PVC_STORAGE_CLASS」不存在（.env 的 K8S_PVC_STORAGE_CLASS），可用的有：$(kubectl get storageclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
    fi
    envsubst < "$REPO_ROOT/k8s/shared-storage/pvc.yaml" | kubectl apply -f -
    ok "PVC 完成（storageClassName: $K8S_PVC_STORAGE_CLASS）"
}

# ── Postgres（store_model_in_db 專用，見 docs/external-models-ops.md「路線 C」）──
deploy_postgres() {
    info "部署 Postgres（store_model_in_db 專用）..."
    export K8S_PVC_STORAGE_CLASS="${K8S_PVC_STORAGE_CLASS:-local-path}"
    if ! kubectl get storageclass "$K8S_PVC_STORAGE_CLASS" &>/dev/null; then
        die "StorageClass「$K8S_PVC_STORAGE_CLASS」不存在（.env 的 K8S_PVC_STORAGE_CLASS）"
    fi
    envsubst < "$REPO_ROOT/k8s/postgres/pvc.yaml" | kubectl apply -f -
    kubectl apply -f "$REPO_ROOT/k8s/postgres/deployment.yaml"
    kubectl apply -f "$REPO_ROOT/k8s/postgres/service.yaml"
    # litellm 接下來的 deploy_litellm 會需要它已經能接受連線才連得上 DB；等到
    # Ready 再往下走，避免 litellm 第一次啟動時 store_model_in_db 初始化失敗。
    kubectl rollout status deployment/postgres -n "$NS" --timeout=120s
    ok "Postgres 部署完成"
}

# ── PriorityClass（浮動 GPU 池搶佔優先權）──────────────────────────────────────
deploy_priorityclasses() {
    info "套用 PriorityClass（gpu-priority-high/medium/low/batch）..."
    kubectl apply -f "$REPO_ROOT/k8s/priorityclasses.yaml"
    ok "PriorityClass 完成"
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

    # 連續測 3 次、間隔 2 秒才判定「檔案不存在」：admin-api/litellm 若剛好在
    # Recreate 重啟過渡期間被檢查到，RWO volume 有時會有短暫沒完全 ready 的瞬間，
    # 單測一次就判定「PVC 內沒資料」曾經真的把 421 個 Keycloak 使用者誤判成全新
    # 環境、蓋成 config/users.json 的範本假資料（2026-08-05 事故）。
    local exists=0 i
    for i in 1 2 3; do
        if kubectl exec -n "$NS" "$pod" -- test -f /app/data/users.db 2>/dev/null; then
            exists=1
            break
        fi
        sleep 2
    done
    if [[ "$exists" == "1" ]]; then
        ok "users.db 已存在於 PVC，略過初始化"
        return 0
    fi

    warn "════════════════════════════════════════════════════════════════"
    warn "偵測不到 users.db，即將用 config/users.json 範本資料建立全新 DB。"
    warn "如果這不是全新環境（PVC 應該已經有正式使用者資料），現在請按 Ctrl+C"
    warn "中止，先查清楚為什麼檔案不見了，不要讓範本假資料蓋過去。"
    warn "════════════════════════════════════════════════════════════════"
    sleep 5

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

# 固定必須存在的服務帳號（account_type=service，如聊天紀錄整理、RAG pipeline 等）不像
# 人類帳號有 Keycloak webhook 自動同步，改用 config/service_accounts.json 宣告 + 這支
# 冪等腳本收斂：帳號不存在就建立（新 key 只印一次），已存在只同步 models/rate limit/
# metadata 等設定欄位，絕不覆蓋既有 api_key。需要 admin-api 已可連線才能執行。
deploy_service_accounts() {
    info "收斂固定服務帳號（config/service_accounts.json）..."
    local pod
    pod=$(kubectl get pod -n "$NS" -l app=admin-api -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "$pod" ]]; then
        warn "找不到 admin-api Pod，略過服務帳號收斂（admin-api 部署好之後執行 ./scripts/deploy.sh service-accounts 手動補上）"
        return 0
    fi
    if ! kubectl wait -n "$NS" "pod/$pod" --for=condition=Ready --timeout=120s >/dev/null 2>&1; then
        warn "admin-api Pod 尚未 Ready，略過服務帳號收斂（稍後執行 ./scripts/deploy.sh service-accounts 手動補上）"
        return 0
    fi

    python3 "$REPO_ROOT/scripts/seed_service_accounts.py"
    ok "服務帳號收斂完成"
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

# ── 節點釘選變數的解析與防呆 ──────────────────────────────────────────────────
# K8S_GPU_NODE_HOSTNAME 與 K8S_MARKER_INGEST_HOST_PATH 只有 light-models 的
# nodeSelector 與 marker-ingest PV 的 nodeAffinity/hostPath 在吃（31b/26b 已改走
# gpu-pool=shared 浮動池）。這兩個變數以前未設定時會**靜默** fallback 成
# `$(hostname)` 與開發機路徑 /home/ai-x/data/docblock/ingest——在單節點開發機剛好
# 都是對的，但在多節點叢集兩者都會出錯，而且是無聲出錯：
#   * 2026-08-19 發現正式叢集的 light-models 被釘在「登入用的無 GPU 入口節點」，
#     Pending 了 14 天沒人察覺（imageID 是 <none>，連 image 都沒 pull 過）。
#   * 同一台的 marker-ingest PV 帶著開發機路徑 Bound 了 15 天。PV 的 hostPath
#     type 是 DirectoryOrCreate，kubelet 會在節點上靜默建一個空目錄，marker 轉檔
#     會「成功」但檔案寫進沒有人讀的地方——最難查的那種失敗。
# 因此：單節點維持自動推導（但一定印 warn），多節點一律要求 .env 顯式設定。
resolve_node_pinning_vars() {
    local nodes count gpu_nodes
    nodes="$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)" \
        || die "kubectl get nodes 失敗，無法判斷是單節點還是多節點叢集（請確認 kubeconfig）"
    [[ -n "$nodes" ]] || die "kubectl get nodes 回傳空清單，無法判斷節點拓樸"
    count="$(wc -w <<<"$nodes")"
    # 有 nvidia.com/gpu allocatable 的節點清單（device plugin 沒裝好時會是空的）
    gpu_nodes="$(kubectl get nodes -o go-template='{{range .items}}{{if index .status.allocatable "nvidia.com/gpu"}}{{.metadata.name}} {{end}}{{end}}' 2>/dev/null || true)"

    if [[ -z "${K8S_GPU_NODE_HOSTNAME:-}" ]]; then
        if (( count > 1 )); then
            die "多節點叢集（$count 個節點：$nodes）必須在 .env 顯式設定 K8S_GPU_NODE_HOSTNAME，不可留空。
       light-models 的 nodeSelector 與 marker-ingest PV 的 nodeAffinity 用它硬釘節點；留空會被推導成
       本機 hostname「$(hostname)」，若那台沒有 GPU，pod 會永遠 Pending 且不會有明顯的錯誤訊息。
       目前具備 nvidia.com/gpu 的節點：${gpu_nodes:-（查不到任何一台，請先確認 device plugin，見 docs/deploy.md 第 0 節）}"
        fi
        export K8S_GPU_NODE_HOSTNAME="$nodes"
        warn "K8S_GPU_NODE_HOSTNAME 未設定：單節點叢集，自動使用節點名稱「$K8S_GPU_NODE_HOSTNAME」"
    fi

    grep -qw -- "$K8S_GPU_NODE_HOSTNAME" <<<"$nodes" \
        || die "K8S_GPU_NODE_HOSTNAME=「$K8S_GPU_NODE_HOSTNAME」不是這個叢集的節點，light-models 會永遠 Pending。
       現有節點：$nodes"
    if ! grep -qw -- "$K8S_GPU_NODE_HOSTNAME" <<<"${gpu_nodes:- }"; then
        warn "節點「$K8S_GPU_NODE_HOSTNAME」目前沒有可配置的 nvidia.com/gpu，而 light-models 需要 1 張卡，會卡 Pending。
       具備 GPU 的節點：${gpu_nodes:-（無）}。device plugin 尚未裝好見 docs/deploy.md 第 0 節；
       若這台本來就不該當 GPU 節點，請改 .env 的 K8S_GPU_NODE_HOSTNAME。"
    fi

    if [[ -z "${K8S_MARKER_INGEST_HOST_PATH:-}" ]]; then
        if (( count > 1 )); then
            die "多節點叢集（$count 個節點）必須在 .env 顯式設定 K8S_MARKER_INGEST_HOST_PATH，不可留空。
       這個路徑必須與 docblock-rag-platform 的 docblock-ingest-pv 指向同一份檔案系統，否則
       marker-service 讀不到 ingest-worker 寫入的 PDF。留空會被推導成開發機的
       /home/ai-x/data/docblock/ingest，而 PV 的 hostPath type 是 DirectoryOrCreate——
       kubelet 會在節點上靜默建一個空目錄，轉檔看起來成功但寫進沒有人讀的地方。"
        fi
        export K8S_MARKER_INGEST_HOST_PATH="/home/ai-x/data/docblock/ingest"
        warn "K8S_MARKER_INGEST_HOST_PATH 未設定：單節點叢集，沿用預設「$K8S_MARKER_INGEST_HOST_PATH」"
    fi
}

# PV 的 hostPath 與 nodeAffinity 建立後不可變（kubectl apply 改不動，也不會用明顯的
# 方式抱怨），所以 apply 之前先比對叢集現況與 .env，不一致就停下來講清楚怎麼處理。
check_marker_ingest_pv_drift() {
    local pv="marker-ingest-pv" cur_path cur_node
    kubectl get pv "$pv" &>/dev/null || return 0   # 還沒建立過，等下 apply 時一併建立

    cur_path="$(kubectl get pv "$pv" -o jsonpath='{.spec.hostPath.path}' 2>/dev/null || true)"
    cur_node="$(kubectl get pv "$pv" -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}' 2>/dev/null || true)"
    [[ "$cur_path" == "$K8S_MARKER_INGEST_HOST_PATH" && "$cur_node" == "$K8S_GPU_NODE_HOSTNAME" ]] && return 0

    die "現有 PV「$pv」與 .env 不一致，而 PV 的 hostPath / nodeAffinity 建立後不可變：
       叢集現況：path=${cur_path:-<空>}  node=${cur_node:-<空>}
       .env 期望：path=$K8S_MARKER_INGEST_HOST_PATH  node=$K8S_GPU_NODE_HOSTNAME
       要改成 .env 的值，必須刪掉重建（PV 是 Retain + hostPath，刪 PV 物件不會刪掉節點上的檔案）：
         kubectl -n $NS scale deploy/light-models-vllm --replicas=0
         kubectl -n $NS delete pvc marker-ingest-pvc
         kubectl delete pv marker-ingest-pv
         ./scripts/deploy.sh light-models
       若叢集現況才是對的，請改 .env 對齊它，不要刪 PV。"
}

# ── vLLM 部署 helper ──────────────────────────────────────────────────────────
deploy_vllm() {
    local name="$1"   # gemma-4-31b / gemma-4-26b / light-models
    local dir="$REPO_ROOT/k8s/vllm/$name"

    # 所有 vLLM Deployment 的 podSpec 都指名 runtimeClassName: nvidia。k3s 單節點
    # 通常內建就有這個 RuntimeClass，標準 kubeadm 叢集完全不會自動生成——沒有的話
    # Pod 會在建立階段就被直接拒絕（連 kubectl get pods 都看不到），需要另外查
    # events 才找得到原因。見 G02 GPU 節點設定文件「建立 RuntimeClass」那步。
    if ! kubectl get runtimeclass nvidia &>/dev/null; then
        die "RuntimeClass「nvidia」不存在，GPU pod 會被直接拒絕建立。手動建立：
  kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF"
    fi

    # K8S_GPU_NODE_HOSTNAME / K8S_MARKER_INGEST_HOST_PATH 只有 light-models 與
    # marker-ingest PV 在吃（31b/26b 已改走浮動池 nodeSelector: gpu-pool=shared），
    # 所以只在這個分支解析與檢查——部署 31b/26b 不該被這兩個變數擋住。
    if [[ "$name" == "light-models" ]]; then
        resolve_node_pinning_vars
        check_marker_ingest_pv_drift

        info "Build light-models combo image..."
        build_and_publish_image "firdi-light-models:latest" "$REPO_ROOT" "$REPO_ROOT/marker-service/Dockerfile"
        export LIGHT_MODELS_IMAGE="$IMAGE_REF"
        export LIGHT_MODELS_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY"

        info "套用 marker 共用 ingest-data PV/PVC（node: $K8S_GPU_NODE_HOSTNAME，path: $K8S_MARKER_INGEST_HOST_PATH）..."
        envsubst < "$REPO_ROOT/k8s/shared-storage/marker-ingest-pvc.yaml" | kubectl apply -f -
    fi

    if [[ "$name" == "light-models" ]]; then
        info "部署 $name vLLM（GPU node: $K8S_GPU_NODE_HOSTNAME，hostname 硬釘）..."
    else
        info "部署 $name vLLM（浮動池：gpu-pool=shared）..."
    fi
    if [[ -z "${K8S_HF_CACHE_HOST_PATH:-}" ]]; then
        export K8S_HF_CACHE_HOST_PATH="/opt/firdi/hf-cache"
        warn "K8S_HF_CACHE_HOST_PATH 未設定，沿用預設「$K8S_HF_CACHE_HOST_PATH」——若節點上既有的快取在別的路徑，模型會整份重新下載"
    fi
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

    # CronJob 套用要放在 rollout status 之前：admin-api 第一次部署、或前面
    # PVC/RWO 死鎖排除中，rollout 常常會等超過 90 秒，set -e 一逾時就整支腳本
    # 中止，CronJob 永遠沒被套用到（且不會有任何錯誤提示，只會發現同步一直沒動靜）。
    kubectl apply -f "$REPO_ROOT/k8s/admin-api/cronjob-pull-sync.yaml"
    ok "openwebui-pull-sync CronJob 已套用"

    kubectl rollout restart deployment/admin-api -n "$NS"
    kubectl rollout status deployment/admin-api -n "$NS" --timeout=90s
    ok "Admin API 部署完成（image: $ADMIN_API_IMAGE）"
}

# ── OpenWebUI Functions ───────────────────────────────────────────────────────
# 刻意不納入 deploy_all：OpenWebUI 由另一個 repo 部署（在 default namespace），這步是「對外部系統
# 做設定」而不是部署本 repo 的元件；OpenWebUI 還沒起來的新環境不該讓 deploy.sh all 卡在這裡。
deploy_openwebui_functions() {
    info "套用 OpenWebUI Functions..."

    [[ -z "${OPENWEBUI_URL:-}" ]]       && die "OPENWEBUI_URL 未設定（請檢查 .env）"
    [[ -z "${OPENWEBUI_ADMIN_KEY:-}" ]] && die "OPENWEBUI_ADMIN_KEY 未設定（請檢查 .env）"

    # ConfigMap 從整個目錄動態產生（子目錄如 __pycache__ 會被 kubectl 略過）：
    # 新增 function 只要把匯出的 json 丟進 openwebui/functions/，這裡跟 Job yaml 都不用改
    kubectl create configmap openwebui-functions -n "$NS" \
        --from-file="$REPO_ROOT/openwebui/functions" \
        --dry-run=client -o yaml | kubectl apply -f -

    # Job 的 spec 不可變更，重跑前必須先砍掉舊的
    kubectl delete job openwebui-apply-functions -n "$NS" --ignore-not-found > /dev/null
    kubectl apply -f "$REPO_ROOT/k8s/openwebui/job-apply-functions.yaml"

    local deadline=$((SECONDS + 300)) result=""
    while (( SECONDS < deadline )); do
        [[ $(kubectl get job openwebui-apply-functions -n "$NS" \
             -o jsonpath='{.status.succeeded}' 2>/dev/null) == "1" ]] && { result=ok; break; }
        [[ -n $(kubectl get job openwebui-apply-functions -n "$NS" \
             -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null) ]] && { result=fail; break; }
        sleep 3
    done

    kubectl logs job/openwebui-apply-functions -n "$NS" 2>/dev/null | sed 's/^/       /' || true
    case "$result" in
        ok)   ok "OpenWebUI Functions 已套用" ;;
        fail) die "Job 失敗，詳情：kubectl describe job/openwebui-apply-functions -n $NS" ;;
        *)    die "Job 300 秒內未結束，詳情：kubectl describe job/openwebui-apply-functions -n $NS" ;;
    esac
}

# ── KEDA ScaledObject ─────────────────────────────────────────────────────────
# 刻意不納入 deploy_all：KEDA operator 本身是一次性 cluster bootstrap，用 Helm 裝
# （不在 deploy.sh 管理範圍）：
#   helm repo add kedacore https://kedacore.github.io/charts && helm repo update
#   helm install keda kedacore/keda --namespace keda --create-namespace
# 這裡只管 k8s/keda/ 底下的 ScaledObject（针對個別模型接上 KEDA），KEDA operator
# 沒裝的話 apply 會因為 CRD 不存在而失敗。
deploy_keda() {
    info "套用 KEDA ScaledObject..."
    for f in "$REPO_ROOT"/k8s/keda/*.yaml; do
        kubectl apply -f "$f"
    done
    ok "KEDA ScaledObject 套用完成"
}

# ── Monitoring（精簡版 Prometheus + node-exporter）───────────────────────────────
# 刻意不納入 deploy_all：這是選配的觀測性元件（2026-07-24 決定精簡版，只抓
# ai-platform 內有 prometheus.io/scrape annotation 的 pod，5 天 retention），
# 不是每個新環境一定要有的核心服務；見 k8s/monitoring/、docs/gpu-optimization.md。
deploy_monitoring() {
    info "部署 Monitoring（Prometheus + node-exporter）..."
    kubectl apply -f "$REPO_ROOT/k8s/monitoring/rbac.yaml"
    kubectl apply -f "$REPO_ROOT/k8s/monitoring/configmap.yaml"
    export K8S_PVC_STORAGE_CLASS="${K8S_PVC_STORAGE_CLASS:-local-path}"
    envsubst < "$REPO_ROOT/k8s/monitoring/prometheus-deployment.yaml" | kubectl apply -f -
    kubectl apply -f "$REPO_ROOT/k8s/monitoring/node-exporter-daemonset.yaml"
    ok "Monitoring 套用完成（Prometheus UI：kubectl port-forward -n $NS svc/prometheus 9090:9090）"
}

# ── Internal LB（專用內部 Traefik，p2c 對 31b/26b 多副本做 least-request 分流）──────
# 2026-07-28 起 config/litellm_config.yaml 的 gemma-4-31b/26b api_base 已經改成
# 單筆寫死指向 internal-lb（不再依 maxReplicaCount 動態列多筆），代表不管副本數
# 是 1 還是多顆，internal-lb 都是這兩個 model 唯一的入口——沒部署的話 litellm 完
# 全打不到 vLLM（2026-08-05 花了很長時間才追到根因：Traefik pod 正常 Running，
# 只是沒有 Traefik CRD 導致零路由規則，所有請求固定 404）。因此已經納入 deploy_all，
# 不再是選配步驟。見 k8s/internal-lb/、docs/gpu-optimization.md「6. least-request LB」。
deploy_internal_lb() {
    info "部署 Internal LB（Traefik p2c）..."

    # ingressroutes.yaml 用的 IngressRoute/Middleware/ServersTransport 是 Traefik
    # 自訂的 CRD（traefik.io/v1alpha1）。k3s 內建 Traefik 當預設 ingress
    # controller，CRD 是內建好的；標準 kubeadm 叢集完全沒有，apply 這個 yaml 會
    # 直接失敗——但 rbac.yaml / deployment.yaml 這兩步不受影響，Traefik pod 照樣
    # 會正常 Running，只是完全沒有任何路由規則，導致所有請求都回 404，且沒有
    # 任何明顯的錯誤提示（2026-08-05 花了很長時間才追到這裡）。
    if ! kubectl get crd ingressroutes.traefik.io &>/dev/null; then
        die "Traefik CRD 不存在（ingressroutes.traefik.io），internal-lb 部署了也不會有任何路由規則。先安裝 CRD：
  kubectl apply -f https://raw.githubusercontent.com/traefik/traefik/v3.6/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml
再重跑 ./scripts/deploy.sh internal-lb"
    fi

    kubectl apply -f "$REPO_ROOT/k8s/internal-lb/rbac.yaml"
    kubectl apply -f "$REPO_ROOT/k8s/internal-lb/deployment.yaml"
    kubectl apply -f "$REPO_ROOT/k8s/internal-lb/ingressroutes.yaml"
    ok "Internal LB 套用完成"
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
    deploy_postgres
    deploy_priorityclasses
    deploy_internal_lb
    deploy_vllm gemma-4-31b
    deploy_vllm gemma-4-26b
    deploy_vllm light-models
    deploy_litellm
    seed_users_db
    deploy_admin_api
    deploy_service_accounts
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
        postgres)     deploy_postgres ;;
        users-db)     seed_users_db ;;
        service-accounts) deploy_service_accounts ;;
        secrets)      deploy_secrets ;;
        priorityclasses) deploy_priorityclasses ;;
        gemma-4-31b)  deploy_vllm gemma-4-31b ;;
        gemma-4-26b)  deploy_vllm gemma-4-26b ;;
        light-models) deploy_vllm light-models ;;
        litellm)      deploy_litellm ;;
        admin-api)    deploy_admin_api ;;
        status)       show_status ;;
        openwebui-functions) deploy_openwebui_functions ;;
        monitoring)   deploy_monitoring ;;
        keda)         deploy_keda ;;
        internal-lb)  deploy_internal_lb ;;
        *)
            echo "用法: $0 [all|storage|postgres|priorityclasses|users-db|service-accounts|secrets|gemma-4-31b|gemma-4-26b|light-models|litellm|admin-api|openwebui-functions|monitoring|keda|internal-lb|status]"
            exit 1
            ;;
    esac
}

main "$@"
