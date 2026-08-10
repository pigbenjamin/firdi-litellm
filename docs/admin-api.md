# Admin API 接口文件

Admin API 提供部門、使用者的資料管理、Keycloak 使用者同步，以及 OpenWebUI ↔ DB 的模型權限同步。

- **Base URL（K8s）**：`http://<node-ip>:30408`
- **互動式文件**：`http://<node-ip>:30408/docs`（FastAPI Swagger UI）

> **模型權限架構**：權威來源是 **OpenWebUI 畫面**上的模型授權，由反向同步（pull）寫回 DB，custom_auth 讀 DB 做 enforcement（加法模型：`部門授權 ∪ 個人授權`）。管理模型權限請在 OpenWebUI 操作；架構與 SOP 見 [permission-sync.md](permission-sync.md)。本文件的部門/使用者接口主要用於**資料查詢**與**例外情況**的手動調整。

---

## 接口總表

| 方法 | 路徑 | 說明 | 認證 |
|------|------|------|------|
| `GET` | `/health` | 服務健康檢查 | 無 |
| | | | |
| `GET` | `/api/v1/models` | 列出系統可用模型清單 | Admin Key |
| `GET` | `/api/v1/models/external` | 列出 DB-managed（自助上架）的模型 | Admin Key |
| `POST` | `/api/v1/models/external` | 自助上架外部模型（OpenRouter／原生 Provider） | Admin Key |
| `DELETE` | `/api/v1/models/external/{model_id}` | 刪除 DB-managed 模型 | Admin Key |
| | | | |
| `GET` | `/api/v1/departments` | 列出所有部門 | Admin Key |
| `POST` | `/api/v1/departments` | 建立部門 | Admin Key |
| `GET` | `/api/v1/departments/{dept_id}` | 取得部門詳情 | Admin Key |
| `PUT` | `/api/v1/departments/{dept_id}` | 完整替換部門資料 | Admin Key |
| `PATCH` | `/api/v1/departments/{dept_id}` | 部分更新部門欄位 | Admin Key |
| `DELETE` | `/api/v1/departments/{dept_id}` | 刪除部門（需先清空成員） | Admin Key |
| | | | |
| `GET` | `/api/v1/users` | 列出使用者（可依部門或帳號類型篩選） | Admin Key |
| `POST` | `/api/v1/users` | 建立服務帳號（human 帳號由 Keycloak 同步） | Admin Key |
| `GET` | `/api/v1/users/{user_id}` | 取得使用者詳情 | Admin Key |
| `PUT` | `/api/v1/users/{user_id}` | 完整替換使用者資料 | Admin Key |
| `PATCH` | `/api/v1/users/{user_id}` | 部分更新使用者欄位 | Admin Key |
| `DELETE` | `/api/v1/users/{user_id}` | 永久刪除使用者 | Admin Key |
| `POST` | `/api/v1/users/{user_id}/block` | 封鎖使用者 | Admin Key |
| `POST` | `/api/v1/users/{user_id}/unblock` | 解除封鎖使用者 | Admin Key |
| `POST` | `/api/v1/users/{user_id}/regenerate-key` | 重新生成使用者 API Key | Admin Key |
| | | | |
| `GET` | `/api/v1/me` | 查詢**自己**的 API Key 與可用模型（JSON，給腳本用） | Keycloak Token |
| `POST` | `/api/v1/me/regenerate-key` | 重設**自己**的 API Key（JSON，給腳本用） | Keycloak Token |
| `GET` | `/api/v1/me/web/login` | **一般使用者請走這支**：瀏覽器登入頁，導向 Keycloak | 無（登入後才驗證）|
| `GET` | `/api/v1/me/web/callback` | 登入完成後的回呼，網頁直接顯示 key | Keycloak（登入流程內部使用）|
| `POST` | `/api/v1/me/web/regenerate-key` | 網頁上的「重設我的 Key」按鈕 | Session Cookie |
| | | | |
| `POST` | `/api/v1/sync/keycloak` | 接收 Keycloak 使用者事件 | Webhook Secret |
| `POST` | `/api/v1/sync/keycloak/bulk` | 從 Keycloak 拉取全部使用者同步 | Admin Key |
| | | | |
| `POST` | `/api/v1/sync/openwebui/pull-models` | OpenWebUI 授權 → DB（主同步，CronJob 自動）| Admin Key |
| `POST` | `/api/v1/sync/openwebui/models` | DB → OpenWebUI 完整鏡像 push | Admin Key |

