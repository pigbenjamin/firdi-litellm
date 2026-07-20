# firdi-litellm — AI 算力平台

以 K8s 部署地端 vLLM 服務，LiteLLM Proxy 統一接取雲端與地端模型，並以部門→使用者兩層權限管控模型存取與流量。

## 整體架構

```
使用者 (Bearer <personal-key>)
  │
  ▼
LiteLLM Proxy (K8s, :30400)
  ├─ custom_auth.py  — 使用者驗證 → 部門權限 → 模型 allowlist → dept rate limit
  ├─ custom_logger.py — 呼叫 log + OpenRouter API key 動態注入
  │    └─ SQLite (PVC: users-db-pvc) ◄── Admin API (K8s, :30408)
  │
  ├─► gemma-4-31b-vllm-service (GPU 0+1, TP=2) — google/gemma-4-31B-it（思考型）
  ├─► gemma-4-26b-vllm-service (GPU 2,    TP=1) — google/gemma-4-26B-A4B-it（快捷型）
  ├─► embed-vllm-service      (GPU 3,  :8000)  — google/embeddinggemma-300m
  ├─► ollama-service          (可選，臨時需求)
  └─► OpenRouter API          (雲端，各部門各自的 API key)
```

使用者與部門的**模型權限**儲存在 SQLite（`users.db`），由 Admin API 管理；Keycloak 負責身份認證，兩者職責分離。

## 目錄結構

```
├── k8s/                         ← K8s manifests（新架構主目錄）
│   ├── namespace.yaml
│   ├── shared-storage/
│   │   └── pvc.yaml             — PVC: users-db-pvc（SQLite）、litellm-logs-pvc（動態佈建，storageClassName 見 .env）
│   ├── vllm/
│   │   ├── gemma-4-31b/         — deployment.yaml, service.yaml（GPU 0+1, TP=2）
│   │   ├── gemma-4-26b/         — deployment.yaml, service.yaml（GPU 2, TP=1）
│   │   └── embed/               — deployment.yaml, service.yaml（GPU 3）
│   ├── litellm/
│   │   ├── deployment.yaml
│   │   ├── service.yaml         — NodePort 30400
│   │   └── secrets.yaml         — Secret 建立說明（不含真實值）
│   ├── admin-api/
│   │   ├── deployment.yaml      — Admin API Pod
│   │   ├── service.yaml         — NodePort 30408
│   │   └── cronjob-pull-sync.yaml — OpenWebUI → DB 權限 pull 同步（每 2 分鐘）
│   └── ollama/                  — 可選
├── admin-api/                   ← 管理 API 原始碼（FastAPI）
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── auth.py
│   ├── routers/
│   │   ├── departments.py       — 部門 CRUD
│   │   ├── users.py             — 使用者 CRUD + block/unblock + regenerate-key
│   │   ├── models.py            — 可用模型清單（代理 LiteLLM /models）
│   │   ├── openwebui.py         — OpenWebUI ↔ DB 模型權限 pull/push 同步
│   │   └── sync.py              — Keycloak webhook 接收 + bulk 同步
│   ├── requirements.txt
│   └── Dockerfile
├── keycloak/
│   ├── SETUP.md                 — Keycloak 插件安裝說明
│   └── plugins/
│       └── keycloak-user-sync-listener/  — 使用者事件 webhook 轉發插件（Java/Maven）
├── docs/
│   ├── deploy.md                — 新機器部署完整 checklist
│   ├── admin-api.md             — Admin API 完整接口文件
│   └── permission-sync.md       — 模型權限同步架構與 SOP（OpenWebUI 主導）
├── config/                      ← LiteLLM + auth 設定（K8s 版）
│   ├── litellm_config.yaml
│   ├── custom_auth.py           — 從 SQLite 讀取使用者/部門設定
│   ├── custom_logger.py
│   └── users.json               — 舊格式保留，供 migrate 腳本使用
├── scripts/
│   ├── deploy.sh                — 一鍵部署（all / 單一元件，見檔頭用法）
│   ├── migrate_users_json.py    — 一次性將 users.json 匯入 SQLite
│   ├── migrate_model_names.sh   — 換模型時遷移 DB 權限中的模型名稱（自動備份 + bump db_version）
│   ├── show_db.sh               — 快速檢視 users.db 內容
│   └── test*.sh / test_sync.py  — auth / admin-api / 同步 測試腳本
└── docker-compose/              ← 舊架構保留（參考用）
```

