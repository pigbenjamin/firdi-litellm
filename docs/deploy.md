# 部署指南（新機器）

給「把整個平台搬到一台全新機器」時使用的完整 checklist。日常在同一台機器上重跑部署，直接用 README「快速部署」小節或 `./scripts/deploy.sh` 即可；這份文件補的是**新機器才會遇到的坑**：`.env` 與 `data/` 不在 git 裡、既有使用者資料要不要搬、GPU 規格可能不同、單節點 k3s 換成多節點 k8s 時要注意的 image 分發與 hostPath 排程問題。

## 0. 前置需求

新機器需要先具備：

- k8s 叢集（k3s 或標準 k8s 皆可，見第 4 節「單節點 vs 多節點」的差異）
- NVIDIA GPU + Device Plugin，`nvidia.com/gpu` 資源可被排程到
- **`RuntimeClass "nvidia"`**（見下方說明，三個 vLLM deployment 的 yaml 都指定 `runtimeClassName: nvidia`，沒有這個物件 Pod 會在建立階段就被拒絕，`kubectl get pods` 甚至不會看到任何 Pod）
- `docker`、`kubectl`、`python3`、`envsubst`（`gettext` 套件）
- 一組已接受 `google/gemma-4-*` 授權的 HuggingFace 帳號 token（gated model，沒接受授權會下載失敗）

```bash
kubectl get nodes -o json | jq '.items[].status.capacity."nvidia.com/gpu"'
```

若無 GPU 資源，先裝 NVIDIA GPU Operator（Helm 版會自動連 device plugin 跟 `RuntimeClass nvidia` 一起裝好）：

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace
```

### RuntimeClass "nvidia" 檢查與手動建立

`nvidia.com/gpu` capacity 能上報，只代表 driver + device plugin 有裝；`RuntimeClass nvidia` 是另一層，containerd 要知道怎麼用 nvidia-container-runtime 起容器，兩者不會互相帶動。**k3s 單節點叢集通常內建就有這個 RuntimeClass**（`kube-system` 裡一個叫 `runtimes` 的 addon，非 GPU Operator 建的）；**標準 kubeadm 叢集完全不會自動生成**，要手動處理。先檢查：

```bash
kubectl get runtimeclass nvidia
```

沒有的話，且沒有裝 GPU Operator，需要**在每一台會跑 GPU workload 的節點各自執行**（containerd 設定是各節點獨立的，這步不能只做一次）：

```bash
# nvidia-container-toolkit 常常已經隨 driver 安裝腳本裝過，可以先確認
dpkg -l nvidia-container-toolkit 2>/dev/null || sudo apt-get install -y nvidia-container-toolkit

sudo nvidia-ctk runtime configure --runtime=containerd
sudo systemctl restart containerd
```

> 新版 containerd（config version 3）`nvidia-ctk` 會把設定寫到 `/etc/containerd/conf.d/99-nvidia.toml` 這種 drop-in 檔案，不是直接改 `/etc/containerd/config.toml`；用 `sudo containerd config dump | grep -A8 'runtimes\.nvidia'` 看合併後的實際生效設定，比直接 `grep config.toml` 準。

每台節點都設定好之後，`RuntimeClass` 物件本身是**叢集層級只需要建一次**：

```bash
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF
```

### ⚠️ Device Plugin 本身也要指定 `runtimeClassName`（2026-07-27 gpu01 踩過的坑）

上面兩步只解決「這顆節點有能力用 nvidia runtime 起容器」，**不代表任何 Pod 會自動套用它**——每個 Pod 都要自己在 spec 裡宣告 `runtimeClassName: nvidia` 才會生效，**包括 device plugin 自己**。用官方 `nvdp/nvidia-device-plugin` Helm chart（非 GPU Operator）安裝時，這個值預設是空字串、不會自動帶入，必須手動指定：

```bash
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm install nvidia-device-plugin nvdp/nvidia-device-plugin \
  -n kube-system --create-namespace \
  --set runtimeClassName=nvidia
