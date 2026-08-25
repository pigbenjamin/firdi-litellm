# 新增外部模型（平台管理員操作手冊）

給平台管理員：如何把一個外部（非自架 vLLM）的模型實際接進 LiteLLM，讓使用者可以像呼叫
`gemma-4-31B-it` 一樣呼叫它。

**新模型優先走路線 C（自助上架，不需碰 K8s、不重啟 pod）。** 路線 A／B（改
`config/litellm_config.yaml` YAML）仍然保留，適用「就是想寫進版控固定下來」或
「Postgres/store_model_in_db 這條路徑本身出問題時」的備援手段，兩者不衝突，可以
共存（YAML 定義的模型跟 DB-managed 模型互不影響，只要 `model_name` 不撞名）。

> 給部門使用管理者的精簡版見 [external-models.md](external-models.md)——路線 C
> 上線後，新增模型多數情況下使用管理者自己就能透過 admin-api 完成，不需要再申請
> 平台管理員代勞。

> 模型權限的權威來源是 **OpenWebUI 畫面上的授權設定**，細節見
> [permission-sync.md](permission-sync.md)。本文件「開放使用權限」一節與該文件一致，
> 三條路線都要走這一步，這裡只補「模型本身怎麼接進來」的前半段。

## 先選路線

| 情境 | 走路線 |
|---|---|
| 一般情況（OpenRouter 或原生 Provider 皆適用）：新增/移除頻繁、不想碰 K8s、不想讓全平台請求短暫中斷 | **路線 C：自助上架（推薦）** |
| 想把模型定義寫進版控固定下來、或 Postgres／store_model_in_db 這條路徑本身異常時的備援 | 路線 A（OpenRouter）／路線 B（原生 Provider API） |

---

## 路線 C：自助上架（DB-managed，推薦）

### 原理

LiteLLM 內建的 `store_model_in_db`（`config/litellm_config.yaml` 的
`general_settings.store_model_in_db: true` + `database_url`）已啟用，模型定義存在
獨立的 Postgres（`k8s/postgres/`，`./scripts/deploy.sh postgres` 部署，`deploy.sh all`
已包含），可以用 LiteLLM 原生的 `POST /model/new` API 動態新增模型，完全不用改
`config/litellm_config.yaml`、不用重啟 litellm pod（YAML 是用 `subPath` 掛進容器，
改了才需要重啟；DB-managed 模型是 litellm 執行期間直接查 Postgres，即時生效）。

admin-api 包了一層薄代理（`admin-api/routers/models.py`，`/api/v1/models/external`），
用 LiteLLM master key 轉呼叫這組 LiteLLM 原生 API，這樣持有 `ADMIN_API_KEY` 的呼叫者
（跟 [external-models.md](external-models.md) 步驟 2 的部門 OpenRouter key PATCH
一樣，沿用既有角色/認證機制）不需要拿到 LiteLLM master key、也不需要任何 kubectl
存取，就能自助上架。

**一次性前置設定**（已完成，新機器/重灌環境才需要重做）：Postgres 部署見
`./scripts/deploy.sh postgres`，密碼在 `.env` 的 `POSTGRES_PASSWORD`。之後日常新增
/刪除模型完全不需要再碰這一段。

### 新增模型

```bash
# OpenRouter 路線：model_name 保留 openrouter/ 前綴只是命名慣例（決策 E 之後，
# 要不要注入部門 key 是 model_key_policies 的明確 key_policy 欄位決定，不再是前綴
# 本身；沒給 key_policy 時後端會用這個前綴推導預設值，效果跟以前一樣），
# api_key/api_base 都留空即可，會自動帶入共用 placeholder 與 https://openrouter.ai/api/v1
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "openrouter/anthropic/claude-sonnet-4-5",
        "model": "openai/anthropic/claude-sonnet-4-5"
      }'

# 原生 Provider 路線：api_key 必填（這裡直接把 key 存進 Postgres，不需要像路線 B
# 那樣先進 K8s Secret 再改 deployment.yaml 才能用）
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "gpt-4o-mini",
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-xxxxxxxx"
      }'
```

`model_name` 若跟現有模型（YAML 或 DB-managed 皆算）撞名會回 409，換個名字或先刪除
舊的。成功回 201。

### 查詢 / 刪除