## 硬體 GPU 分配

| GPU | 服務 | 模型 | 定位 | 關鍵 vLLM 參數 |
|-----|------|------|------|----------------|
| GPU 0+1 | gemma-4-31b-vllm | google/gemma-4-31B-it | 思考型：長 CoT、深度推理 | TP=2, max-model-len=65536, max-num-seqs=2, thinking 預設**開** |
| GPU 2 | gemma-4-26b-vllm | google/gemma-4-26B-A4B-it（MoE 25.2B/A3.8B） | 快捷型：快速問答、高並發 | TP=1, max-model-len=32768, max-num-seqs=256, thinking 預設**關** |
| GPU 3 | embed-vllm | google/embeddinggemma-300m | 向量化 | gpu-mem-util=0.15（300M 小模型；卡上餘裕留作未來擴充） |

GPU 由 NVIDIA Device Plugin 自動分配，不需手動指定 GPU index。
獨立 rerank 服務（Qwen3-Reranker-8B）已於 2026-07 移除：無實際流量、且依去中國模型政策無乾淨替代品；需要重排序時改以一般 LLM（如 gemma-4-26B-A4B-it）做 listwise rerank。

### vLLM 部署要點（Gemma 4）

- **冷啟動很慢是正常的**：首次啟動要下載權重 + torch.compile + CUDA graph 編譯，26B 實測約 15 分鐘、31B（TP=2）更久。startup probe 已放寬到 21 分鐘預算（`failureThreshold: 40`），不要調回去——預算不足會在快編完時被 kubelet 殺掉，陷入重編循環。
- **編譯快取已持久化**：`VLLM_CACHE_ROOT` 指到 HF cache hostPath（`vllm-compile-cache/`），暖重啟約 2~4 分鐘。升級 vLLM 版本或改模型參數後第一次啟動仍會全額重編。
- **更新 deployment 用 Recreate**：單副本 GPU 工作負載已設 `strategy: Recreate`（先殺舊 pod 釋放 GPU 再起新 pod）；RollingUpdate 在 GPU 佔滿時會死鎖。

## 快速部署

> 在同一台機器上重新部署／換模型時可直接照本節操作。若是把整個平台搬到一台全新機器，請改用 [docs/deploy.md](docs/deploy.md)，裡面補了 `.env`／`data/` 重建、既有資料搬遷、GPU 規格核對等新機器才會遇到的步驟。

### 1. 確認 K8s GPU 環境

```bash
kubectl get nodes -o json | jq '.items[].status.capacity'
# 應看到 "nvidia.com/gpu": "4"
```

若無 GPU 資源，先安裝 NVIDIA GPU Operator：

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace
```

### 2. 建立 Namespace 與 Secrets

```bash
kubectl apply -f k8s/namespace.yaml

# LiteLLM master key（同時供 Admin API 查詢 /models 使用）
kubectl create secret generic litellm-secrets \
  --from-literal=master-key=sk-firdi-master-CHANGE-ME \
  -n ai-platform

# HuggingFace token（下載 gated 模型用）
kubectl create secret generic hf-token \
  --from-literal=token=<your-hf-token> \
  -n ai-platform