```

**這個坑很難被發現**：漏設的話 device plugin 的 Pod 仍會正常建立、正常啟動（不會卡在建立階段，跟上面「沒有 `RuntimeClass` 物件」的症狀完全不同），只是 container 內部呼叫 NVML 初始化 GPU 時失敗，log 會看到：

```
Failed to initialize NVML: ERROR_LIBRARY_NOT_FOUND
```

host 上直接跑 `nvidia-smi`、`nvidia-container-cli info` 都完全正常（問題只發生在這個 container 沒被套用 nvidia runtime），容易誤判成驅動問題而繞遠路。症狀：`kubectl describe node` 的 Capacity/Allocatable **完全沒有 `nvidia.com/gpu` 這個 key**（不是數字 0，是整行不存在），所有請求 GPU 的 Pod 全部 `Pending`；如果節點曾經正常註冊過、後來 device plugin pod 重啟失敗，既有的 GPU workload 會變成 `UnexpectedAdmissionError`。

檢查現況：

```bash
kubectl get daemonset -n kube-system nvidia-device-plugin -o yaml | grep runtimeClassName
```

沒有輸出就是漏了，補上（`--version` 記得釘住目前版本號，避免順便跳版）：

```bash
helm upgrade nvidia-device-plugin nvdp/nvidia-device-plugin -n kube-system \
  --version <目前版本> --reuse-values --set runtimeClassName=nvidia
```

這個設定寫進 Helm release / etcd 裡，跟上面 containerd 設定、`RuntimeClass` 物件一樣是持久化的宣告式狀態，**重開機不會消失、不需要每次重做**。

> 若走 GPU Operator 安裝（見上方），這件事通常由 Operator 內部處理好，但建議裝完還是照上面指令查一次確認。

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
| Postgres | `POSTGRES_PASSWORD` | LiteLLM `store_model_in_db` 用（見 [external-models-ops.md「路線 C」](external-models-ops.md)），必須改，不可沿用 `change-me` |
| K8s PVC / hostPath | `K8S_PVC_STORAGE_CLASS`（users-db-pvc/litellm-logs-pvc/postgres-data-pvc 用）、`K8S_HF_CACHE_HOST_PATH` / `K8S_MARKER_INGEST_HOST_PATH`（仍是 hostPath，改成這台機器要用的路徑；不用預先 `mkdir`，`deploy.sh` 會自動建立） | 見下方說明 |
| 多節點（單節點可留空） | `REGISTRY` / `K8S_GPU_NODE_HOSTNAME` | 見第 4 節 |

## 3. 使用者資料庫（`users-db-pvc`，動態佈建）

`users-db-pvc` 是 storageClassName 動態佈建的 PVC（見 `k8s/shared-storage/pvc.yaml`），不是 hostPath，本機沒有檔案可以直接對應到裡面的內容。`deploy.sh users-db`（`deploy.sh all` 也會在 litellm 部署完後自動跑一次）邏輯是：

1. 找 litellm Pod，檢查 `/app/data/users.db` 是否已存在（存在就跳過，不會覆蓋正式資料）。
2. 不存在的話：若 `.env` 的 `K8S_DATA_HOST_PATH` 指到的目錄下有既有 `users.db`（舊機器 hostPath 時代遺留、或手動搬過來的），直接用 `kubectl cp` 灌那份既有資料；否則才退回用 `config/users.json`（模板資料）跑 `migrate_users_json.py` 產生一個全新空白的 `users.db` 再灌進去。

- **全新環境**：`K8S_DATA_HOST_PATH` 留空或指到不存在的路徑即可，直接用預設流程，之後靠 Admin API / Keycloak 同步建立使用者。
- **要延續舊機器既有使用者/部門**：先把舊機器的 `users.db` 複製到新機器 `.env` 的 `K8S_DATA_HOST_PATH` 目錄下（檔名固定 `users.db`），再跑 `deploy.sh users-db`（或整套 `deploy.sh all`）即可自動搬進 PVC；也可以自己手動 `kubectl cp <本機路徑> ai-platform/<litellm-pod>:/app/data/users.db`。

### 3.1 固定服務帳號（`config/service_accounts.json`）

`users-db` 只負責把「一整份 users.db」搬進新機器；但**服務帳號**（`account_type=service`，例如 RAG pipeline、聊天紀錄整理等固定跑的自動化角色）通常是新機器也要重新具備、而不是單純延續舊資料的東西。`deploy.sh service-accounts`（`deploy.sh all` 也會在 admin-api 部署完後自動跑一次）讀 `config/service_accounts.json` 這份 git 追蹤的清單，收斂到 admin-api 目前狀態：帳號不存在就建立（新 `api_key` 只印一次，需自行存進 Secret）、已存在只同步 models/rate limit 等設定、絕不覆蓋既有 key。詳細規則見 [admin-api.md「固定服務帳號」](admin-api.md#固定服務帳號新機器--重灌環境必須帶的帳號)。

## 4. 單節點 vs 多節點

hf-cache（模型快取）與 marker-ingest（PDF 共用目錄）這兩塊仍是 **hostPath**，本質上綁定在某一台實體節點的本地磁碟上。單節點 k3s 沒有這個問題（叢集只有一個節點，pod 不可能排到別的地方）；換成真正的多節點 k8s 叢集後，兩件事一定要處理，否則 pod 可能被排到沒有該 hostPath 目錄的節點。（`users-db-pvc` / `litellm-logs-pvc` 已經是動態佈建的 PVC，不受這節影響；admin-api 改用 `podAffinity` 釘住 litellm 所在節點來滿足 RWO 限制，見 `k8s/admin-api/deployment.yaml`。）

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

### 4.2 節點釘選：浮動池（gpu-pool label）vs hostPath 硬釘（nodeAffinity / nodeSelector）

`gemma-4-31b` / `gemma-4-26b` 已改走**浮動 GPU 池**：`nodeSelector: {gpu-pool: shared}` + `priorityClassName`（`gpu-priority-high` / `gpu-priority-medium`，見 `k8s/priorityclasses.yaml`）。部署前要先幫每個 GPU 節點貼上 label：

```bash
./scripts/label-nodes.sh            # 單節點 k3s：不帶參數，標記本機 hostname
./scripts/label-nodes.sh gpu01 gpu02  # 多節點：標記指定的節點
```

沒貼過這個 label 的節點，31b/26b 的 pod 會卡在 `Pending`。`k8s/priorityclasses.yaml` 是叢集層級資源，`./scripts/deploy.sh priorityclasses`（或 `deploy.sh all` 已內含）套用一次即可，之後新增服務只要引用同一組 `gpu-priority-*` 就能加入搶佔序列。

`light-models` **仍是 hostname 硬釘**，還沒加入浮動池：它掛的 `marker-ingest-pvc` 是 hostPath PV，`nodeAffinity` 綁死一個 hostname（見下方），就算 Deployment 改用 `gpu-pool` label 選擇器，pod 實際上還是只能落在那個節點，不會真的浮動；要等 marker-ingest 換成 RWX 網路儲存才能一起改。`.env` 的 `K8S_GPU_NODE_HOSTNAME` 因此只剩 light-models 的 `nodeSelector` 與 `marker-ingest-pv` 的 `nodeAffinity` 在用（值需與 `kubectl get nodes` 印出的 `NAME` 一致）：

```bash
kubectl get nodes   # 確認節點名稱

