# 模型權限同步（OpenWebUI 主導架構）

## 架構總覽

**權威來源：OpenWebUI 畫面上的模型授權設定。**

```
Keycloak ──(webhook/bulk sync)──→ DB：使用者身分 + 部門歸屬
                                       ▲
OpenWebUI（管理員設定每個模型的 group / user 授權）
   │
   │  pull（CronJob 每 2 分鐘 + 可手動）
   ▼
DB：departments.allowed_models ← group 授權
    users.models               ← 個別使用者授權
   │
   ▼
custom_auth enforcement（chat 時擋 403、/models 過濾）
```

- **enforcement 語意（加法）**：使用者可用模型 = `部門授權 ∪ 個人授權`，聯集為空 = 拒絕
- **管理範圍**：只管 LiteLLM 接口暴露的模型；OpenWebUI 自己的連線模型（`local-ollama.*`）不碰
- **public 模型不算授權**：OpenWebUI 上未設任何授權的模型 = 沒人能用（會看得到但 403）

## 雙入口架構（可選：入口 A + 入口 B）

需要把不同功能拆成兩個 OpenWebUI 入口、但共用同一份權限時採用。**主從關係**：

- **入口 A = 唯一權限管理入口（權威來源）**：管理員只在 A 上設定模型授權，pull 只讀 A。
- **入口 B = 唯讀鏡像**：CronJob 每 2 分鐘 `push target=b` 把 DB 權限鏡像到 B，使 B 對齊 A。
  **在 B 上改授權沒有意義——會在 ≤2 分鐘內被自動還原。** 要改權限一律回到 A。
- **兩入口共用權限的原理**：A、B 各自有自己的 OpenWebUI user UUID，但都接同一個 Keycloak SSO，
  對映到同一個 `oauth.oidc.sub`（= DB user_id）。custom_auth 依請求帶的 service key 判斷來源入口、
  去對的實例把 UUID 解析成 sub，最後落到同一個 DB user、同一份權限。

**安全性不依賴 B 的同步**：真正的 enforcement 在 custom_auth（LiteLLM 層）。就算 B 的鏡像有延遲，
未授權請求照樣被 403 擋下；同步到 B 只是讓 B 的模型清單顯示正確。故這是 UX 一致性，不是安全邊界。

**啟用 B 的前提**（沿用單入口的已知邊界）：B 接同一個 Keycloak realm、開 `ENABLE_OAUTH_GROUP_MANAGEMENT`
（group name = dept_id）與 `ENABLE_FORWARD_USER_INFO_HEADERS`、LiteLLM connection 的 key 填 `OPENWEBUI_SERVICE_KEY_B`；
個人層級授權的對象要在 B 登入過一次（有 oidc.sub）才鏡像得到，否則列入 push 的 `missing_users`。

**未啟用 B 時**：三個 B 相關 env / secret key 留空即可，`push target=b` 回 `status=skipped`（HTTP 200），
CronJob 該步為安全 no-op，整體行為與單入口完全相同。

## 日常操作

### 改模型權限（正常流程）

1. OpenWebUI → Workspace → Models → 選模型 → 設定 group / user 授權
2. 等 CronJob（最多 2 分鐘）或手動立即生效：

```bash
curl -X POST "http://<node-ip>:30408/api/v1/sync/openwebui/pull-models" \
  -H "Authorization: Bearer <ADMIN_API_KEY>"
```

### 新模型上線

1. `config/litellm_config.yaml` 加 model entry → 更新 ConfigMap → 重啟 litellm
   （地端模型走內部 vLLM；外部模型的完整步驟見 [external-models-ops.md](external-models-ops.md)，
   給部門使用管理者的精簡版見 [external-models.md](external-models.md)）
2. **立刻**到 OpenWebUI 設定該模型的授權
   （新模型預設 public：全部人看得到但 enforcement 全擋，設完授權才可用）
3. 等 pull 或手動觸發

### DB 側改權限（例外流程，用 admin-api PATCH）

**必須成對操作，順序固定：**

```bash
# 1. pull：先把 OpenWebUI 最新狀態收進 DB
curl -X POST ".../api/v1/sync/openwebui/pull-models" -H "Authorization: Bearer <KEY>"

# 2. PATCH：改 DB
curl -X PATCH ".../api/v1/departments/PM" -H "Authorization: Bearer <KEY>" \
  -H "Content-Type: application/json" -d '{"allowed_models":["gemma-4-26B-A4B-it"]}'

# 3. push：立刻鏡像回 OpenWebUI
curl -X POST ".../api/v1/sync/openwebui/models" -H "Authorization: Bearer <KEY>"
```

> ⚠️ 只 PATCH 不 push → 改動會在下次 pull（≤2 分鐘）被 OpenWebUI 狀態**無聲還原**。

## 端點參考

| 端點 | 方向 | 用途 |
|------|------|------|
| `POST /api/v1/sync/openwebui/pull-models` | 入口 A → DB | 主同步（CronJob 自動跑；只讀權威入口 A）|
| `POST /api/v1/sync/openwebui/models?target=a` | DB → 入口 A | 完整鏡像 push（DB 側改動後用，預設 target=a）|
| `POST /api/v1/sync/openwebui/models?target=b` | DB → 入口 B | 鏡像到第二入口 B（CronJob 自動跑；B 未設定時 no-op）|

兩者皆支援 `?dry_run=true`（只回報差異，不寫入）。`target` 省略時預設 `a`。

### pull 回報欄位

| 欄位 | 意義 |
|------|------|
| `changed_departments` / `changed_users` | 本次寫入的差異（from → to）|
| `unknown_departments` | OpenWebUI 有 group 授權但 DB 無此部門（可能需跑 Keycloak bulk sync）|
| `unknown_users` | OpenWebUI 有 user 授權但 DB 無此使用者 |
| `ignored_models` | 不屬於 LiteLLM 的 OpenWebUI 模型（如 local-ollama.*），不處理 |
| `skipped_grants` | 對映不到的授權（unknown_group / no_oidc_sub）|

## 安全設計

- pull 抓取失敗（OpenWebUI / LiteLLM 連不上）→ 整批中止，DB 不動，enforcement 沿用現狀
- 有差異才寫入 + bump version（避免每 2 分鐘打斷 custom_auth 快取）
- CronJob：`k8s/admin-api/cronjob-pull-sync.yaml`（`*/2 * * * *`，Forbid 併發）

## 已知邊界

- **服務帳號（`account_type="service"`）不受同步影響**：service 帳號不存在於 OpenWebUI，其 `models` 由 admin-api 直接管理，pull / push 都只處理 `human` 帳號，不會覆寫 service 帳號。其有效模型一樣是加法：`部門 ∪ 自身 models`（若不想吃到部門模型，把它掛在沒有 group 授權的部門）。
- **個別使用者授權**：對象必須登入過 OpenWebUI 且有 Keycloak SSO 身分（oidc.sub），否則 pull 對映不到（列入 `skipped_grants`）
- **群組成員即時性**：使用者換部門後，OpenWebUI 的 group 成員在他下次登入才更新；enforcement 用的 DB dept_id 則由 Keycloak webhook 即時同步
- **race window**：PATCH→push 之間若撞上 CronJob pull，改動會遺失（重做即可，不會壞資料）