# Admin API 全部 secrets（包含 Keycloak 連線參數）
kubectl create secret generic admin-api-secrets \
  --from-literal=api-key=<admin-api-key-CHANGE-ME> \
  --from-literal=webhook-secret=<webhook-secret-CHANGE-ME> \
  --from-literal=keycloak-url=https://<keycloak-host>:<port>/ \
  --from-literal=keycloak-realm=<realm-name> \
  --from-literal=keycloak-client-id=user-sync-service \
  --from-literal=keycloak-client-secret=<client-secret> \
  --from-literal=keycloak-ssl-verify=false \
  -n ai-platform
```

### 3. 建立 ConfigMaps

```bash
kubectl create configmap litellm-config \
  --from-file=litellm_config.yaml=config/litellm_config.yaml \
  -n ai-platform

kubectl create configmap litellm-custom-auth \
  --from-file=custom_auth.py=config/custom_auth.py \
  -n ai-platform

kubectl create configmap litellm-custom-logger \
  --from-file=custom_logger.py=config/custom_logger.py \
  -n ai-platform
```

### 4. 建立 PVC 並匯入初始資料

`users-db-pvc`（SQLite）與 `litellm-logs-pvc` 都是 `storageClassName` 動態佈建，`.env` 的 `K8S_PVC_STORAGE_CLASS` 決定用哪個 storage class（單節點 k3s 用內建的 `local-path`；公司叢集看 `kubectl get storageclass` 選一個支援 `ReadWriteOnce` 的，例如 `rook-ceph-block`）。它們是 RWO，admin-api 用 `podAffinity` 釘住 litellm 所在節點，兩者才能同時掛上 `users-db-pvc`（見 `k8s/admin-api/deployment.yaml`），不需要手動排程處理。

```bash
kubectl get storageclass   # 確認叢集支援的 storage class 名稱

# 建立 PVC（會展開 .env 的 K8S_PVC_STORAGE_CLASS）
source .env && envsubst < k8s/shared-storage/pvc.yaml | kubectl apply -f -
```

PVC 是動態佈建的，本機沒有檔案能直接對應到裡面的內容，`users.db` 要用 `kubectl cp` 灌進已經掛載 `users-db-pvc` 的 litellm Pod（`./scripts/deploy.sh users-db` 會自動做這件事，邏輯見 `docs/deploy.md` 第 3 節；下面是手動版本）：

```bash
# 先確保 litellm 已部署且 Pod Ready（見第 7 節），才有 Pod 可以 kubectl cp 進去
POD=$(kubectl get pod -n ai-platform -l app=litellm -o jsonpath='{.items[0].metadata.name}')

# 產生 SQLite DB（從現有 users.json 匯入）
python3 scripts/migrate_users_json.py \
  --json config/users.json \
  --db /tmp/users.db

# 直接複製進 litellm Pod 掛載的 users-db-pvc
kubectl cp /tmp/users.db "ai-platform/$POD:/app/data/users.db"
```

### 5. 部署 Admin API

`k8s/admin-api/deployment.yaml` 的 `image` 欄位是 `${ADMIN_API_IMAGE}` 模板變數，需要 `envsubst` 展開，不能直接 `kubectl apply -f`。用 `./scripts/deploy.sh admin-api` 部署（會自動 build image，單節點 k3s 直接 import，設定 `.env` 的 `REGISTRY` 則 push 到 registry，多節點叢集見 [docs/deploy.md](docs/deploy.md) 第 4 節）：

```bash
./scripts/deploy.sh admin-api
```

Admin API 啟動後在 `http://<node-ip>:30408`，API 文件在 `/docs`。

### 6. 部署 vLLM 服務

```bash
# 依序部署（首次啟動需下載模型 + 編譯，Gemma 4 約 15~25 分鐘，見「vLLM 部署要點」）
kubectl apply -f k8s/vllm/gemma-4-31b/
kubectl apply -f k8s/vllm/gemma-4-26b/
kubectl apply -f k8s/vllm/embed/

# 觀察 Pod 狀態
kubectl get pods -n ai-platform -w
```

> 以上步驟 2~7 也可以直接用 `./scripts/deploy.sh`（讀取 `.env`）一鍵完成，或用 `./scripts/deploy.sh gemma-4-31b` 等指令部署單一元件。