K8S_GPU_NODE_HOSTNAME=node-b       # 承載 K8S_MARKER_INGEST_HOST_PATH 的節點（light-models 所在節點）
```

單節點 k3s 這個變數留空即可，`deploy.sh` 會自動帶入 `$(hostname)`。

`users-db-pvc`（admin-api + litellm 共用）不再靠節點釘選，而是 admin-api 用 `podAffinity` 主動釘住 litellm Pod 所在節點（見 `k8s/admin-api/deployment.yaml`），滿足 Ceph RBD / local-path 這類 storageClassName 的 ReadWriteOnce 限制；`litellm-logs-pvc` 只有 litellm 自己掛，沒有跨 Deployment 共用問題，不需要 affinity。

> 多台 GPU 節點（例如切換 `K8S_GPU_NODE_HOSTNAME` 在 gpu01/gpu02 之間）時，第 0 節「RuntimeClass "nvidia"」那步要**在每一台 GPU 節點各自確認/安裝過**——containerd 設定是每台機器獨立的，gpu01 裝好不代表 gpu02 也好了；`RuntimeClass` 物件本身才是叢集層級只需要建一次。同理 `K8S_HF_CACHE_HOST_PATH` 這類 hostPath 快取，內容也是每台節點各自獨立，換節點等於換一份全新（或要手動搬過去）的快取。

#### 事後修改 `K8S_GPU_NODE_HOSTNAME`（叢集已在跑）

單改 `.env` 沒有用——這個變數只在 `deploy.sh` 跑 `envsubst` 展開 yaml 時才生效，要重新 apply 受影響的資源（現在只剩 light-models 這條路徑會用到；31b/26b 已改走 `gpu-pool` label，換節點只要對新節點多跑一次 `./scripts/label-nodes.sh`，不用碰這個變數）：

- `light-models` 的 `nodeSelector`：直接重跑 `deploy.sh light-models` 即可（`strategy: Recreate`，pod 會自動重排到新節點）。
- `marker-ingest-pv` 的 `nodeAffinity`：**建立後不可修改**，`kubectl apply` 會被拒絕，要先刪掉 PVC/PV 再重建（hostPath + `Retain`，磁碟資料不會被動到）。

```bash
# 1. 先把佔用 marker-ingest-pvc 的 pod 停掉，否則 PVC 會卡在等待釋放
#    （kubectl scale 只改 replicas，Deployment 本體保留；pod 走正常的優雅終止：
#     先從 Service endpoints 移除 → SIGTERM → 預設 30 秒 grace period 後才 SIGKILL）
kubectl scale deployment/light-models-vllm -n ai-platform --replicas=0

