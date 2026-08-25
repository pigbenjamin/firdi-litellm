# 部門管理入口（admin-web）

瀏覽器版的平台管理工具，掛在 admin-api 的 `/api/v1/admin/web`。取代原本只能用
`curl` + `ADMIN_API_KEY` 做的三件日常操作：上架/下架外部模型、設定部門的
provider key、觸發模型權限同步。`ADMIN_API_KEY` 的 curl 路徑完全保留、能力更廣
（使用者 CRUD、rate limit、清除 provider key），CronJob 也還在用它——admin-web
只是把日常會做的那幾件事搬到網頁上，不是取代品。

規劃背景、五個架構決策、實作階段拆解見 [admin-web-plan.md](admin-web-plan.md)。

## 登入

打開 `http://<admin-api 位址>/api/v1/admin/web`，沒登入會自動導向
`/api/v1/admin/web/login` → Keycloak 登入畫面（跟登入 OpenWebUI 同一套帳號）。

**只有白名單內的帳號能進去**，比對的是 Keycloak 的 `preferred_username`，白名單
是環境變數 `ADMIN_WEB_USERNAMES`（逗號分隔，可多筆，例如 `firdiadm` 或
`firdiadm,ops-alice`）。不在白名單內的帳號登入完會看到清楚的 403 頁面，並附一顆
連到一般使用者自助頁（`/api/v1/me/web`）的連結。

**不需要在 auth DB 裡有帳號**——管理帳號通常不是平台使用者（`firdiadm` 的
Keycloak group 是空的，`keycloak_bulk_sync` 明確跳過沒有群組的帳號），身分驗證
只看「Keycloak 登入通過 ＋ 在白名單內」這兩件事。

可管理範圍固定是**全部部門**，不做任何部門層級的委派——已定案不下放給部門，見
admin-web-plan.md「已定案的六項」。

### 開通前置設定（每個環境都要做一次）

Keycloak client `firdi-admin-api-selfservice`（跟 `/api/v1/me/web` 共用）要補兩個
redirect URI，見 [keycloak/SETUP.md](../keycloak/SETUP.md) 一之二。**這是唯一
擋在前面的手動步驟**——沒設定的話 `/api/v1/admin/web/callback` 會直接被 Keycloak
回 400（redirect_uri 未註冊），跟 `ADMIN_WEB_USERNAMES` 有沒有設完全無關（先卡在
Keycloak 那一關，根本進不到白名單檢查）。

三個環境（ai-x-dev / k8s01 / gpu01）的 `firdiadm` 是各自建立的 Keycloak 帳號，
`sub` 不同，所以白名單比對用 `preferred_username`（見 admin-api/admin_auth.py）；
但 redirect URI 是 client 層級設定、不會跨環境同步，三個環境都要各自在 Keycloak
畫面補一次。

## 畫面

| 頁面 | 路徑 | 用途 |
|---|---|---|
| 總覽 | `GET /api/v1/admin/web` | 身分、可管理部門總覽、各部門 OpenRouter key 是否已設定、待處理提示 |
| 模型清單 | `GET /api/v1/admin/web/models` | DB-managed 模型的名稱、上游、key 來源、已授權部門（唯讀；模型授權本身一律在 OpenWebUI 設定） |
| 上架模型 | `GET/POST /api/v1/admin/web/models/new` | 三步驟表單：選上游 → 選 key 來源 → 填 slug/model_name/api_base。**不可逆**，改設定只能刪除重建 |
| 下架模型 | `POST /api/v1/admin/web/models/{id}/delete` | 從模型清單頁的下架按鈕觸發，二次確認 |
| Provider Key | `GET/POST /api/v1/admin/web/keys` | 依 provider 分列，可勾選多個部門一次套用同一把 key；key 一律遮罩，**不提供清除功能** |
| 同步與診斷 | `GET/POST /api/v1/admin/web/sync` | GET 顯示 dry-run 預覽（不寫入）；POST 觸發真正同步（pull，OpenWebUI → DB），30 秒節流 |
| 登出 | `GET /api/v1/admin/web/logout` | 同時結束 Keycloak SSO session |

## 上架模型：怎麼選

表單只問兩個問題，其餘欄位（`litellm_params.model`、`api_base`、建議的
`model_name`）都是後端推導，不需要知道任何前綴慣例：

| 上游 | key 來源可選 | api_base |
|---|---|---|
| OpenRouter | 各部門自己 ／ 共用一把 | 系統帶入 `https://openrouter.ai/api/v1` |
| OpenAI／Anthropic／Gemini 官方 | 各部門自己 ／ 共用一把 | 留空，LiteLLM 內建端點 |
| 地端 vLLM | 固定共用（`EMPTY`） | 必填，Service DNS + `/v1` |
| 地端 Ollama | 固定共用（`EMPTY`） | 必填，預設 `http://ollama-service:11434`（不帶 `/v1`） |
| 其他 OpenAI 相容 | 各部門自己 ／ 共用一把 | 必填 |