> 模型權限同步的架構說明與 SOP 見 [permission-sync.md](permission-sync.md)。
> **注意**：DB 側用 PATCH 改權限必須遵守 `pull → PATCH → push` 順序，否則會被下次 pull 還原。

---

## 認證

除 `/health`、`/api/v1/sync/keycloak`（Webhook Secret）與 `/api/v1/me`（Keycloak Token）外，
所有接口需在 HTTP Header 帶入 Admin API Key：

```
Authorization: Bearer <ADMIN_API_KEY>
```

### 自助端點的認證（`/api/v1/me*`）

給**一般使用者**用，不吃 `ADMIN_API_KEY`。有兩組並存、認證方式不同：

- **`/api/v1/me/web/*`**（瀏覽器登入頁，一般使用者的**主要途徑**）：不需要自己組
  HTTP 請求。使用者點 `/api/v1/me/web/login` → 導向 Keycloak 熟悉的登入畫面（跟登
  入 OpenWebUI 同一套帳號）→ 登入完自動導回、網頁直接顯示 key，並附一顆「重設我的
  Key」按鈕。伺服器端用短效 cookie（`me_session`，存的是 Keycloak access token 本
  身、HttpOnly、`SameSite=Lax`）記住這次登入，按重設鈕時不用再登入一次；`SameSite=Lax`
  同時是唯一的 CSRF 防護——瀏覽器不會在跨站 POST 帶上這顆 cookie，因此不需要另外
  做 CSRF token。
- **`/api/v1/me`（GET）/ `/api/v1/me/regenerate-key`（POST）**（JSON，給腳本/CI
  用）：帶使用者自己的 Keycloak access token：
  ```
  Authorization: Bearer <Keycloak access token>
  ```
  **注意**：這支只負責驗證你交來的 token，本身不提供「怎麼拿到 token」的手段——
  目前唯一驗證過可行的取得方式是走 `/api/v1/me/web/login` 的瀏覽器登入流程（人工
  操作一次）。如果你的腳本需要在無瀏覽器環境下自動取得 token（例如 CI），Keycloak
  client 是否開放對應的 grant（如 Direct Access Grants）尚未評估與設定，請先跟平台
  管理員確認，不要假設它已經可行。

兩者共用同一套底層邏輯（`admin-api/routers/me.py` 的 `resolve_db_user()` /
`to_me_out()` / `regenerate_key_for()`）。無論哪個入口，驗證都是把 token 送去
Keycloak 的 `userinfo` 端點（`admin-api/keycloak.py` 的 `fetch_userinfo()`），由
Keycloak 自己判斷有效性——刻意不在本地驗 JWT 簽章，這樣**已登出／已撤銷但還沒過期**
的 token 也會被擋掉。取得的 `sub` 對應 DB 的 `users.user_id`；沒命中時退回用
`email` 比對（涵蓋 Keycloak 帳號重建導致 sub 換掉的情況）。

回傳的 `MeOut` 只含使用者自己該知道的欄位（`user_id` / `key_name` / `user_email` /
`dept_id` / `api_key` / `allowed_models` / `blocked`），不含 rpm/tpm、metadata、部門
OpenRouter key 等管理面資訊，而且**只會回傳 token 對應的那一個帳號**。

`allowed_models` 是「部門 ∪ 個人」的聯集（`["*"]` 代表不限制），僅供顯示；真正的
enforcement 在 `config/custom_auth.py` 的 `_effective_models()`，兩邊邏輯要保持一致。

`/api/v1/sync/keycloak` 使用專屬的 `X-Webhook-Secret` header 認證。

---

## 通用錯誤碼

| HTTP 狀態碼 | 說明 |
|------------|------|
| `400` | 請求格式錯誤 |
| `401` | API Key 無效或未提供 |
| `404` | 資源不存在 |
| `409` | 資源衝突（重複 ID 或部門仍有使用者） |
| `422` | 請求 Body 欄位驗證失敗 |
| `500` | 伺服器設定錯誤（環境變數未設定） |
| `502` | 無法連線至 LiteLLM 或 Keycloak |

---

## 健康檢查

### `GET /health`

不需認證。回傳服務狀態，供 K8s liveness/readiness probe 使用。

**回應範例**

```json
{"status": "ok"}
```

---

## 部門（Departments）

部門是模型權限的第一層控制單位，定義該部門所有成員共用的模型白名單與流量上限。

### 資料結構

