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
| 模型清單 | `GET /api/v1/admin/web/models` | 名稱／狀態／類型／上游／key 來源／本期用量與額度／點數費率／測試結果／已授權部門。**含 YAML 定義的地端模型**（標「既有」） |
| 模型詳情 | `GET /api/v1/admin/web/models/detail?model_name=…` | 單一模型的全部欄位，以及測試呼叫／發布／停用／重新啟用／永久刪除 |
| 上架模型 | `GET/POST /api/v1/admin/web/models/new` | 選上游 → 填欄位（兩步）；可套用或存成「常用範本」。上架後是**草稿**（一般使用者一律 403） |
| 編輯草稿 | `GET/POST /api/v1/admin/web/models/edit?model_name=…` | 只有草稿能改上游設定（實作是刪除重建） |
| 模型授權 | `GET /api/v1/admin/web/access` | 部門清單（唯讀現況）＋ 可展開的唯讀矩陣 ＋ 個人授權搜尋 |
| 部門授權編輯 | `GET /api/v1/admin/web/access/dept/edit?dept_id=…` | 一次改一個部門；模型按上游分組，每列標「原本」。存檔即生效 |
| 按模型授權 | `GET /api/v1/admin/web/access/model/edit?model_name=…` | 反向：一個模型一次開給多個部門。存檔即生效 |
| Provider Key | `GET/POST /api/v1/admin/web/keys` | **舊制維護**（只服務既有的 `dept:<provider>` 模型）：依 provider 分列，可勾選多個部門一次套用同一把 key；key 一律遮罩，**不提供清除功能** |
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

## 點數費率：只存不算

每個模型可以填兩個費率：**每 1K 輸入 token 幾點**、**每 1K 輸出 token 幾點**（可填
小數）。上架表單、編輯草稿、詳情頁的「可修改的欄位」三處都有這兩格，已發布的模型
也能改——費率不影響請求打到哪裡去。

**這兩個欄位純粹是記錄。** 這個平台不累計點數、不檢查點數上限、也不會因為點數用完
而擋下任何呼叫——`config/custom_auth.py` 與 `config/custom_logger.py` 完全不看它們。
扣點與部門／人員的總點數上限由**外部系統**處理，它需要的兩份資料都已經齊備：

- **費率**：`GET /api/v1/models/external` 回應裡每個模型的 `meta.points_per_1k_prompt`
  / `meta.points_per_1k_completion`。
- **token 數**：`usage.jsonl` 每筆 `llm_call` 的 `billing_model`、`prompt_tokens`、
  `completion_tokens`，附帶 `user_id` 與 `dept_id`。

**留空是「還沒填」，不是 0。** 存進 DB 是 `NULL`，畫面顯示「未設定」。填 0 的意思是
「每 1K token 零點」，在外部系統眼裡等於這個模型免費，兩者差很多。

## 模型授權：存檔即生效

`GET /api/v1/admin/web/access`。**這是 [admin-web-plan.md](admin-web-plan.md) 決策 D
「模型授權一律唯讀」的翻轉**（該文件稱為第二期）。

當初唯讀的理由是「權威來源是 OpenWebUI，寫 DB 會在下次 pull（≤2 分鐘）被覆寫」。
現在的解法不是關掉 pull，而是**寫完立刻 push 回 OpenWebUI**，讓兩邊一致——之後不管
什麼時候 pull 回來，結果都一樣。等於把原本的手動 SOP（pull → PATCH → push）包成
一次「儲存」。

- **總覽（`/access`）唯讀**：部門清單直接列出每個部門現在有哪些模型，另附一份可
  展開的「部門 × 模型」矩陣，用來比對哪些部門有同一個模型。這一頁改不了任何東西。
- **編輯一次一個部門（`/access/dept/edit?dept_id=…`）**：模型縱向排列、按上游分組，
  每一列都標「原本：已授權／未授權」，動過的列會即時上色，並提供「本組全選／全不選」
  與「還原成原設定」。