### 7. 部署 LiteLLM

```bash
kubectl apply -f k8s/litellm/deployment.yaml
kubectl apply -f k8s/litellm/service.yaml
```

LiteLLM 對外服務在 `http://<node-ip>:30400`。

### 更換模型 SOP

換模型（如本次 Qwen → Gemma 4）牽涉多處，依序處理：

1. `k8s/vllm/<model>/deployment.yaml` — 模型名、`--reasoning-parser`、thinking 預設、資源需求
2. `config/litellm_config.yaml` — `model_name`（對外名稱）與 `api_base`
3. 先刪舊 deployment/service 釋放 GPU，再部署新的（改名部署 K8s 不會自動取代同功能舊資源）
4. `./scripts/deploy.sh litellm && kubectl rollout restart deployment/litellm -n ai-platform`
5. `./scripts/migrate_model_names.sh` — 把 DB 權限（departments.allowed_models / users.models）中的舊模型名換成新名，自動備份並 bump db_version（30 秒內生效）；OpenWebUI 側的模型授權也要跟著補（pull 同步以 OpenWebUI 為權威）
6. 磁碟空間：新模型下載前確認餘裕（大模型下載可能觸發節點 DiskPressure，造成全 namespace 驅逐）

## 可用模型

| 模型名稱 | 實際模型 | 定位 | 位置 |
|---------|---------|------|------|
| `gemma-4-31B-it` | google/gemma-4-31B-it | 思考型（深度推理、長 CoT） | gemma-4-31b-vllm-service:8000 |
| `gemma-4-26B-A4B-it` | google/gemma-4-26B-A4B-it | 快捷型（快速問答、高並發） | gemma-4-26b-vllm-service:8000 |
| `embeddinggemma-300m` | google/embeddinggemma-300m | 向量化 | embed-vllm-service:8000 |
| `openrouter/<provider>/<model>` | 各家雲端模型 | 雲端 | OpenRouter API |
| `ollama/<model>` | 任意 Ollama 模型 | 臨時地端 | ollama-service:11434 |

## 使用方式

### 思考型 LLM（深度推理、長 CoT）

`gemma-4-31B-it` 預設**開啟** thinking，適合深度推理任務：

```bash
curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-eng-user-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma-4-31B-it", "messages": [{"role": "user", "content": "請分析以下程式碼的時間複雜度..."}]}'
```

### 快捷型 LLM（快速問答）

`gemma-4-26B-A4B-it` 預設**關閉** thinking，直接回答：

```bash
curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-eng-user-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma-4-26B-A4B-it", "messages": [{"role": "user", "content": "你好"}]}'
```

### Thinking 模式控制（兩顆 Gemma 4 通用）

每個請求可用 `chat_template_kwargs` 覆寫預設值，例如讓快捷型也思考：

```bash
curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-eng-user-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-4-26B-A4B-it",
    "messages": [{"role": "user", "content": "9.11 和 9.9 哪個大?"}],
    "max_tokens": 2000,
    "chat_template_kwargs": {"enable_thinking": true}
  }'
```

注意事項：

- 思考內容在回應的 **`message.reasoning`** 欄位（不是 `reasoning_content`），`message.content` 只含最終答案
- 開 thinking 時 `max_tokens` 要給足（思考本身可能耗數百 token）；太小會導致 content 空白
- 思考 token 一樣計入用量與 TPM 限額

### Embedding

```bash
curl http://<node-ip>:30400/v1/embeddings \
  -H "Authorization: Bearer sk-dev-eng-user-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "embeddinggemma-300m", "input": "這是要 embed 的文字"}'
```

### 雲端模型（OpenRouter）

```bash
curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-eng-user-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "openrouter/anthropic/claude-sonnet-4-5", "messages": [{"role": "user", "content": "你好"}]}'
```

## 權限架構