```json
{
  "dept_id": "engineering",
  "dept_name": "工程部",
  "openrouter_api_key": "sk-or-...",
  "allowed_models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it"],
  "dept_rpm_limit": 300,
  "dept_tpm_limit": 1000000,
  "created_at": "2025-01-01T00:00:00",
  "updated_at": "2025-01-01T00:00:00"
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| `dept_id` | string | 部門唯一識別碼（主鍵，不可更改） |
| `dept_name` | string | 部門顯示名稱 |
| `openrouter_api_key` | string | 此部門存取雲端 OpenRouter 的 API Key |
| `allowed_models` | string[] | 部門白名單模型清單，`["*"]` 代表全部允許 |
| `dept_rpm_limit` | int \| null | 部門每分鐘請求數上限，`null` 為不限制 |
| `dept_tpm_limit` | int \| null | 部門每分鐘 token 數上限，`null` 為不限制 |

---

### `GET /api/v1/departments`

列出所有部門。

**回應**：`DepartmentOut[]`（200）

```bash
curl http://<host>:30408/api/v1/departments \
  -H "Authorization: Bearer <key>"
```

---

### `POST /api/v1/departments`

建立新部門。

**Request Body**

```json
{
  "dept_id": "data-science",
  "dept_name": "資料科學部",
  "openrouter_api_key": "sk-or-...",
  "allowed_models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it", "embeddinggemma-300m"],
  "dept_rpm_limit": 200,
  "dept_tpm_limit": 500000
}
```

- `dept_id`、`dept_name` 必填；其餘選填（預設空字串 / 空陣列 / null）

**回應**：`DepartmentOut`（201）／ `409` 若 `dept_id` 已存在

---

### `GET /api/v1/departments/{dept_id}`

取得單一部門詳情。

**回應**：`DepartmentOut`（200）／ `404`

---

### `PUT /api/v1/departments/{dept_id}`

完整替換部門資料（所有欄位必填）。

**回應**：`DepartmentOut`（200）／ `404`

---

### `PATCH /api/v1/departments/{dept_id}`

部分更新部門。只需提供要修改的欄位，其餘保持不變。

**Request Body**（所有欄位選填）

```json
{
  "allowed_models": ["gemma-4-31B-it"],
  "dept_rpm_limit": 100
}
```

> ⚠️ **改 `allowed_models` 是例外流程**：權威來源是 OpenWebUI，直接 PATCH 的變更會被下次 pull（≤2 分鐘）還原。若真要從 DB 側改，須遵守 `pull → PATCH → push` 順序，見 [permission-sync.md](permission-sync.md)。日常改權限請在 OpenWebUI 操作。

**回應**：`DepartmentOut`（200）／ `404`

---

### `DELETE /api/v1/departments/{dept_id}`

刪除部門。若部門內仍有使用者，回傳 `409`（需先移除所有使用者才能刪除）。

**回應**：`204 No Content`

---

## 使用者（Users）

使用者的模型權限是**加法**的：個人 `models` 是「額外授權給此人」的模型，與部門 `allowed_models` **聯集**構成實際可用清單（`部門 ∪ 個人`）。個人模型**可以超出部門**，不受部門子集限制。一般使用者 `models` 留空即繼承部門。

### 資料結構

```json
{
  "api_key": "sk-abc123...",
  "key_name": "alice",
  "user_id": "keycloak-uuid-...",
  "user_email": "alice@company.com",
  "dept_id": "engineering",
  "models": ["gemma-4-31B-it", "gemma-4-26B-A4B-it"],
  "rpm_limit": 60,
  "tpm_limit": 200000,
  "aliases": {},
  "metadata": {},
  "blocked": false,
  "created_at": "2025-01-01T00:00:00",
  "updated_at": "2025-01-01T00:00:00"
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| `api_key` | string | 使用者呼叫 LiteLLM 的 Bearer token（主鍵） |
| `key_name` | string | 顯示名稱（通常為 Keycloak username） |
| `user_id` | string | 唯一使用者 ID（通常為 Keycloak UUID） |
| `user_email` | string \| null | 使用者 Email |
| `dept_id` | string | 所屬部門 ID（外鍵，部門須先存在） |
| `account_type` | `"human"` \| `"service"` | 帳號類型（預設 `"human"`） |
| `models` | string[] | 個別授權給此人的**額外**模型（與部門聯集）；空 = 只繼承部門；`["*"]` = 全部 |
| `rpm_limit` | int \| null | 個人每分鐘請求數上限 |
| `tpm_limit` | int \| null | 個人每分鐘 token 數上限 |
| `aliases` | object | LiteLLM model alias 對應（選用） |
| `metadata` | object | 自訂 metadata（選用） |
| `blocked` | bool | `true` 時拒絕所有請求 |

---

### `GET /api/v1/users`

列出使用者，可依部門或帳號類型篩選，兩個參數可同時使用。

**Query Parameters**

| 參數 | 說明 |
|------|------|
| `dept_id` | （選填）只列出指定部門的使用者 |
| `account_type` | （選填）`human` 或 `service` |

**回應**：`UserOut[]`（200）

```bash
# 列出所有使用者
curl "http://<host>:30408/api/v1/users" \
  -H "Authorization: Bearer <key>"

# 只列出 engineering 部門的人類帳號
curl "http://<host>:30408/api/v1/users?dept_id=engineering&account_type=human" \
  -H "Authorization: Bearer <key>"

# 列出所有服務帳號
curl "http://<host>:30408/api/v1/users?account_type=service" \
  -H "Authorization: Bearer <key>"
```

---

### `POST /api/v1/users`

建立服務帳號（`account_type: "service"`）。人類帳號由 Keycloak webhook 自動建立，**不應透過此接口建立**，以確保身份資料以 Keycloak 為唯一來源。

**Request Body**

```json
{
  "api_key": "sk-svc-rag-pipeline-001",
  "key_name": "rag-pipeline",
  "user_id": "svc-rag-pipeline",
  "user_email": null,
  "dept_id": "engineering",
  "account_type": "service",
  "models": ["gemma-4-26B-A4B-it", "embeddinggemma-300m"],
  "rpm_limit": 120,
  "tpm_limit": 500000,
  "aliases": {},
  "metadata": {"description": "RAG 文件摘要排程服務"},
  "blocked": false
}
```

- `api_key`、`key_name`、`user_id`、`dept_id` 必填
- `account_type` 預設 `"human"`，服務帳號請明確填 `"service"`
- `dept_id` 對應的部門必須已存在

**回應**：`UserOut`（201）／ `404`（部門不存在）／ `409`（api_key 或 user_id 重複）

---

### `GET /api/v1/users/{user_id}`

取得單一使用者詳情。

**回應**：`UserOut`（200）／ `404`

---

### `PUT /api/v1/users/{user_id}`

完整替換使用者資料（所有欄位必填，`user_id` 不可更改）。

**回應**：`UserOut`（200）／ `404`

---

### `PATCH /api/v1/users/{user_id}`

部分更新使用者。只需提供要修改的欄位。

> ⚠️ **改 `models` 是例外流程**：模型權限的權威來源是 OpenWebUI（個人授權對應 OpenWebUI 的 user grant），直接 PATCH 的 `models` 會被下次 pull（≤2 分鐘）還原。要給某人額外模型，日常請在 OpenWebUI 為該模型加 user 授權；若真要從 DB 側改，須遵守 `pull → PATCH → push`，見 [permission-sync.md](permission-sync.md)。
>
> 加法語意：`models` 是「加給此人的額外模型」，與部門聯集，**可超出部門**。`rpm_limit` / `tpm_limit` / `dept_id` 等非權限欄位不受此限制，可直接 PATCH。

**常見用法**

```bash
# 調整個人 Rate Limit（非權限欄位，可直接改）
curl -X PATCH "http://<host>:30408/api/v1/users/alice-unique-id" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"rpm_limit": 30, "tpm_limit": 100000}'

# 移轉部門（dept_id 由 Keycloak 同步為主；手動改為例外）
curl -X PATCH "http://<host>:30408/api/v1/users/alice-unique-id" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"dept_id": "data-science"}'
```

**回應**：`UserOut`（200）／ `404`

---

### `DELETE /api/v1/users/{user_id}`

永久刪除使用者資料。若只是要停用，建議改用 `block`。

**回應**：`204 No Content`

---

### `POST /api/v1/users/{user_id}/block`

封鎖使用者，後續所有 LiteLLM 請求將返回 `401`。資料保留，可隨時解封。

**回應**：`UserOut`（blocked=true）（200）

```bash
curl -X POST "http://<host>:30408/api/v1/users/alice-unique-id/block" \
  -H "Authorization: Bearer <key>"
```

---

### `POST /api/v1/users/{user_id}/unblock`

解除封鎖使用者。

**回應**：`UserOut`（blocked=false）（200）

---

### `POST /api/v1/users/{user_id}/regenerate-key`

重新生成使用者的 API Key（格式 `sk-{32位隨機hex}`）。

適用場景：API Key 外洩、定期輪換。舊 Key 立即失效（最多 30 秒快取延遲）。

**回應**：`UserOut`（含新 `api_key`）（200）

```bash
curl -X POST "http://<host>:30408/api/v1/users/alice-unique-id/regenerate-key" \
  -H "Authorization: Bearer <key>"
```

```json
{
  "api_key": "sk-a3f9c2e1b7d4...",
  "key_name": "alice",
  "user_id": "alice-unique-id",
  ...
}
```

> 務必將新 `api_key` 通知使用者，並更新其應用程式設定。
> 使用者也可以自己重設，不必透過這支，見下方「自助端點」。

---

## 自助端點（Me）

給一般使用者查詢／重設**自己**的 API Key，不需要 admin 介入。認證方式與回傳欄位的
設計理由見上方「[自助端點的認證](#自助端點的認證apiv1me)」。

使用者端的完整操作說明見 [api-access.md](api-access.md) 第 1 節（**一般使用者請看
那一節的瀏覽器登入方式即可**，不用理解下面這組 JSON 端點）。

### `GET /api/v1/me/web/login`

**主要途徑，給一般使用者。** 瀏覽器直接打開：

```
http://<admin-api 位址>/api/v1/me/web/login
```

會 302 導向 Keycloak 登入畫面（跟登入 OpenWebUI 同一套帳號），登入完自動導回
`/api/v1/me/web/callback`，網頁直接顯示 `key_name` / 部門 / email / 可用模型 /
API Key，並附一顆「重設我的 Key」按鈕（POST 到 `/api/v1/me/web/regenerate-key`，
靠登入時設好的 session cookie 驗證，不用重新登入）。

未設定 `KEYCLOAK_SELFSERVICE_CLIENT_ID` / `KEYCLOAK_SELFSERVICE_CLIENT_SECRET` /
`ADMIN_API_PUBLIC_URL` 時直接回 `500`，並在錯誤訊息裡列出缺了哪些變數；設定方式見
[keycloak/SETUP.md](../keycloak/SETUP.md)「一之二」。

### `GET /api/v1/me` （JSON，給腳本用）

**認證**：`Authorization: Bearer <Keycloak access token>`

**回應**：`MeOut`（200）

```bash
curl "http://<host>:30408/api/v1/me" -H "Authorization: Bearer $KEYCLOAK_TOKEN"
```

```json
{
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "key_name": "alice",
  "user_email": "alice@example.com",
  "dept_id": "RD",
  "api_key": "sk-a3f9c2e1b7d4...",
  "allowed_models": ["gemma-4-26B-A4B-it", "gemma-4-31B-it"],
  "blocked": false
}
```

| 狀態碼 | 情況 |
|---|---|
| `401` | token 無效、已過期或已撤銷 |
| `403` | 帳號已被封鎖（`blocked=1`） |
| `404` | Keycloak 身分在平台上還沒有對應帳號（sub 與 email 都對不到） |
| `500` | admin-api 沒設定 `KEYCLOAK_URL` / `KEYCLOAK_REALM` |
| `502` | 連不上 Keycloak |

### `POST /api/v1/me/regenerate-key` （JSON，給腳本用）

重設自己的 API Key，行為與 `POST /api/v1/users/{user_id}/regenerate-key` 相同，只是限定
在自己的帳號上。**舊 Key 立即失效**（最多 30 秒快取延遲），所有填過舊 key 的工具都要更新。

**回應**：`MeOut`（含新 `api_key`）（200）

```bash
curl -X POST "http://<host>:30408/api/v1/me/regenerate-key" \
  -H "Authorization: Bearer $KEYCLOAK_TOKEN"
```

---

## 可用模型（Models）

### `GET /api/v1/models`

向 LiteLLM 查詢目前設定的所有可用模型，供 UI 建立模型選取清單使用。

**依賴環境變數**：`LITELLM_URL`、`LITELLM_MASTER_KEY`（admin-api deployment 已設定）

**回應**（200）

```json
{
  "models": [
    "embeddinggemma-300m",
    "gemma-4-26B-A4B-it",
    "gemma-4-31B-it"
  ]
}
```

模型清單直接反映 `litellm_config.yaml` 的 `model_list`，新增或移除模型後重啟 LiteLLM 即更新。

```bash
curl "http://<host>:30408/api/v1/models" \
  -H "Authorization: Bearer <key>"
```

### `GET /api/v1/models/external`

列出目前 DB-managed（`store_model_in_db`，見 [external-models-ops.md「路線
C」](external-models-ops.md)）的模型，不含 `litellm_config.yaml` `model_list` 定義的那些。

**回應**（200）

```json
{
  "models": [
    {
      "id": "112f74fa...",
      "model_name": "openrouter/anthropic/claude-sonnet-4-5",
      "model": "openai/anthropic/claude-sonnet-4-5",
      "api_base": "https://openrouter.ai/api/v1"
    }
  ]
}
```

### `POST /api/v1/models/external`

自助上架一個外部模型，直接寫進 Postgres，不改 `litellm_config.yaml`、不重啟 litellm pod。

**請求**

```json
{
  "model_name": "gpt-4o-mini",
  "model": "openai/gpt-4o-mini",
  "api_key": "sk-xxxxxxxx",
  "api_base": null
}
```

- `model_name`：使用者呼叫時填的名字；`openrouter/` 前綴的模型會由
  `custom_logger.py` 依呼叫者的部門動態注入 `openrouter_api_key`，這種情況
  `api_key`/`api_base` 可以留空（自動帶入共用 placeholder 與 OpenRouter 端點）。
- `model`：`litellm_params.model`，供應商前綴 + 模型 id（`openai/gpt-4o-mini`、
  `anthropic/claude-...`……），OpenRouter 路線則是 `openai/<openrouter slug>`。
- `api_key`：非 `openrouter/` 前綴時必填（直接存進 Postgres，LiteLLM 會加密儲存）。

**回應**：201 成功；409 表示 `model_name` 已存在（YAML 或 DB-managed 皆算）；422
表示非 openrouter 路線卻沒給 `api_key`。

```bash
curl -X POST "http://<host>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "gpt-4o-mini", "model": "openai/gpt-4o-mini", "api_key": "sk-xxxxxxxx"}'
```

### `DELETE /api/v1/models/external/{model_id}`

刪除一個 DB-managed 模型。`model_id` 是 `GET /api/v1/models/external` 回傳的 `id`
（不是 `model_name`）。成功回 204。

```bash
curl -X DELETE "http://<host>:30408/api/v1/models/external/<id>" \
  -H "Authorization: Bearer <key>"
```

> 新增模型後別忘了到 OpenWebUI 開放使用權限，否則沒有人能實際呼叫，細節見
> [external-models.md](external-models.md) 與 [permission-sync.md](permission-sync.md)。

---

## Keycloak 同步（Sync）

### `POST /api/v1/sync/keycloak`

接收 Keycloak 使用者事件，自動同步使用者資料至 SQLite。

**不需要 Admin API Key**，改用 `X-Webhook-Secret` header 認證。

**Request Headers**

```
X-Webhook-Secret: <WEBHOOK_SECRET>
Content-Type: application/json
```

**Request Body**

```json
{
  "user_id": "<keycloak-user-uuid>",
  "event_type": "CREATE",
  "source": "admin_event"
}
```

| 欄位 | 說明 |
|------|------|
| `user_id` | Keycloak 使用者 UUID |
| `event_type` | `CREATE` / `UPDATE` / `DELETE` / `REGISTER` / `UPDATE_PROFILE` / `ACTION` |
| `source` | `admin_event`（管理員操作）或 `user_event`（使用者自身操作） |

**處理邏輯**

| 情況 | 動作 |
|------|------|
| `event_type = DELETE` | blocked=1，不查 Keycloak，不刪資料 |
| 使用者存在於 DB | 更新 key_name / user_email / dept_id / blocked |
| 使用者不存在於 DB | 建立使用者，分配 `sk-{uuid}` API Key，`models` 留空（權限由 OpenWebUI pull 填入）|
| 使用者不存在於 Keycloak | 略過，回傳 `status: skipped` |
| 使用者群組對應的部門不存在 | 自動建立空部門（allowed_models=[]），需手動設定 |

**回應範例**

```json
{"status": "created", "user_id": "uuid-...", "api_key": "sk-abc123..."}
{"status": "updated", "user_id": "uuid-..."}
{"status": "blocked", "user_id": "uuid-..."}
{"status": "skipped", "user_id": "uuid-...", "reason": "user not found in Keycloak"}
```

> **注意**：同步只更新使用者基本資料（identity），不會覆蓋管理員手動設定的個人模型白名單（`models`）及 Rate Limit。

---

## 模型權限設定流程

> **模型權限的日常管理在 OpenWebUI 畫面操作**，不透過本 API。以下場景說明各情境下該做什麼，完整 SOP 見 [permission-sync.md](permission-sync.md)。

### 場景：為部門開放某個模型（含新部門）

在 **OpenWebUI → Workspace → Models** 選該模型，把對應部門的 **group** 加入授權即可。部門對應的 OpenWebUI group 名稱 = `dept_id`；部門本身由 Keycloak 同步建立。

設定後等 CronJob（≤2 分鐘）或手動觸發同步：

```bash
curl -X POST http://<host>:30408/api/v1/sync/openwebui/pull-models \
  -H "Authorization: Bearer <key>"
```

同步後 `dept.allowed_models` 會反映 OpenWebUI 的授權，該部門所有成員即可使用。

### 場景：為特定使用者額外開放模型

加法模型下，個人授權是「**加給**此人」的（可超出部門）。在 **OpenWebUI** 該模型的授權裡，把對象加成 **user** 授權（非 group），再 pull。

> 個人授權的對象必須登入過 OpenWebUI 且有 Keycloak SSO 身分（`oidc.sub`），pull 才對映得到 DB 使用者。

### 場景：緊急封鎖某位使用者

```bash
# 封鎖
curl -X POST http://<host>:30408/api/v1/users/<user_id>/block \
  -H "Authorization: Bearer <key>"

# 同時重新生成 API Key（防止 key 被持有者繼續使用）
curl -X POST http://<host>:30408/api/v1/users/<user_id>/regenerate-key \
  -H "Authorization: Bearer <key>"
```

---

### 場景：使用者換部門（人事異動）

換部門由 **Keycloak** 主導：在 Keycloak 調整使用者群組，webhook 會同步更新 DB 的 `dept_id`。使用者的模型權限隨新部門的 group 授權自動改變（`user.models` 是加法額外授權，通常留空，不需手動處理）。

- Keycloak group 成員在使用者**下次登入 OpenWebUI** 才更新，屆時新部門的模型才會在畫面出現
- enforcement 用的 `dept_id` 由 webhook 即時同步，換部門後舊部門模型立即失效

```bash
# 確認 DB 的 dept_id 已更新（webhook 可能有數秒延遲）
curl http://<host>:30408/api/v1/users/<user_id> \
  -H "Authorization: Bearer <key>"
```

---

### 場景：部門縮減模型

在 **OpenWebUI** 該模型的授權裡移除對應部門的 group，再 pull。同步後 `dept.allowed_models` 不再含該模型，該部門成員即刻失去存取（enforcement 讀 DB）。

```bash
curl -X POST http://<host>:30408/api/v1/sync/openwebui/pull-models \
  -H "Authorization: Bearer <key>"
```

---

### 場景：新員工上線

Keycloak 建立帳號 → webhook 自動建立 DB 使用者（`models` 留空、`dept_id` 依群組）。模型權限來自其部門的 OpenWebUI group 授權，**不需針對個人操作**。

```bash
# 確認使用者已同步（Keycloak 事件可能有數秒延遲）
curl http://<host>:30408/api/v1/users/<keycloak-user-uuid> \
  -H "Authorization: Bearer <key>"
```

> 若該部門是第一次有人加入、OpenWebUI 尚無此 group：使用者登入 OpenWebUI 後 group 會自動建立，管理員再到該部門要用的模型上加 group 授權並 pull。

---

### 場景：員工離職

Keycloak 停用或刪除帳號會自動觸發 webhook，將使用者設為 `blocked=true`，無需手動操作。但建議確認狀態並同時重置 API Key，防止 Key 在離職前已被帶走使用。

```bash
# 確認封鎖狀態（Keycloak 事件可能有數秒延遲）
curl http://<host>:30408/api/v1/users/<user_id> \
  -H "Authorization: Bearer <key>"
# 確認 "blocked": true

# 重置 API Key（即使已封鎖，仍建議輪換以防 Key 外流）
curl -X POST http://<host>:30408/api/v1/users/<user_id>/regenerate-key \
  -H "Authorization: Bearer <key>"
```

---

### 場景：建立服務帳號

服務帳號供程式直接呼叫 LiteLLM 使用（CI/CD、排程腳本、後端服務等），不透過 Keycloak 建立，需手動管理生命週期。

> **這個平台固定需要的服務帳號，請走 `config/service_accounts.json` + `scripts/seed_service_accounts.py`**（見下方獨立章節），不要只用下面的 curl 建一次就結束——那樣這個帳號只活在當下這台機器的 SQLite 裡，git 完全沒有紀錄，換機器或重灌 `users-db-pvc` 會整個消失且無感。下面的 curl 流程適用於**臨時測試**或**還沒決定要不要固定下來**的一次性帳號。

**建議命名慣例**
- `user_id`：`svc-<服務名稱>`，例如 `svc-rag-pipeline`
- `key_name`：與 `user_id` 相同或加上環境後綴，例如 `svc-rag-pipeline-prod`
- `api_key`：自訂有意義的前綴或讓系統產生，例如 `sk-svc-rag-prod-<隨機碼>`

```bash
# 步驟 1：確認服務帳號要掛在哪個部門，查詢該部門允許的模型
curl http://<host>:30408/api/v1/departments/engineering \
  -H "Authorization: Bearer <key>"

# 步驟 2：建立服務帳號（models 為此帳號可用的模型；service 帳號的 models 由 admin-api 直接管理，OpenWebUI pull 不會覆寫）
curl -X POST http://<host>:30408/api/v1/users \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "api_key": "sk-svc-rag-prod-a1b2c3d4",
    "key_name": "svc-rag-pipeline-prod",
    "user_id": "svc-rag-pipeline",
    "user_email": null,
    "dept_id": "engineering",
    "account_type": "service",
    "models": ["gemma-4-26B-A4B-it", "embeddinggemma-300m"],
    "rpm_limit": 120,
    "tpm_limit": 500000,
    "metadata": {"description": "RAG 文件摘要排程服務", "owner": "backend-team"}
  }'
```

> 建立後請將 `api_key` 安全地存入服務的 Secret 管理系統（Vault、K8s Secret 等），Admin API 之後不再能查詢明文 key，只能重置。

**定期輪換 Key**

```bash
curl -X POST http://<host>:30408/api/v1/users/svc-rag-pipeline/regenerate-key \
  -H "Authorization: Bearer <key>"
# 回傳新 api_key，需同步更新服務的 Secret 設定
```

**服務下線**

```bash
# 停用（保留資料）
curl -X POST http://<host>:30408/api/v1/users/svc-rag-pipeline/block \
  -H "Authorization: Bearer <key>"

# 或完全移除
curl -X DELETE http://<host>:30408/api/v1/users/svc-rag-pipeline \
  -H "Authorization: Bearer <key>"
```

---

## 固定服務帳號（新機器 / 重灌環境必須帶的帳號）

`config/service_accounts.json` 是 git 追蹤的宣告式清單，列出這個平台**必須存在**的服務帳號（`user_id`/`dept_id`/`models`/`rpm_limit`/`tpm_limit`/`metadata`，**不含 `api_key`**）。`scripts/seed_service_accounts.py` 把目前 admin-api 的實際狀態收斂到這份清單，新機器部署或既有機器補帳號都跑同一支：

```bash
# 預覽會有什麼變更，不實際送出
ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py --dry-run

# 實際套用
ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py

# 只處理清單裡的其中幾個
ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py --only svc-chat-summarizer
```

冪等規則：
- **帳號不存在** → 建立，隨機產生新 `api_key`（`sk-<user_id>-<32位隨機hex>`），**只印一次**，需立刻存進該服務的 Secret 管理系統
- **帳號已存在** → 只 `PATCH` `key_name`/`user_email`/`dept_id`/`models`/`rpm_limit`/`tpm_limit`/`metadata` 對齊清單，**絕不覆蓋既有 `api_key`**（避免重跑就讓所有消費端的 key 一起失效），也不動 `blocked`（封鎖/解封是操作動作，不是宣告式設定）
- 引用到的 `dept_id` 若不存在會自動建立一個空部門（`allowed_models=[]`）

`./scripts/deploy.sh service-accounts` 是這支腳本的 wrapper，會先等 admin-api Pod Ready 再執行；`./scripts/deploy.sh all`（即 `deploy_all()`）跑完整部署時已經自動包含這一步，新機器一鍵部署就會把清單裡的服務帳號一併補齊。

**要新增一個「固定下來」的服務帳號**：把定義加進 `config/service_accounts.json`（比照 [POST /api/v1/users](#post-apiv1users) 的欄位，`account_type` 由腳本固定填 `service`，不用寫），跑一次腳本即可；不需要再手動 curl。

---

## 權限生效時間

Admin API 的所有寫入操作會同時遞增 SQLite `db_version` 計數器。  
LiteLLM 的 `custom_auth.py` 每次驗證請求時檢查 `db_version`，版本變化時立即重載快取。在版本未變且快取未過期（TTL=30s）的情況下使用舊快取，因此**最多 30 秒生效**。