```bash
# 只列出 DB-managed（store_model_in_db）的模型，不含 YAML model_list 那些
curl "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>"

# 刪除時要用上面查到的 id（不是 model_name——同一個 model_name 理論上可以有
# 多筆 deployment 做負載平衡）
curl -X DELETE "http://<node-ip>:30408/api/v1/models/external/<id>" \
  -H "Authorization: Bearer <admin-api-key>"
```

### 接下來

跟路線 A/B 一樣，模型接進來只代表「LiteLLM 認得它」，還沒有任何人能打，一定要
接著做本文件後半的「開放使用權限」。**不需要**做「部署＋重啟」那一節——那一節只
適用路線 A/B 的 YAML 改動。

### 地端 Ollama（跑在主機上、不進 YAML 的地端來源）

路線 C 不限於雲端模型——`api_base` 可以指向任何 LiteLLM 連得到的位址，所以「主機上
跑著的 Ollama」也走這條路。跟 `k8s/vllm/` 那幾個地端模型的分工是：

| | 走哪裡 |
|---|---|
| 長期正式服務、需要 KEDA 擴縮與 internal-lb least-request 分流 | `k8s/vllm/` ＋ YAML `model_list`（牽涉 Deployment 本身） |
| 臨時、應急、不需要擴縮的地端來源（例如主機上的 Ollama） | **路線 C** |

#### 1. 建立 `ollama-service`（一次性）

Ollama 跑在節點主機上，不是 K8s 資源，所以叢集內沒有名字可用。`k8s/ollama/` 用一個
**沒有 selector 的 Service ＋ 手寫 EndpointSlice** 給它一個固定 DNS 名稱：

```bash
# .env 設 OLLAMA_HOST_IP（多節點必填；單節點留空會自動取節點 InternalIP 並印 warn）
./scripts/deploy.sh ollama
```

這支指令**不會安裝也不會啟動 Ollama**，只建立指向它的 Service/Endpoints；Ollama 還沒
啟動時照樣可以先跑，之後啟動不需要重跑。它也刻意不在 `deploy.sh all` 裡（選配元件）。

前提是 Ollama 要監聽 `0.0.0.0`（`OLLAMA_HOST=0.0.0.0:11434`）——只綁 `127.0.0.1` 的話
Pod 網路過來會被拒絕。驗證 Pod 真的連得到：

```bash
kubectl run -n ai-platform ollama-probe --rm -i --restart=Never \
  --image=curlimages/curl:8.7.1 -- curl -sf http://ollama-service:11434/api/tags
```

#### 2. 逐個註冊要開放的模型

```bash
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "ollama/gemma4:31b",
        "model": "ollama/gemma4:31b",
        "api_base": "http://ollama-service:11434",
        "api_key": "EMPTY"
      }'
```

- `api_key` 是必填的（`model_name` 沒有 `openrouter/` 前綴時，`create_external_model`
  推導出的 `key_policy` 預設是 `"model"`，也就是必須自帶 `api_key`，少了會回 422；
  要改成從部門 key 注入，上架時明確帶 `key_policy: "dept:<provider>"` 即可），
  但 Ollama 不驗證，填 `EMPTY` 即可——跟 YAML 裡地端 vLLM 那幾筆一致。
- `ollama/` 是 LiteLLM 原生 provider 前綴，`api_base` **不帶 `/v1`**。要帶 `/v1` 的話
  `model` 得改成 `openai/gemma4:31b`（走 Ollama 的 OpenAI 相容端點）。
- `api_base` 填 `ollama-service` 而不是節點 IP，模型定義裡才不會硬編碼一個換機器就
  失效的位址。
- 地端 vLLM 同理，`model` 用 `hosted_vllm/<served-model-name>`、`api_base` 指向該
  Service，跟 `config/litellm_config.yaml` 現有那幾筆的寫法一致。

#### 為什麼不能靠 `ollama/*` wildcard

`config/litellm_config.yaml` 有一筆 `model_name: ollama/*`（`api_base:
http://ollama-service:11434`）。建好 Service 之後它會解析得到，但**這條路線在權限層
是不可用的**，不要拿它當正式方案：

- LiteLLM 的 `/models` 對 wildcard 條目回的是 `ollama/*` 這個字串本身，不會列舉實際
  的模型，所以 OpenWebUI 只看得到一個叫 `ollama/*` 的模型，permission sync 也只能
  授權這個字串。
- 而 `custom_auth.py` 的 `_check_model_allowed` 是精確字串比對，`_effective_models`
  只認字面上的 `"*"` 代表不限制，不會把 `"ollama/*"` 展開。使用者實際發的請求是
  `ollama/gemma4:31b`，跟 `ollama/*` 不相等 → 403。