# 2. nodeAffinity 不可變，只能刪掉重建
kubectl delete pvc marker-ingest-pvc -n ai-platform
kubectl delete pv marker-ingest-pv

# 3. 重新部署（light-models 會順便用新的 K8S_GPU_NODE_HOSTNAME 重建 PV/PVC）
./scripts/deploy.sh light-models
```

換節點前記得確認新節點上已有 `K8S_HF_CACHE_HOST_PATH`（沒有的話會重新下載模型 + 重新 `torch.compile`，首次啟動約 15~25 分鐘）與 `K8S_MARKER_INGEST_HOST_PATH` 目錄；若走 `REGISTRY` 流程，`deploy.sh` 會自動處理 image 分發，不用額外操作。`litellm` / `admin-api` / `secrets` / `storage` 不受影響（`users-db-pvc` / `litellm-logs-pvc` 是動態佈建的 PVC，跟 `K8S_GPU_NODE_HOSTNAME` 無關）。

## 5. GPU 資源核對

`k8s/vllm/*/deployment.yaml` 目前假設 RTX PRO 6000（96GB）等級的卡：`gemma-4-31b`（FP8 量化 + `--tensor-parallel-size=1`）、`gemma-4-26b`、`light-models` 每個都只請求 **1 張** `nvidia.com/gpu`，沒有假設固定的 GPU 編號——31b/26b 是浮動池（見 4.2 節），排到哪一張純看當下哪張卡空，`light-models` 才是硬釘一個節點但不指定卡號。若新機器的顯存明顯較小（例如 48GB 級的卡），部署前要調整對應 deployment 的 `--gpu-memory-utilization`，否則會 OOM；若要恢復 TP>1（多卡跑一個 replica），要注意 TP 不能跨節點，且無 NVLink 環境下 TP=2 曾在評測時觸發過 GPU 故障（見 [gpu-optimization.md](gpu-optimization.md) FP8 評測記錄），沒有特殊理由不建議改回去。

## 6. 一鍵部署

```bash
./scripts/deploy.sh          # secrets → storage(PVC) → postgres → priorityclasses → 3個 vLLM → litellm → users.db 初始化 → admin-api → 固定服務帳號
./scripts/deploy.sh status   # 檢查 Pod/Service 狀態、印出 NodeIP + port
```

也可分段跑：

```bash
./scripts/deploy.sh secrets
./scripts/deploy.sh storage
./scripts/deploy.sh postgres      # LiteLLM store_model_in_db 專用，見 external-models-ops.md「路線 C」；要在 litellm 之前跑
./scripts/deploy.sh gemma-4-31b
./scripts/deploy.sh gemma-4-26b
./scripts/deploy.sh light-models
./scripts/deploy.sh litellm
./scripts/deploy.sh users-db     # litellm Pod Ready 後才能執行，見第 3 節
./scripts/deploy.sh admin-api
./scripts/deploy.sh service-accounts  # admin-api Pod Ready 後才能執行，見第 3.1 節
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
  - **新環境務必檢查 Keycloak realm 的「Require SSL」設定**（Realm Settings → General）。設成 `External requests` 時，Keycloak 只把 loopback(127.0.0.1) 判定為「非 external」，叢集內部 pod 對 pod（真實 pod IP，不是 loopback）打的請求一樣會被當 external、要求 HTTPS，導致 admin-api 呼叫 Keycloak 的 client_credentials 一律 403「HTTPS required」——不只影響一次性 bulk sync，Keycloak 那邊即時 webhook 觸發的 CREATE/UPDATE 同步（`POST /api/v1/sync/keycloak`）也會用同一支 `_get_admin_token()`，一樣會壞掉，且無感（webhook 呼叫方通常不會重試或告警）。內部服務對服務的流量，通常直接把這個設定改成 `None` 即可（使用者瀏覽器登入走的是 `KEYCLOAK_BROWSER_URL`，前面有反向代理處理 HTTPS，不受影響）。
- **OpenWebUI Connection 設定**（OpenWebUI 由另一個 repo 部署，這幾點不在 `deploy.sh` 管理範圍，但每次接新的 OpenWebUI 實例都要重做一次）：
  1. Admin Panel → Settings → Connections，Connection 的 API Key 要填 `.env` 的 `OPENWEBUI_SERVICE_KEY`（**不是** `LITELLM_MASTER_KEY`）——填 master key 會讓 `custom_auth.py` 直接放行、`models=[]`（等於不分部門全開放），整套權限/rate limit 形同虛設。
  2. 同一個 Connection 的「API Type」要選 **Chat Completions**（不是 Responses）——vLLM 沒有實作 `/v1/responses`，選錯會讓所有對話固定 404。
  3. OpenWebUI 本身要開啟轉發使用者身份的環境變數（通常是 `ENABLE_FORWARD_USER_INFO_HEADERS=true`），`custom_auth.py` 靠請求裡的 `X-OpenWebUI-User-Id` header 才能解析出使用者部門/權限，沒有這個 header 會直接 401。
  4. 改完上述設定後，OpenWebUI 前端有快取，**要硬刷新頁面或登出重新登入**才會套用新設定，只是關掉編輯視窗不會生效。
- **marker-service ingest 目錄對齊**：若這台機器同時要跑 `docblock-rag-platform`，`.env` 的 `K8S_MARKER_INGEST_HOST_PATH` 必須跟該專案的 `docblock-ingest-pv` 指向同一個實體目錄，否則 marker-service 讀不到 PDF 且是**靜默失敗**（沒有錯誤訊息，只是轉檔目錄是空的）。
- **對外防火牆**：LiteLLM NodePort `30400`、Admin API NodePort `30408`，若要對外存取記得開通。
- **Prometheus + node-exporter 監控**：`./scripts/deploy.sh monitoring`。精簡版（無 Operator/Grafana/Alertmanager），只抓 `ai-platform` namespace 內有 `prometheus.io/scrape` annotation 的 pod，60 天 retention。查詢不用 port-forward，`scripts/query-metrics.sh`（`./scripts/query-metrics.sh help` 看常用查詢）透過 `kubectl exec` 直接打 Prometheus API。細節見 [gpu-optimization.md](gpu-optimization.md)。
- **KEDA 自動擴縮**：KEDA operator 本身是一次性 cluster bootstrap，`deploy.sh` 不管理，要先用 Helm 裝：
  ```bash
  helm repo add kedacore https://kedacore.github.io/charts && helm repo update
  helm install keda kedacore/keda --namespace keda --create-namespace
  ```
  裝好後 `./scripts/deploy.sh keda` 套用 31b/26b 的 `ScaledObject`（監控 monitoring 要先部署好，KEDA 的 trigger 是查 Prometheus）。新機器預設 `minReplicaCount=maxReplicaCount=1`，只接線不會真的擴容；要放大前請先讀 [gpu-optimization.md](gpu-optimization.md) 的風險表（31b 擴容會搶佔驅逐 26b）。
- **Internal LB（Traefik p2c，多副本 least-request 分流）**：`./scripts/deploy.sh internal-lb`（**`deploy.sh all` 已經包含這步，不是選配**——`config/litellm_config.yaml` 的 `gemma-4-31b`/`gemma-4-26b` `api_base` 已經寫死指向 internal-lb，不管副本數是 1 還是多顆，這是這兩個 model 唯一的入口，沒部署 vLLM 會完全連不到）。只走 `ClusterIP`、不對外曝露。**前置需求：叢集要先有 Traefik 的 CRD**（`kubectl get crd ingressroutes.traefik.io`），k3s 內建 Traefik 當預設 ingress controller 會自動有；標準 kubeadm 叢集要手動裝：
  ```bash
  kubectl apply -f https://raw.githubusercontent.com/traefik/traefik/v3.6/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml
  ```
  沒裝 CRD 的話 `deploy.sh` 會直接 die 並印出這行指令；如果是舊版 deploy.sh 沒有這個檢查，症狀是 internal-lb pod 正常 Running、但所有請求都回 404（Traefik 完全沒有路由規則，2026-08-05 花了很長時間才追到這裡）。細節見 [gpu-optimization.md](gpu-optimization.md)。

## 相關文件

- 日常換模型、模型權限管理 SOP：[README.md](../README.md)
- Admin API 完整接口：[admin-api.md](admin-api.md)
- 模型權限同步（OpenWebUI 主導）：[permission-sync.md](permission-sync.md)
- vLLM 效能調校：[gpu-optimization.md](gpu-optimization.md)
