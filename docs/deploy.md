# 部署指南（新機器）

給「把整個平台搬到一台全新機器」時使用的完整 checklist。日常在同一台機器上重跑部署，直接用 README「快速部署」小節或 `./scripts/deploy.sh` 即可；這份文件補的是**新機器才會遇到的坑**：`.env` 與 `data/` 不在 git 裡、既有使用者資料要不要搬、GPU 規格可能不同、單節點 k3s 換成多節點 k8s 時要注意的 image 分發與 hostPath 排程問題。

## 0. 前置需求

新機器需要先具備：

- k8s 叢集（k3s 或標準 k8s 皆可，見第 4 節「單節點 vs 多節點」的差異）
- NVIDIA GPU + Device Plugin，`nvidia.com/gpu` 資源可被排程到
- `docker`、`kubectl`、`python3`、`envsubst`（`gettext` 套件）
- 一組已接受 `google/gemma-4-*` 授權的 HuggingFace 帳號 token（gated model，沒接受授權會下載失敗）

```bash
kubectl get nodes -o json | jq '.items[].status.capacity."nvidia.com/gpu"'
```

若無 GPU 資源，先裝 NVIDIA GPU Operator：

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace
```

## 1. Clone 專案

```bash
git clone git@github.com:pigbenjamin/firdi-litellm.git
cd firdi-litellm
```

## 2. 重建 `.env`

`.gitignore` 排除了 `.env`，git 只會帶來 `.env.example`，需要手動重建：

```bash
cp .env.example .env
```

| 類別 | 變數 | 處理方式 |
|------|------|---------|
| LiteLLM / Admin API | `LITELLM_MASTER_KEY`、`ADMIN_API_KEY`、`WEBHOOK_SECRET` | 必須改，不可沿用 `CHANGE-ME` |
| Keycloak | `KEYCLOAK_URL` / `_REALM` / `_CLIENT_ID` / `_CLIENT_SECRET` | 若接同一個 Keycloak，從舊機器 `.env` 複製過來 |
| OpenWebUI | `OPENWEBUI_URL` / `_ADMIN_KEY` / `_SERVICE_KEY` | 若接同一個 OpenWebUI，從舊機器複製 |
| HuggingFace | `HF_TOKEN` | 新機器自己的 token，需先在 HF 網站接受 Gemma 授權 |
| Langfuse | `LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY` / `_HOST` | 要觀測性才需要，可留空 |
| K8s hostPath | `K8S_DATA_HOST_PATH` / `K8S_LOGS_HOST_PATH` / `K8S_HF_CACHE_HOST_PATH` / `K8S_MARKER_INGEST_HOST_PATH` | 改成這台機器上要用的路徑；不用預先 `mkdir`，`deploy.sh` 會自動建立 |
| 多節點（單節點可留空） | `REGISTRY` / `K8S_STORAGE_NODE_HOSTNAME` / `K8S_GPU_NODE_HOSTNAME` | 見第 4 節 |

## 3. 使用者資料庫（`data/` 也不在 git 裡）

`data/*` 被 gitignore 排除，git 只帶來 `.gitkeep`。`deploy.sh storage` 預設會用 `config/users.json`（模板資料）跑 `migrate_users_json.py`，產生一個**全新空白**的 `users.db`。

- **全新環境**：直接用預設流程，之後靠 Admin API / Keycloak 同步建立使用者。
- **要延續舊機器既有使用者/部門**：先從舊機器複製 `data/users.db`（實際路徑是舊機器 `.env` 裡的 `K8S_DATA_HOST_PATH/users.db`），放到新機器對應的 `K8S_DATA_HOST_PATH` 目錄下，蓋掉 `deploy.sh` 產生的空 db；或部署完 storage 後用 `kubectl cp` 覆蓋 PVC 內的檔案。

## 4. 單節點 vs 多節點

所有 K8s 資源用的 SQLite / logs / HF cache / marker-ingest 都是 **hostPath**，本質上綁定在某一台實體節點的本地磁碟上。單節點 k3s 沒有這個問題（叢集只有一個節點，pod 不可能排到別的地方）；換成真正的多節點 k8s 叢集後，兩件事一定要處理，否則 pod 可能被排到沒有該 hostPath 目錄的節點：

### 4.1 Image 分發

`deploy.sh` 對 `admin-api` / `light-models` 是本機 `docker build`；要讓其他節點的 kubelet 抓得到，`.env` 設定 `REGISTRY`（例如 `registry.internal:5000` 或 Docker Hub 帳號）：

```bash
REGISTRY=registry.internal:5000
```

設定後 `deploy.sh` 會自動 `docker tag` + `docker push` 到該 registry，並把 deployment 的 `imagePullPolicy` 切成 `Always`（確保每個節點都抓最新版，而不是吃到本地快取的舊 `:latest`）。

留空 `REGISTRY` 時維持原本行為：k3s 環境會 `docker save` + `k3s ctr images import` 匯入本機 containerd（僅適用單節點）；非 k3s 且未設定 `REGISTRY` 則只在本機 docker，僅供單節點測試用。

**私有 registry**（例如私有的 ghcr.io package）需要登入才能 push/pull，再設定：

```bash
REGISTRY=ghcr.io/<github帳號或org>
REGISTRY_USERNAME=<github帳號>
REGISTRY_PASSWORD=<PAT，push 需要 write:packages、pull 需要 read:packages>
K8S_IMAGE_PULL_SECRET=ghcr-pull-secret   # 叢集端 pull 用的 k8s secret 名稱，自訂即可
```

`./scripts/deploy.sh secrets` 會用這組帳密：push 端 `docker login` 登入後再 push；同時（因為 `K8S_IMAGE_PULL_SECRET` 也設定了）自動建立對應的 `kubectl create secret docker-registry`。`./scripts/deploy.sh admin-api` / `light-models` 則會把這個 secret 透過 `kubectl patch` 掛到對應 Deployment 的 `imagePullSecrets`。三個變數留空時完全不影響現有單節點 k3s（不會建 secret、不會 patch）。

若不想處理認證，也可以把 GHCR package 設成 public，這樣三個變數都不用填，`REGISTRY` 照樣能 push/pull，只是叢集內任何人都能 pull 到這個 image。

**建立 GitHub PAT**：github.com → Settings → Developer settings → Personal access tokens，建 classic token，勾 `write:packages`（push 用）+ `read:packages`（叢集端 pull 用）。建議設過期日、範圍只給 packages，`.env` 裡是明碼存放（`.gitignore` 已排除 `.env` 不會進 git，但外洩風險還是自己留意）。

> ⚠️ **`deploy.sh` 沒有「只 push 不部署」的模式**：`./scripts/deploy.sh admin-api` / `light-models` 會在 build + push 之後，緊接著對**目前 kubectl context 指向的叢集**做 `kubectl apply` / `patch` / `rollout restart`。所以：
> - 在單節點開發機上跑，會連這台機器自己的 admin-api / light-models 也一起用 GHCR image 重新部署一次（`imagePullPolicy` 變 `Always`）。這台機器要能連到 `ghcr.io`；如果 package 設為 private，這台機器的 `.env` 也要填 `K8S_IMAGE_PULL_SECRET`，否則這裡的 rollout 會 `ImagePullBackOff`。
> - 目標的多節點叢集那邊**還是要自己 clone 這個 repo、填一樣的 `REGISTRY`（+ 若私有則含 `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`/`K8S_IMAGE_PULL_SECRET`）、在那邊跑 `deploy.sh`**——它會在那台機器上重新 build 一次再 push（同一個 tag，蓋掉沒差），不是只把已經 push 好的 image 抓下來就結束。

### 4.2 hostPath 節點釘選（nodeAffinity / nodeSelector）

`.env` 新增兩個變數，指定各 hostPath 實際落在哪個節點（值需與 `kubectl get nodes` 印出的 `NAME` 一致）：

```bash
kubectl get nodes   # 確認節點名稱

K8S_STORAGE_NODE_HOSTNAME=node-a   # 承載 K8S_DATA_HOST_PATH / K8S_LOGS_HOST_PATH 的節點
K8S_GPU_NODE_HOSTNAME=node-b       # 承載 K8S_HF_CACHE_HOST_PATH / K8S_MARKER_INGEST_HOST_PATH 的節點（有 GPU 的那台）
```

- `K8S_STORAGE_NODE_HOSTNAME`：寫進 `users-db-pv` 的 `nodeAffinity`。admin-api、litellm 都掛 `users-db-pvc`，scheduler 會因此自動把兩者都排到這個節點；`K8S_LOGS_HOST_PATH` 是 litellm 直接掛的 hostPath（沒有走 PVC），要記得把該目錄也建在同一個節點上。
- `K8S_GPU_NODE_HOSTNAME`：寫進 3 個 vLLM deployment 的 `nodeSelector`，以及 `marker-ingest-pv` 的 `nodeAffinity`。理論上 GPU 資源請求（`nvidia.com/gpu`）本身就會把這些 pod 排到有 GPU 的節點，但叢集若有多台 GPU 節點、且 hostPath 目錄只存在其中一台時，明確指定 `nodeSelector` 才不會排錯台。

單節點 k3s 這兩個變數留空即可，`deploy.sh` 會自動帶入 `$(hostname)`。

## 5. GPU 資源核對

`k8s/vllm/*/deployment.yaml` 目前假設 4× RTX PRO 6000（96GB）：GPU 0+1 給 `gemma-4-31b`（TP=2）、GPU 2 給 `gemma-4-26b`、GPU 3 給 `light-models`。若新機器 GPU 數量或顯存不同，部署前要調整對應 deployment 的 GPU 資源請求、`--tensor-parallel-size`、`--gpu-memory-utilization`，否則會排程失敗或 OOM。

## 6. 一鍵部署

```bash
./scripts/deploy.sh          # secrets → storage → 3個 vLLM → litellm → admin-api
./scripts/deploy.sh status   # 檢查 Pod/Service 狀態、印出 NodeIP + port
```

也可分段跑：

```bash
./scripts/deploy.sh secrets
./scripts/deploy.sh storage
./scripts/deploy.sh gemma-4-31b
./scripts/deploy.sh gemma-4-26b
./scripts/deploy.sh light-models
./scripts/deploy.sh litellm
./scripts/deploy.sh admin-api
```

vLLM 冷啟動很慢是正常現象（Gemma 4 首次下載+編譯約 15~25 分鐘），細節與 `strategy: Recreate` 等維運要點見 README「vLLM 部署要點」，不重複列在此。

## 7. 部署後驗證

```bash
kubectl get pods -n ai-platform -o wide -w   # -o wide 可順便確認 pod 排到預期的節點

curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer <一組有效的 api_key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma-4-26B-A4B-it", "messages": [{"role": "user", "content": "你好"}]}'

curl http://<node-ip>:30408/api/v1/models -H "Authorization: Bearer <ADMIN_API_KEY>"
```

## 8. 選用元件

- **Keycloak 使用者同步插件**：`deploy.sh` 不會自動裝，需另外照 [keycloak/SETUP.md](../keycloak/SETUP.md) 手動 build + 部署到 Keycloak。
- **marker-service ingest 目錄對齊**：若這台機器同時要跑 `docblock-rag-platform`，`.env` 的 `K8S_MARKER_INGEST_HOST_PATH` 必須跟該專案的 `docblock-ingest-pv` 指向同一個實體目錄，否則 marker-service 讀不到 PDF 且是**靜默失敗**（沒有錯誤訊息，只是轉檔目錄是空的）。
- **對外防火牆**：LiteLLM NodePort `30400`、Admin API NodePort `30408`，若要對外存取記得開通。

## 相關文件

- 日常換模型、模型權限管理 SOP：[README.md](../README.md)
- Admin API 完整接口：[admin-api.md](admin-api.md)
- 模型權限同步（OpenWebUI 主導）：[permission-sync.md](permission-sync.md)
- vLLM 效能調校：[gpu-optimization.md](gpu-optimization.md)
