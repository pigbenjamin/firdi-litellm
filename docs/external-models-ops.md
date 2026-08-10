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
# OpenRouter 路線：model_name 保留 openrouter/ 前綴（custom_logger.py 靠這個前綴
# 決定要不要注入部門 key），api_key/api_base 都留空即可，會自動帶入共用 placeholder
# 與 https://openrouter.ai/api/v1
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

---

## 路線 A：透過 OpenRouter

平台既有機制：`model_list` 裡的 OpenRouter 模型其實是用 `openai/` provider 打
OpenRouter 的 OpenAI 相容端點；真正的 API key **不寫死在 YAML 裡**，而是
[config/custom_logger.py](../config/custom_logger.py) 的 `async_pre_call_hook` 在每次呼叫時，
依發話者的部門動態塞入該部門的 `openrouter_api_key`。

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
- `api_key` 那行**不用改**，只是佔位符，實際 key 由 pre_call_hook 動態注入。

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

1. OpenWebUI → Workspace → Models → 選這個模型 → 設定 group（部門）／user 授權
2. 等 CronJob（最多 2 分鐘）自動 pull，或手動立即生效：

```bash
curl -X POST "http://<node-ip>:30408/api/v1/sync/openwebui/pull-models" \
  -H "Authorization: Bearer <admin-api-key>"
```

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
