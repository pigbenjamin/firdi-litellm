# 部門管理入口（admin-web）

瀏覽器版的平台管理工具，掛在 admin-api 的 `/api/v1/admin/web`。取代原本只能用
`curl` + `ADMIN_API_KEY` 做的日常操作：模型的完整生命週期（上架 → 測試 → 發布 →
停用 → 重新啟用 → 刪除）、誰能用哪個模型、部門的 provider key、權限同步、稽核查詢。
`ADMIN_API_KEY` 的 curl 路徑完全保留、行為完全沒變（使用者 CRUD、rate limit、
清除 provider key、硬刪除模型），CronJob 也還在用它——admin-web 只是把日常會做的
那些事搬到網頁上，不是取代品。

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
| 模型清單 | `GET /api/v1/admin/web/models` | 名稱／狀態／類型／上游／key 來源／本期用量與額度／測試結果／已授權部門。**含 YAML 定義的地端模型**（標「既有」） |
| 模型詳情 | `GET /api/v1/admin/web/models/detail?model_name=…` | 單一模型的全部欄位，以及測試呼叫／發布／停用／重新啟用／永久刪除 |
| 上架模型 | `GET/POST /api/v1/admin/web/models/new` | 選上游 → 選 key 來源 → 填欄位；可套用或存成「常用範本」。上架後是**草稿**（一般使用者一律 403） |
| 編輯草稿 | `GET/POST /api/v1/admin/web/models/edit?model_name=…` | 只有草稿能改上游設定（實作是刪除重建） |
| 模型授權 | `GET /api/v1/admin/web/access` | 部門×模型矩陣 + 個人授權搜尋。存檔即生效 |
| Provider Key | `GET/POST /api/v1/admin/web/keys` | 依 provider 分列，可勾選多個部門一次套用同一把 key；key 一律遮罩，**不提供清除功能** |
| 同步與診斷 | `GET/POST /api/v1/admin/web/sync` | GET 顯示 dry-run 預覽（不寫入）；POST 觸發 pull（OpenWebUI → DB），30 秒節流 |
| 稽核紀錄 | `GET /api/v1/admin/web/audit` | 依時間／操作者／動作／目標查詢，可匯出 CSV |
| 登出 | `GET /api/v1/admin/web/logout` | 同時結束 Keycloak SSO session |

模型相關的網址一律用 query string 或表單欄位傳 `model_name`，不放進路徑——
`model_name` 本身含斜線（`openrouter/anthropic/claude-sonnet-4-5`），百分比編碼的
`%2F` 會被 ASGI 伺服器解碼回真正的斜線，路徑參數就切錯段了。

## 模型的生命週期

```
                 測試通過
  上架 ──▶ 草稿 ─────────▶ 已發布
            │  ▲              │
      停用  │  │ 重新啟用     │ 停用
            ▼  │              ▼
           已停用 ◀───────────┘
```

| 狀態 | 使用者打得到嗎 | 在 LiteLLM 裡 | 可以改什麼 |
|---|---|---|---|
| **草稿** | 否——`custom_auth` 回 403 並說明原因 | 有（不然測不起來） | 全部，含上游設定 |
| **已發布** | 是（前提是有授權） | 有 | 只有描述性欄位：顯示名稱、成本歸屬、額度、備註 |
| **已停用** | 否 | 沒有（真的被刪掉） | 只能重新啟用 |

幾件實作上重要、但從畫面上看不出來的事：

- **「編輯」草稿的實作是刪除重建。** LiteLLM 沒有 `/model/update` 這支端點，只有
  `/model/new` 跟 `/model/delete`。草稿還沒有人在用，重建期間打不通沒有影響；也
  因為這樣，已發布的模型不開放改上游——那等於偷換成另一個服務。要改請先停用。
- **改完草稿的上游設定，上一次的測試結果會被清掉**，要重新測試才能發布。
- **沒通過測試呼叫的模型不能發布。** 這是客戶回饋明確要求的閘門。
- **停用不會動 `allowed_models` / `users.models`。** 授權是獨立的一件事，重新啟用
  之後模型回到 LiteLLM 清單，原本的授權自然又生效，不必重放一次。要留意的副作用
  是：停用期間 CronJob 的 pull 會把 `dept.allowed_models` 裡這個 model_name 拿掉
  （pull 以 LiteLLM `/models` 清單過濾）。OpenWebUI 那邊的 `access_grants` 不受
  影響，所以重新啟用後下一次 pull（≤2 分鐘）就會把授權補回 DB。
- **重新啟用會回到哪個狀態，看有沒有通過測試**：通過過的回「已發布」，沒有的回
  「草稿」——不然「停用一個草稿再啟用」就變成繞過發布閘門的後門。