**權威來源為 OpenWebUI** 畫面上的模型授權；由反向同步（pull）寫回 DB，custom_auth 讀 DB 做 enforcement。完整架構與操作 SOP 見 [docs/permission-sync.md](docs/permission-sync.md)。

```
部門 (Department)
  ├─ openrouter_api_key     ← 雲端模型共用金鑰（各部門獨立）
  ├─ allowed_models[]       ← 部門可用模型（來自 OpenWebUI group 授權）
  ├─ dept_rpm_limit         ← 部門每分鐘請求數上限
  ├─ dept_tpm_limit         ← 部門每分鐘 token 數上限
  └─ users[]
       ├─ api_key           ← 個人 Bearer token
       ├─ models[]          ← 個別授權給此人的「額外」模型（來自 OpenWebUI user 授權）
       ├─ rpm_limit         ← 個人每分鐘請求數上限
       └─ tpm_limit         ← 個人每分鐘 token 數上限
```

### 模型權限規則（加法模型）

使用者可用模型 = **部門授權 ∪ 個人授權**（聯集）：

- `dept.allowed_models` 有 → 部門所有成員可用
- `user.models` 有 → 額外只授權給該使用者（可超出部門）
- 兩者聯集為空 → **拒絕所有 model**（未明確授權 = 沒人能用）
- 任一邊含 `"*"` → 允許全部 model

> 個人 `models` 是「加給」不是「限縮」；一般使用者留空即繼承部門。改權限請在 OpenWebUI 設定（見 permission-sync.md）。

### 管理使用者與部門（Admin API）

所有操作使用 `Authorization: Bearer <admin-api-key>` 標頭，API 文件詳見 `http://<node-ip>:30408/docs`。

**新增部門**
```bash
curl -X POST http://<node-ip>:30408/api/v1/departments \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "dept_id": "data-science",
    "dept_name": "資料科學部",
    "openrouter_api_key": "sk-or-...",
    "allowed_models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it", "openrouter/anthropic/claude-sonnet-4-5"],
    "dept_rpm_limit": 300,
    "dept_tpm_limit": 1000000
  }'
```

**新增使用者**
```bash
curl -X POST http://<node-ip>:30408/api/v1/users \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "api_key": "sk-ds-user-001",
    "key_name": "ds-user-alice",
    "user_id": "ds-alice",
    "user_email": "alice@company.com",
    "dept_id": "data-science",
    "models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it"],
    "rpm_limit": 60,
    "tpm_limit": 200000
  }'
```

**封鎖 / 解封使用者**
```bash
curl -X POST http://<node-ip>:30408/api/v1/users/ds-alice/block \
  -H "Authorization: Bearer <admin-api-key>"

curl -X POST http://<node-ip>:30408/api/v1/users/ds-alice/unblock \
  -H "Authorization: Bearer <admin-api-key>"
```

**重新生成使用者 API Key**（例如 key 外洩時）
```bash
curl -X POST http://<node-ip>:30408/api/v1/users/ds-alice/regenerate-key \
  -H "Authorization: Bearer <admin-api-key>"
# 回傳含新 api_key 的使用者物件
```

**更新部門可用模型**（例外流程：權威來源是 OpenWebUI，DB 側 PATCH 須遵守 `pull → PATCH → push`，見 [docs/permission-sync.md](docs/permission-sync.md)）
```bash
curl -X PATCH http://<node-ip>:30408/api/v1/departments/data-science \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"allowed_models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it"]}'
```

**查詢系統可用模型清單**（UI 選取時使用）
```bash
curl http://<node-ip>:30408/api/v1/models \
  -H "Authorization: Bearer <admin-api-key>"
# {"models": ["embeddinggemma-300m", "gemma-4-26B-A4B-it", "gemma-4-31B-it"]}
```

變更**即時生效**（custom_auth.py 在下一次 auth check 時會偵測到 db_version 變化並重載快取，最慢 30 秒）。

完整接口文件詳見 [docs/admin-api.md](docs/admin-api.md)。
模型權限管理（OpenWebUI 主導）的架構與操作 SOP 詳見 [docs/permission-sync.md](docs/permission-sync.md)。