- 結果是這筆 wildcard 只有 master key 打得到（master key 走 `custom_auth` 的
  PROXY_ADMIN 分支，跳過模型檢查），一般使用者完全用不了。

所以它的定位只有「管理員拿 master key 快速試打某個 Ollama 模型」，要給人用一定要照
上面第 2 步逐個註冊。

#### 日後改由 K8s 啟動

已規劃把 Ollama 從主機搬進 K8s。**這條管道不受影響**：`ollama-service` 這個名稱就是
遷移的接縫，已註冊模型的 `api_base` 都指向它，屆時只要給 `k8s/ollama/service.yaml`
補上 selector、刪掉 `endpointslice.yaml`，模型定義一行都不用改。

真正要處理的是 Ollama pod 本身，兩件事已知會卡住，留給做遷移的人先評估：

- **GPU 配額**：這個叢集的 device plugin 配的是整張卡、不是記憶體切片。以 ai-x-dev 為例
  `nvidia.com/gpu` allocatable 是 2，gemma-4-26b 與 light-models 各佔 1、已經配完，
  所以宣告 `nvidia.com/gpu: 1` 的 pod 會永遠 Pending——即使卡上還有大量 VRAM 閒著。
  主機版能跑正是因為它繞過 scheduler 直接用閒置 VRAM。
- **模型儲存**：主機上 `/usr/share/ollama/.ollama` 約 265GB（24 個模型）。用 PVC 要
  同等容量，用 hostPath 免搬移但會把 pod 釘死在該節點（`marker-ingest-pvc` 踩過這個坑）。

#### 運維注意：GPU 爭用

主機上的 Ollama **不受 KEDA 管、不會排隊**，它跟 vLLM 搶同幾張卡是先搶先贏。開放大
模型（例如 `gpt-oss:120b` 約 65GB、`llama4:scout` 約 67GB）之前先確認卡上還有多少
餘量，否則可能把正在服務的 vLLM 擠掉。查法：

```bash
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
```

---

## 路線 A：透過 OpenRouter

平台既有機制：`model_list` 裡的 OpenRouter 模型其實是用 `openai/` provider 打
OpenRouter 的 OpenAI 相容端點；真正的 API key **不寫死在 YAML 裡**。
[config/custom_auth.py](../config/custom_auth.py) 在每次請求驗證時，依這個模型的
`key_policy`（預設是 `dept:openrouter`）解析出該用哪個部門的哪把 key，放進
metadata；[config/custom_logger.py](../config/custom_logger.py) 的 `async_pre_call_hook`
只負責「metadata 有值就套用」，不再自己判斷前綴（決策 E，見
[admin-web-plan.md](admin-web-plan.md)）。

### 1. 加一筆 model_list

在 [config/litellm_config.yaml](../config/litellm_config.yaml) 依現有 OpenRouter 那筆樣式加：

```yaml
- model_name: openrouter/anthropic/claude-sonnet-4-5
  litellm_params:
    model: openai/anthropic/claude-sonnet-4-5
    api_base: https://openrouter.ai/api/v1
    api_key: os.environ/OPENROUTER_API_KEY_PLACEHOLDER
```

- `model_name`：使用者呼叫時填的名字，慣例上保留 `openrouter/` 前綴方便辨識來源。
- `litellm_params.model`：真正打 OpenRouter 的 model id，`openrouter.ai/models` 頁面上的
  slug 就是 `anthropic/claude-sonnet-4-5` 這種格式，前面加 `openai/` 是因為走的是 OpenAI
  相容端點，不是 LiteLLM 原生 OpenRouter provider。
- `api_key` 那行**不用改**，只是佔位符，實際 key 由 `custom_auth.py` 解析、
  `custom_logger.py` 的 pre_call_hook 動態注入。

### 2. 部門的 OpenRouter key

這個欄位跟模型「使用權限」（`allowed_models`）是分開的東西，**不受** OpenWebUI 權限同步管
（`pull`/`push` 只動 `allowed_models` 與 `users.models`，不碰 `openrouter_api_key`），所以
PATCH 完立即生效、不用額外做 push。**這步驟使用管理者可以自己做**（見
[external-models.md](external-models.md)），不需要平台管理員代勞：