- **停用要能一鍵復原，就得自己留住上游設定**（含共用的 `api_key`）。LiteLLM 的
  `/model/info` 會遮罩 key，撈不回來。這些值存在 `model_metadata` 表，跟
  `departments.provider_keys`、`users.api_key` 同一顆 SQLite、同樣是明文欄位；
  UI 與稽核紀錄一律只顯示末四碼。
- **永久刪除前會先算出影響範圍**（哪些部門、幾個人、哪些人是個別授權），要勾選
  確認才送得出去。`ADMIN_API_KEY` 的 curl `DELETE` 沒有這道確認。
- **草稿擋的是「呼叫」，不是「列出來」。** `allowed_models` 是 `*` 的部門，草稿模型
  仍然會出現在 LiteLLM 的 `/models` 清單裡（那條路徑上 `*` 就是不過濾），只是一打
  就 403。實務上不成問題——模型要出現在 OpenWebUI 的聊天下拉選單，還得有人去
  「模型 IDs」加一筆，那是發布之後才會做的事。
- **不受狀態機管理的模型標成「既有」**，有兩種：YAML `model_list` 定義的地端模型
  （`gemma-4-31B-it` 那些），以及這個功能上線前就用 curl 上架、還沒補過設定的外部
  模型。在模型詳情頁把描述性欄位存一次就會建起管理紀錄（行為完全不變，但從此可以
  設類型、寫備註、指定成本歸屬）。**地端模型即使納管過也永遠顯示「既有」**——它
  沒有草稿／發布／停用可言，改它要動 `config/litellm_config.yaml` 並重啟 litellm
  pod，所以畫面上不提供停用、刪除與編輯上游。

## 額度上限

每個模型可以設一個額度上限與週期（每月／累計），並**選擇是否真的擋下來**：

- **不勾「超額擋下來」**：只累計用量，畫面上看得到超額了，但呼叫照常。適合先觀察
  一個月再決定。
- **勾了**：`config/custom_auth.py` 在認證階段就回 429，訊息會寫出已用多少、上限
  多少、哪個週期。

實作上要知道的：

- **LiteLLM 內建的 budget 機制在本專案沒有資料可用。** `config/litellm_config.yaml`
  刻意關掉了 `disable_spend_logs` / `disable_spend_updates`（用量記錄走
  `custom_logger` 的 jsonl + Langfuse，那顆 Postgres 只存模型定義）。所以額度是
  自己累計的：`config/custom_logger.py` 每次成功呼叫把 `response_cost` 加進
  `model_spend` 表，`config/custom_auth.py` 讀出來比對。
- **額度用完後最多 30 秒才會開始擋。** 累計不會 bump `db_version`（每筆請求都
  bump 會讓認證端的設定快取一直失效），靠的是 30 秒的 TTL。這是刻意的取捨：額度
  是成本護欄，不是硬性配額。
- **算不出成本的呼叫不會累計。** 地端 vLLM／Ollama 模型沒有定價，`response_cost`
  是 `null`，額度對它們形同虛設。usage.jsonl 的 `response_cost` 欄位看得出來。
- **額度是整個模型的總量，不是每個部門各自的。** `cost_center`（成本歸屬部門）只是
  標記，不切分額度。

## 模型授權：存檔即生效

`GET /api/v1/admin/web/access`。**這是 [admin-web-plan.md](admin-web-plan.md) 決策 D
「模型授權一律唯讀」的翻轉**（該文件稱為第二期）。

當初唯讀的理由是「權威來源是 OpenWebUI，寫 DB 會在下次 pull（≤2 分鐘）被覆寫」。
現在的解法不是關掉 pull，而是**寫完立刻 push 回 OpenWebUI**，讓兩邊一致——之後不管
什麼時候 pull 回來，結果都一樣。等於把原本的手動 SOP（pull → PATCH → push）包成
一次「儲存」。

- **部門×模型矩陣**：一頁全表，勾選＝該部門可以用該模型。
- **個人授權**：用 email／user_id／key 名稱搜尋到人再編輯，不做幾百人的全表。個人
  授權是**加在部門授權之上**的（兩者聯集）。

### 一定要先看差異預覽

push 是**取代式的全平台鏡像**，一次錯誤的寫入可以清掉所有人的權限（這在
admin-web-plan.md 就列為已知風險）。所以這裡強制兩段式，沒有例外：

```
表單 → 預覽變更（純計算，一個字都不寫）→ 人看過按確認 → 寫 DB + push
```

預覽頁會把「收回授權」單獨標紅——那是會讓人在 OpenWebUI 聊天畫面上立刻少掉模型的
那種變更。每次確認寫入都會把 before/after 記進稽核紀錄。

### 這個畫面刻意不做的事

- **`allowed_models` 是 `*`（不限制）的部門不列在矩陣裡。** 在矩陣裡編輯它們會把
  `*` 換成一份逐筆清單、語意完全不同。真的要改請走 `ADMIN_API_KEY` 的 curl 路徑。
