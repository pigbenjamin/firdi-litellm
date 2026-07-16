# 部署指南（新機器）

給「把整個平台搬到一台全新機器」時使用的完整 checklist。日常在同一台機器上重跑部署，直接用 README「快速部署」小節或 `./scripts/deploy.sh` 即可；這份文件補的是**新機器才會遇到的坑**：`.env` 與 `data/` 不在 git 裡、既有使用者資料要不要搬、GPU 規格可能不同。

## 0. 前置需求

新機器需要先具備：

- **k3s**（`deploy.sh` 用 `k3s ctr images import` 把本機 build 的 image 塞進 containerd，不是 push 到 registry）
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
| K8s hostPath | `K8S_DATA_HOST_PATH` / `K8S_LOGS_HOST_PATH` / `K8S_HF_CACHE_HOST_PATH` | 改成這台機器上要用的路徑；不用預先 `mkdir`，`deploy.sh` 會自動建立 |

## 3. 使用者資料庫（`data/` 也不在 git 裡）

`data/*` 被 gitignore 排除，git 只帶來 `.gitkeep`。`deploy.sh storage` 預設會用 `config/users.json`（模板資料）跑 `migrate_users_json.py`，產生一個**全新空白**的 `users.db`。

- **全新環境**：直接用預設流程，之後靠 Admin API / Keycloak 同步建立使用者。
- **要延續舊機器既有使用者/部門**：先從舊機器複製 `data/users.db`（實際路徑是舊機器 `.env` 裡的 `K8S_DATA_HOST_PATH/users.db`），放到新機器對應的 `K8S_DATA_HOST_PATH` 目錄下，蓋掉 `deploy.sh` 產生的空 db；或部署完 storage 後用 `kubectl cp` 覆蓋 PVC 內的檔案。

## 4. GPU 資源核對

`k8s/vllm/*/deployment.yaml` 目前假設 4× RTX PRO 6000（96GB）：GPU 0+1 給 `gemma-4-31b`（TP=2）、GPU 2 給 `gemma-4-26b`、GPU 3 給 `light-models`。若新機器 GPU 數量或顯存不同，部署前要調整對應 deployment 的 GPU 資源請求、`--tensor-parallel-size`、`--gpu-memory-utilization`，否則會排程失敗或 OOM。

## 5. 一鍵部署

```bash
./scripts/deploy.sh          # secrets → storage → 3個 vLLM → litellm → admin-api
./scripts/deploy.sh status   # 檢查 Pod/Service 狀態、印出 NodeIP + port
```

也可分段跑（image 名稱已是本機 tag `firdi-admin-api:latest` / `firdi-light-models:latest`，非 registry placeholder，`deploy.sh` 會自動 `docker build` + `k3s ctr images import`）：

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

## 6. 部署後驗證

```bash
kubectl get pods -n ai-platform -w

curl http://<node-ip>:30400/v1/chat/completions \
  -H "Authorization: Bearer <一組有效的 api_key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma-4-26B-A4B-it", "messages": [{"role": "user", "content": "你好"}]}'

curl http://<node-ip>:30408/api/v1/models -H "Authorization: Bearer <ADMIN_API_KEY>"
```

## 7. 選用元件

- **Keycloak 使用者同步插件**：`deploy.sh` 不會自動裝，需另外照 [keycloak/SETUP.md](../keycloak/SETUP.md) 手動 build + 部署到 Keycloak。
- **marker-service ingest 目錄對齊**：若這台機器同時要跑 `docblock-rag-platform`，`k8s/shared-storage/marker-ingest-pvc.yaml` 的 hostPath 必須跟該專案的 `docblock-ingest-pv` 指向同一個實體目錄，否則 marker-service 讀不到 PDF 且是**靜默失敗**（沒有錯誤訊息，只是轉檔目錄是空的）。
- **對外防火牆**：LiteLLM NodePort `30400`、Admin API NodePort `30408`，若要對外存取記得開通。

## 相關文件

- 日常換模型、模型權限管理 SOP：[README.md](../README.md)
- Admin API 完整接口：[admin-api.md](admin-api.md)
- 模型權限同步（OpenWebUI 主導）：[permission-sync.md](permission-sync.md)
- vLLM 效能調校：[gpu-optimization.md](gpu-optimization.md)