```bash
curl -X PATCH "http://<node-ip>:30408/api/v1/departments/RD" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"openrouter_api_key": "sk-or-v1-xxxxxxxx"}'
```

沒設定 key 的部門呼叫這類模型時會用回 `OPENROUTER_API_KEY_PLACEHOLDER`，打 OpenRouter 一定
失敗（401），跟「有沒有 `allowed_models` 授權」是兩回事，兩個都要設對才能真的打通。

> `openrouter_api_key` 這個獨立欄位仍然可用、行為不變。決策 E 之後它實際上是
> `departments.provider_keys`（JSON，key 為 provider 名稱）裡 `"openrouter"` 這一格
> 的同義寫法，兩者由 admin-api 自動同步——要設定其他 provider（如 `openai`）的部門
> key，PATCH `provider_keys`，例如 `{"provider_keys": {"openai": "sk-..."}}`。

---

## 路線 B：直接接原生供應商 API

適用 OpenAI、Gemini、Anthropic 官方端點等——LiteLLM（`ghcr.io/berriai/litellm` image）原生
支援的 provider，不需要額外裝套件。目前 K8s 正式環境還沒有任何一個這樣的模型（docker-compose
開發環境有示範，見 [../docker-compose/config/litellm_config.yaml](../docker-compose/config/litellm_config.yaml)），
所以要多兩步把 key 從 Secret 帶進 pod。

### 1. 加一筆 model_list

`config/litellm_config.yaml`：

```yaml
- model_name: gpt-4o-mini
  litellm_params:
    model: openai/gpt-4o-mini
    api_key: os.environ/OPENAI_API_KEY
```

`litellm_params.model` 前綴依供應商換：`openai/`、`gemini/`、`anthropic/`……皆為 LiteLLM
原生 provider 前綴，不需要 `api_base`（除非是私有部署/代理端點）。

### 2. 把 key 放進 K8s Secret

編輯 [scripts/deploy.sh](../scripts/deploy.sh) 的 `deploy_secrets()`，在 `litellm-secrets`
的 `kubectl create secret generic` 那段加一行（跟著現有 `langfuse-public-key` 之類的樣式）：

```bash
--from-literal=openai-api-key="${OPENAI_API_KEY:-}" \
```

並在 `.env` 補上：

```bash
OPENAI_API_KEY=sk-xxxxxxxx
```

### 3. Deployment 加對應 env

[k8s/litellm/deployment.yaml](../k8s/litellm/deployment.yaml) 的 `env:` 區塊，照
`LANGFUSE_PUBLIC_KEY` 那組樣式加一組：

```yaml
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: litellm-secrets
      key: openai-api-key
```

---

## 部署＋重啟（只有路線 A/B 需要；路線 C 不需要這一節）

```bash
./scripts/deploy.sh secrets   # 只有路線 B、新增了 secret 欄位時才需要
./scripts/deploy.sh litellm
```

`deploy_litellm` 只是重建 ConfigMap 再 `kubectl apply`，**不會自動重啟 pod**——
`litellm_config.yaml` 是用 `subPath` 掛進容器，K8s 對 subPath 掛載的檔案不會熱更新（跟
admin-api 的部署函式不一樣，那邊有內建 `rollout restart`，這邊沒有）。所以每次改完設定都要
手動：

```bash
kubectl rollout restart deployment/litellm -n ai-platform
kubectl rollout status deployment/litellm -n ai-platform
```

> **這步驟會讓全平台所有使用者的請求短暫中斷**（pod 重啟），不是只影響新模型本身。這是
> 路線 A/B（改 YAML）本質上的限制，路線 C（見本文件前半「自助上架」）完全不會有這個問題——
> 新增模型優先用路線 C，路線 A/B 保留給「想寫進版控固定下來」或 Postgres 異常時的備援。

---

## 開放使用權限（三條路線都要做，否則沒人打得到）

`model_list` 裡有這個模型只代表「LiteLLM 認得它」。使用者能不能實際呼叫，是
[custom_auth.py](../config/custom_auth.py) 依**加法白名單**判斷：
`可用模型 = 部門授權(allowed_models) ∪ 個人授權(users.models)`，聯集為空 = 403。
新模型預設沒有任何授權（等同 public 但沒人能用），一定要手動開通。

**權威來源是 OpenWebUI**，正常流程（使用管理者可自行操作，見
[external-models.md](external-models.md)）：

1. OpenWebUI → 設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」加一筆，字串必須等於
   `model_name`（見下方「模型 ID 必須逐字相同」）