- **授權一個 LiteLLM 裡不存在的模型會被擋成 422。** 寫進去不會報錯，但 push 會靜默
  跳過它（push 以 LiteLLM `/models` 過濾），變成「畫面上有、實際上沒有」的鬼授權。
- **只 push 入口 A。** 入口 B 是唯讀鏡像，既有的 CronJob 每 2 分鐘會自動對齊。

## 上架模型：怎麼選

表單問「上游是誰」「key 從哪來」兩個問題，其餘路由欄位（`litellm_params.model`、
`api_base`、建議的 `model_name`）都是後端推導，不需要知道任何前綴慣例；另外再填
一組管理面欄位（顯示名稱、模型類型、成本歸屬部門、額度上限、備註）。填過一次的
組合可以存成**常用範本**下次直接套用——範本刻意**不含 key**，它會被列出來、會被
別人套用，不該夾帶祕密。

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

上架完成後這個模型是**草稿**，還沒有任何人能用。完整的後半段是：

1. 到模型詳情頁按「測試呼叫」，確認真的打得通（沒通過不能發布）
2. 按「發布」
3. 到[模型授權](#模型授權存檔即生效)把它開給要用的部門或個人（存檔即生效）
4. 到 OpenWebUI：設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」新增一筆，字串要跟
   `model_name` 逐字相同——這一步只是讓模型出現在聊天畫面的下拉選單，**授權本身已經
   在上一步做完了**

成功頁會給可直接複製的 `model_name` 跟這四步的導引連結。

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

「立即同步」是 **pull**（OpenWebUI → DB，跟 CronJob 每 2 分鐘自動跑的方向一致）。
按鈕本身受 30 秒節流（同一帳號），CronJob 本來就會自動跑，這顆按鈕只是不想等的
時候手動觸發。

**反方向的 push 現在由[模型授權](#模型授權存檔即生效)頁自動處理**，不需要在這裡
手動觸發——那個畫面每次「確認寫入」都會自己 push 一次，這正是它「存檔即生效」的
機制。

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

每個寫入操作都寫一筆 jsonl 到 `ADMIN_AUDIT_LOG_PATH`（跟 `config/custom_logger.py`
的用量記錄同樣的格式風格），含時間、操作者的 Keycloak `preferred_username`/`sub`/
`email`、目標、動作、結果，以及**變更前／變更後的值**。**不記 key 內容**，只記末四碼。

涵蓋的動作：上架、編輯草稿、修改描述欄位、測試呼叫、發布、停用、重新啟用、永久刪除、
儲存／刪除範本、設定部門模型授權、設定個人模型授權、推送授權到 OpenWebUI、從
OpenWebUI 拉回授權、設定部門 Provider Key。

`GET /api/v1/admin/web/audit` 可以依時間區間／操作者／動作／目標查詢，畫面最多列
最新 500 筆；`下載 CSV` 會把符合條件的**全部**紀錄匯出（UTF-8 with BOM、欄位標題
中文，Excel 直接開不會亂碼）。沒做 xlsx——那要多裝 `openpyxl`，而 CSV 已經滿足
「丟進 Excel 看」這個需求。

查詢是直接線性掃 jsonl，沒有另開資料表：寫入量只有管理者的手動操作，量很小，多一份
資料就多一個會跟 jsonl 對不起來的地方。紀錄檔掛在 PVC 上（跟 litellm 共用
`litellm-logs-pvc`），pod 重啟不會遺失。

```bash
tail -f /app/logs/admin-web-audit.jsonl   # 在 admin-api pod 內
```

## 已知限制

- **`allowed_models` 是 `*` 的部門、個人授權是 `*` 的使用者，畫面不提供編輯**——
  `*` 跟一份逐筆清單語意完全不同，在矩陣裡改會把它換掉。走 curl 路徑。
- **Provider key 沒有清除功能**，只能覆寫；清除走 `ADMIN_API_KEY` 的 curl 路徑。
- **只有一個中央管理帳號白名單**，不支援部門層級的委派管理。
- **已發布的模型不能改上游設定**，要改得先停用（見上方生命週期）。
- **地端模型的額度形同虛設**：LiteLLM 算不出它們的成本，`response_cost` 是 `null`，
  用量累計不到。
- **額度是模型層級的總量**，不能依部門切分。
- **地端模型與舊版 curl 上架的模型不受狀態機管理**（清單上標「既有」），不能停用、
  刪除或編輯上游；地端模型要下架得改 `config/litellm_config.yaml` 並重啟 litellm pod。
- **GCP Vertex AI / AWS Bedrock 還不是可選上游**——它們的認證形狀（service account
  JSON、AWS key + secret + region）跟目前的單一 `api_key` 字串不同，不是加一個枚舉
  項就好。