- **按模型授權（`/access/model/edit?model_name=…`）**：同一份資料的另一個軸——一個
  模型 × 所有部門，勾部門而不是勾模型。剛上架一個模型要開給好幾個部門時走這裡：
  逐部門改要進出 N 次、push N 輪，而且差異預覽被切成 N 段，看不到「這個模型總共影響
  幾個人」。畫面上的變更摘要會即時累計人數。入口有三個：授權總覽、模型清單「已授權
  部門」欄的〔改授權〕、模型詳情頁的〔編輯這個模型的部門授權〕（發布完最自然的下一步），
  另外唯讀矩陣的欄位標題也直接連到這一頁。
- **個人授權**：用 email／user_id／key 名稱搜尋到人再編輯，不做幾百人的全表。個人
  授權是**加在部門授權之上**的（兩者聯集）。編輯頁跟部門那頁同一個形狀。

> **為什麼不是一頁大矩陣**（第二期原本的樣子）：模型數量會隨自助上架一直往右長、
> 部門只有個位數，矩陣因此又寬又稀疏；橫向捲動之後表頭與部門名都不在視線內，是誤點
> 的主因；而「沒被勾的就收回」的取代語意一旦涵蓋全表，漏看一格就會靜默收回別的部門
> 的授權——push 又是全平台鏡像，錯了就是全公司少模型。逐部門編輯把取代範圍鎖在
> `scope` 帶的那一個部門，寫壞的影響面積跟著縮到最小。全局比對的需求由總覽那份唯讀
> 矩陣負責。

### 按模型授權為什麼不把「其他模型」塞進表單

以模型為軸編輯時，每個部門的期望清單還是得包含它原有的其他模型（寫入是取代式的）。
兩種湊出這份清單的做法，這裡選第二種：

1. ✗ 頁面把各部門的其他模型全部渲染成 hidden 欄位一起送回來。那份是**開頁那一刻的
   快照**——你開著頁面時別人改了同一批部門的別的模型，一送出就被蓋掉；而且一個部門
   三十個模型就要背幾十個 hidden 欄位。
2. ✓ 表單只送 `model_name` 與勾選的部門，後端拿**當下的 DB** 算
   `desired = (該部門現有授權 − 這個模型) ∪ (勾了就加回)`。

寫入範圍也只含「這個模型的授權狀態真的有變」的部門：沒變的部門一個字都不寫，連它
裡面的失效授權都不清。有變的部門則會順帶清掉失效授權（取代式寫入的必然結果），
編輯頁事先就把哪些部門有失效授權列出來，差異預覽也會再顯示一次。

### 一定要先看差異預覽

push 是**取代式的全平台鏡像**，一次錯誤的寫入可以清掉所有人的權限（這在
admin-web-plan.md 就列為已知風險）。所以這裡強制兩段式，沒有例外：

```
表單 → 預覽變更（純計算，一個字都不寫）→ 人看過按確認 → 寫 DB + push
```

預覽頁會把「收回授權」單獨標紅——那是會讓人在 OpenWebUI 聊天畫面上立刻少掉模型的
那種變更。每次確認寫入都會把 before/after 記進稽核紀錄。

### 這個畫面刻意不做的事

- **`allowed_models` 是 `*`（不限制）的部門不提供編輯。** 總覽只把它顯示成
  「＊ 不限制」且不給編輯入口；直接開它的編輯頁是 409，把它塞進 `scope` 是 422。
  在畫面上編輯它會把 `*` 換成一份逐筆清單、語意完全不同（之後新上架的模型它就不會
  自動有了）。真的要改請走 `ADMIN_API_KEY` 的 curl 路徑。
- **授權一個 LiteLLM 裡不存在的模型會被擋成 422。** 寫進去不會報錯，但 push 會靜默
  跳過它（push 以 LiteLLM `/models` 過濾），變成「畫面上有、實際上沒有」的鬼授權。
- **只 push 入口 A。** 入口 B 是唯讀鏡像，既有的 CronJob 每 2 分鐘會自動對齊。