2. OpenWebUI → Workspace → Models → 選這個模型 → 設定 group（部門）／user 授權
3. 等 CronJob（最多 2 分鐘）自動 pull，或手動立即生效：

```bash
curl -X POST "http://<node-ip>:30408/api/v1/sync/openwebui/pull-models" \
  -H "Authorization: Bearer <admin-api-key>"
```

### 模型 ID 必須逐字相同（三條路線都適用的常見故障）

`pull_openwebui_model_access`（[openwebui.py](../admin-api/routers/openwebui.py)）的第一件事
是拿 LiteLLM `/models` 的 id 集合當白名單，OpenWebUI 上 `id` 不在集合裡的模型直接歸入回應的
`ignored_models` 跳過——這是為了不去碰 OpenWebUI 自己的連線模型（`local-ollama.*`）與 pipe。
副作用是：OpenWebUI 那邊模型 ID 打錯時，同步**不會報錯**（HTTP 200、`changed_departments` 空），
只是那筆授權永遠不會進 DB，使用者照樣 403。實務上最常見的填錯是把路線 C 的 `model` 供應商
slug（`openai/gpt-5.6-terra`）當成模型 ID，而正確的是 `model_name`
（`openrouter/openai/gpt-5.6-terra`）。

排查：`?dry_run=true` 跑一次 pull，看目標模型是落在 `changed_departments` 還是 `ignored_models`。

`dept_id` 的粒度也常被問到：`dept_id` = Keycloak group path 第一層
（`keycloak/SETUP.md`、`sync.py` 的 `parts[0]`），**沒有階層繼承**——授權 `DE0000` 不會及於
`DE1200`。目前 realm 的 `DExxxx` 都是各自獨立的第一層 group，所以四位數部門代碼就是
group 授權能到的最細層級；再細只能走個人授權（`users.models`）。

### 例外流程：直接 PATCH DB（例如 OpenWebUI 那邊還沒排到、想先開放測試）

**必須成對操作、順序固定，只 PATCH 不 push 的話，2 分鐘內會被下一次 pull 無聲還原**：

```bash
# 1. pull：先把 OpenWebUI 最新狀態收進 DB，避免蓋掉別人剛改的授權
curl -X POST "http://<node-ip>:30408/api/v1/sync/openwebui/pull-models" \
  -H "Authorization: Bearer <admin-api-key>"

# 2. PATCH：改 DB（也可用 scripts/grant_model.py，見下）
curl -X PATCH "http://<node-ip>:30408/api/v1/departments/RD" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"allowed_models": ["gemma-4-31B-it", "openrouter/anthropic/claude-sonnet-4-5"]}'

# 3. push：立刻鏡像回 OpenWebUI，否則下次 pull 會把步驟 2 的改動蓋掉
curl -X POST "http://<node-ip>:30408/api/v1/sync/openwebui/models" \
  -H "Authorization: Bearer <admin-api-key>"
```

也可以用現成腳本快速對多個部門開通，**但它只做 PATCH、不會自動 push**，跑完務必自己補上面
的步驟 3：

```bash
ADMIN_API_KEY=xxx ./scripts/grant_model.py openrouter/anthropic/claude-sonnet-4-5 --all
ADMIN_API_KEY=xxx ./scripts/grant_model.py gpt-4o-mini --dept RD PM
```

---

## 驗證

```bash
# 1. 模型有沒有被 LiteLLM 認到
curl http://<node-ip>:30408/api/v1/models -H "Authorization: Bearer <admin-api-key>"

# 2. 已授權的使用者打得通
curl http://<平台位址>/v1/chat/completions \
  -H "Authorization: Bearer sk-使用者的key" \
  -H "Content-Type: application/json" \
  -d '{"model": "openrouter/anthropic/claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]}'

# 3. 沒授權的部門/使用者應該收到 403（而不是連不到 model 或別的錯誤）
```

## 收尾建議

- 更新 [api-access.md](api-access.md)「可用模型」表格，讓直接用 API 的使用者知道多了什麼可以打。
- 外部 API 是真的要花錢的，平台目前**幾乎沒設速率限制**（見 api-access.md 已知限制）。
  建議順手幫用得到的部門設 `dept_rpm_limit` / `dept_tpm_limit`（同一個
  `PATCH /api/v1/departments/{dept_id}`），避免失控的 agent 迴圈把費用打爆。