「各部門自己」對應決策 E 的 `key_policy = dept:<provider>`：執行期由
`config/custom_auth.py` 依呼叫者的部門，從 `departments.provider_keys` 挑對應
provider 的 key 注入；沒設定或值是 `sk-or-CHANGE` 開頭的未換 placeholder 一律
視為未設定，該部門呼叫這個模型會 401（跟「有沒有 `allowed_models` 授權」是分開
判斷的兩件事，兩個都要對才能真的打通）。

上架後這個模型**還沒有任何人能用**——上架跟開放使用權限是分開的，一定要接著到
OpenWebUI 完成：設定 → 連線 → 「模型 IDs」新增（字串要跟 `model_name` 逐字相同）
→ Workspace → Models → 設定部門或使用者授權。成功頁會給可直接複製的
`model_name` 跟這個導引連結。

## Provider Key：一次套用給多個部門

決策 E 把部門的 provider key 從單一 `openrouter_api_key` 欄位升級成
`provider_keys`（JSON，key 為 provider 名稱，如 `{"openrouter": "...", "openai":
"..."}`）。`openrouter_api_key` 這個舊欄位仍然可用、行為完全不變，兩者由
admin-api 自動同步——這個畫面設定的其實就是 `provider_keys` 這個更大的 JSON。

- 生效時間 ≤30 秒（enforcement 端的 `db_version` 快取有 30 秒 TTL）。
- 空白的 key 欄位＝不修改，**不是清除**；這個畫面沒有清除功能。真的要清除某個
  provider 的 key，走 `ADMIN_API_KEY` 的 curl 路徑：
  ```bash
  curl -X PATCH "http://<host>:30408/api/v1/departments/<dept_id>" \
    -H "Authorization: Bearer <admin-api-key>" -H "Content-Type: application/json" \
    -d '{"provider_keys": {"openai": ""}}'
  ```
- 勾選多個部門套用同一把 key 時是逐一 PATCH，某個部門失敗不影響其他部門，畫面
  會列出每個部門各自的結果。

## 同步與診斷

「立即同步」第一期是 **pull**（OpenWebUI → DB，跟 CronJob 每 2 分鐘自動跑的方向
一致），不是 push；決策文件裡的第二期（翻轉權威來源）目前建議不排程，見
[admin-web-plan.md](admin-web-plan.md)。按鈕本身受 30 秒節流（同一帳號），CronJob
本來就會自動跑，這顆按鈕只是不想等的時候手動觸發。

診斷頁把 dry-run 結果翻成人話：模型 ID 對不上 LiteLLM（最常見的故障）、
OpenWebUI 群組對映不到部門、使用者授權對映不到 DB 帳號、使用者還沒用 SSO 登入
過 OpenWebUI。

## 環境變數

| 變數 | 說明 | 預設 |
|---|---|---|
| `ADMIN_WEB_USERNAMES` | 管理帳號白名單，逗號分隔 | 空（fail-closed，沒人能登入） |
| `ADMIN_AUDIT_LOG_PATH` | 稽核 jsonl 路徑 | `/app/logs/admin-web-audit.jsonl` |

其餘沿用 `/api/v1/me/web` 已有的 Keycloak 設定（`KEYCLOAK_SELFSERVICE_CLIENT_ID`
等，見 `.env.example`），admin-web 不需要新的 Keycloak client。

## 稽核

每個寫入操作（上架/下架模型、設定 provider key、觸發同步）都寫一筆 jsonl 到
`ADMIN_AUDIT_LOG_PATH`（跟 `config/custom_logger.py` 的用量記錄同樣的格式風格），
含時間、操作者的 Keycloak `preferred_username`/`sub`/`email`、目標、動作、結果。
**不記 key 內容**，只記末四碼。

```bash
tail -f /app/logs/admin-web-audit.jsonl   # 在 admin-api pod 內
```

## 已知限制（第一期範圍）

- 模型授權（哪些部門/使用者能用哪個模型）一律唯讀，畫面不提供設定——權威來源是
  OpenWebUI，DB 只是反向同步的結果，寫 DB 會在下次 pull（≤2 分鐘）被覆寫。
- 上架的模型不能編輯，只能刪除後重建。
- Provider key 沒有清除功能，只能覆寫；清除走 `ADMIN_API_KEY` 的 curl 路徑。
- 只有一個中央管理帳號白名單，不支援部門層級的委派管理。