## 上架模型：怎麼選

表單只問一個問題：**上游是誰**。其餘路由欄位（`litellm_params.model`、`api_base`、
建議的 `model_name`）都是後端推導，不需要知道任何前綴慣例；另外再填一組管理面欄位
（顯示名稱、模型類型、成本歸屬部門、額度上限、點數費率、備註）。填過一次的組合可以
存成**常用範本**下次直接套用——範本刻意**不含 key**，它會被列出來、會被別人套用，
不該夾帶祕密。

| 上游 | key | api_base |
|---|---|---|
| OpenRouter | 必填 | 系統帶入 `https://openrouter.ai/api/v1` |
| OpenAI／Anthropic／Gemini 官方 | 必填 | 留空，LiteLLM 內建端點 |
| 地端 vLLM | 固定 `EMPTY`，不用填 | 必填，Service DNS + `/v1` |
| 地端 Ollama | 固定 `EMPTY`，不用填 | 必填，預設 `http://ollama-service:11434`（不帶 `/v1`） |
| 其他 OpenAI 相容 | 必填 | 必填 |

### 要給某個部門專屬的 key 怎麼做

**再上架一個模型。** 同一個上游、同一個 slug，`model_name` 加後綴（例如
`gpt-4o-deptA`），key 填那個部門的。它就是一個普通模型，走同一條草稿 → 測試 →
發布的路；**要開給哪些部門仍然在[模型授權](#模型授權存檔即生效)決定**——名稱裡的
`deptA` 只是給人看的命名慣例，不會綁定授權範圍，管理者要開給兩個部門也可以。

原本表單上的「第二步：key 從哪來？」已經拿掉，新模型一律是「模型自帶 key」
（`key_policy = model`）。決策 E 時期建立的 `dept:<provider>` 模型不受影響，繼續由
`config/custom_auth.py` 依呼叫者的部門從 `departments.provider_keys` 注入 key，
維護入口仍是 [Provider Key](#provider-key一次套用給多個部門) 那一頁（該頁現在只
服務這些既有模型）。這條舊路的失敗模式要記得：沒設定、或值是 `sk-or-CHANGE` 開頭的
未換 placeholder 一律視為未設定，該部門呼叫那個模型會 401（跟「有沒有
`allowed_models` 授權」是分開判斷的兩件事，兩個都要對才能真的打通）。

上架完成後這個模型是**草稿**，還沒有任何人能用。完整的後半段是：

1. 到模型詳情頁按「測試呼叫」，確認真的打得通（沒通過不能發布）
2. 按「發布」
3. 到[模型授權](#模型授權存檔即生效)把它開給要用的部門或個人（存檔即生效）
4. 到 OpenWebUI：設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」新增一筆，字串要跟
   `model_name` 逐字相同——這一步只是讓模型出現在聊天畫面的下拉選單，**授權本身已經
   在上一步做完了**

成功頁會給可直接複製的 `model_name` 跟這四步的導引連結。

## Provider Key：一次套用給多個部門（舊制維護）

**這一頁只服務決策 E 時期建立的 `dept:<provider>` 模型。** 上架動線已不再產生這種
模型（見[上架模型](#上架模型怎麼選)），新模型一律自帶 key；但既有的還在跑，換 key
時仍然要來這裡。

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
- **點數費率只是記錄**：這個平台不扣點、不檢查點數上限、也不會因為點數用完而擋下
  呼叫。扣點與部門／人員的總點數上限由外部系統處理（見上方「點數費率：只存不算」）。
- **地端模型與舊版 curl 上架的模型不受狀態機管理**（清單上標「既有」），不能停用、
  刪除或編輯上游；地端模型要下架得改 `config/litellm_config.yaml` 並重啟 litellm pod。
- **GCP Vertex AI / AWS Bedrock 還不是可選上游**——它們的認證形狀（service account
  JSON、AWS key + secret + region）跟目前的單一 `api_key` 字串不同，不是加一個枚舉
  項就好。