## Keycloak 使用者同步

使用者身份（user_id、email、所屬部門）由 Keycloak 透過 webhook 自動同步至 Admin API，**無需手動建立使用者帳號**。

### 同步流程

```
Keycloak 事件
  │  REGISTER / UPDATE_PROFILE（使用者自身操作）
  │  CREATE / UPDATE / DELETE（管理員操作）
  ▼
keycloak-user-sync-listener（JAR 插件）
  │  POST /api/v1/sync/keycloak
  │  Header: X-Webhook-Secret: <webhook-secret>
  ▼
Admin API
  ├─ 查詢 Keycloak Admin API 取得使用者詳情
  ├─ 解析群組路徑（/DeptName/...）→ dept_id
  ├─ 新使用者：建立帳號，分配 sk-{uuid} API key，models 留空（模型權限由 OpenWebUI pull 填入）
  ├─ 舊使用者：更新 email / username / dept_id / blocked
  └─ DELETE 事件：blocked=1（資料保留，封鎖存取）
```

### 插件安裝

詳見 [keycloak/SETUP.md](keycloak/SETUP.md)。簡要步驟：

1. `cd keycloak/plugins/keycloak-user-sync-listener && mvn package`
2. 將 `target/keycloak-user-sync-listener-1.0.0.jar` 複製到 Keycloak 的 `providers/` 目錄
3. 重啟 Keycloak，在 Realm → Events → Event listeners 啟用 `user-sync-listener`
4. 在 Keycloak Realm 設定中加入環境變數（或在插件 `ProviderFactory` 中設定）：
   - `USER_SYNC_WEBHOOK_URL`：`http://<admin-api-host>/api/v1/sync/keycloak`
   - `USER_SYNC_WEBHOOK_SECRET`：與 `WEBHOOK_SECRET` 一致

### 部門對應規則

使用者的 Keycloak 群組路徑第一段作為 `dept_id`：

| Keycloak 群組 | dept_id |
|--------------|---------|
| `/engineering` | `engineering` |
| `/engineering/backend` | `engineering` |
| `/data-science/senior` | `data-science` |

若部門不存在會自動建立（`allowed_models` 為空，需管理員手動設定）。

## 使用記錄（Log）

Log 格式（JSON Lines）位於 LiteLLM Pod 內 `/app/logs/usage.jsonl`，包含 `dept_id` 欄位：

```json
{"timestamp": "...", "event": "llm_call", "status": "success", "user_id": "eng-user-001", "key_name": "engineering-dev-user", "dept_id": "engineering", "model": "gemma-4-31B-it", "prompt_tokens": 20, "completion_tokens": 50, "total_tokens": 70, "latency_ms": 1234}
```

查看 log：

```bash
kubectl exec -n ai-platform deploy/litellm -- tail -f /app/logs/usage.jsonl
```

## 監控指標

若已部署 DCGM Exporter + Prometheus，關鍵指標：

| 指標 | 意義 |
|------|------|
| `vllm:num_requests_waiting` | Queue 積壓數，> 0 代表壅塞 |
| `vllm:gpu_cache_usage_perc` | KV cache 使用率，接近 100% 代表 throughput 瓶頸 |
| `vllm:num_requests_running` | 目前正在處理的請求數 |
| `DCGM_FI_DEV_GPU_UTIL` | GPU 計算使用率 |

## Ollama 臨時使用

4 張 GPU 已全數分配。若需臨時使用 Ollama，手動釋放 embed：

```bash
kubectl scale deploy/embed-vllm --replicas=0 -n ai-platform
# ... 使用 Ollama ...
kubectl scale deploy/embed-vllm --replicas=1 -n ai-platform
```

## 舊有架構

原 Docker Compose 架構保留於 `docker-compose/` 目錄，可用於本地開發測試：

```bash
cd docker-compose
docker compose up -d
```
