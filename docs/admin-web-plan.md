# 部門管理入口（admin-web）實作交接

把現在只能用 curl 做的平台管理動作（上架外部模型、設定部門 provider key、觸發權限同步），
變成中央管理帳號在網頁上就能完成的流程。掛在 admin-api 的 `/api/v1/admin/web`。

**狀態：第一期（階段 01～06）、第二期（WP1～WP5）與第三期（點數費率欄位 + 上架
動線簡化，見下方各節）程式碼與文件皆已完成。** 使用說明、畫面、
環境變數見 [admin-web.md](admin-web.md)；本文件保留架構決策的脈絡與「為什麼這樣
做」，供之後改動時參考。唯一還沒做的是**三個環境各自的 Keycloak redirect URI
註冊**（見下方「開工前的手動設定」）與實際部署——這兩步驟需要 Keycloak 管理
主控台存取與逐環境確認，留給有權限的人執行。

## 三份完整文件

| 文件 | 網址 | 內容 |
|---|---|---|
| 規劃 | https://claude.ai/code/artifact/1befd76b-f9e5-4ebb-901f-37546bf13a3c | 五個架構決策、階段拆解、風險盤點 |
| 規格書 | https://claude.ai/code/artifact/c6a27b16-7b3a-4321-bdd6-5b1c2616bce4 | 8 個畫面、5 個寫入操作、43 條規則（R-01～R-43） |
| 驗證清單 | https://claude.ai/code/artifact/83a88fd0-ff78-437d-99d2-d18522a7ddba | 56 個驗證情境（V-01～V-56），含做法與判斷依據 |

（這三份是 Claude Code 的 Artifact。終端機打 `/artifacts` 可列出並開啟。）

## 已定案的六項

1. **模型上架／下架只有中央使用者能做**——不下放給部門。
2. **下架不需要 owner 記錄**——因為只有一個人能刪，省掉比對邏輯。
3. **不設 Keycloak 管理子群組**，只認 `firdiadm` 一個帳號（環境變數白名單，可多筆）。
4. **provider 枚舉七項**：OpenRouter、OpenAI、Anthropic、Gemini、地端 vLLM、地端 Ollama、其他 OpenAI 相容。
5. **禁止從 UI 清除 provider key**——空欄位＝不修改。要清除走 `ADMIN_API_KEY` 的 curl 路徑。
6. **保留「立即同步」按鈕**，含節流。

## 五個架構決策

**A. 登入沿用 `me_web` 的 Authorization Code flow。** 現成可複用
[keycloak.py](../admin-api/keycloak.py) 的 `build_authorize_url` / `exchange_code_for_token` /
`fetch_userinfo` / `build_logout_url`，以及 cookie session 的設計（cookie 存 access token、
每次重新打 userinfo 驗證、`SameSite=Lax` 免 CSRF token）。
要動的是 `CALLBACK_PATH` 這個模組常數與 `selfservice_redirect_uri()`——兩者寫死了 me_web 的路徑，
新頁面需要自己的 callback，得參數化。

**B. 只有一個中央管理帳號。** 白名單比對 `preferred_username`（**不要比對 `sub`**——三個環境的
firdiadm 是各自建立的、sub 不同，用 sub 會讓設定不能跨環境；現有 `profile` scope 已含
`preferred_username`）。可管理範圍＝全部部門。

**C. 既有 router 的認證重構。** [models.py](../admin-api/routers/models.py) 與
[departments.py](../admin-api/routers/departments.py) 是 router 級綁 `verify_admin_key`，
網頁流程認的是 Keycloak session。**不要讓網頁層拿 `ADMIN_API_KEY` 用 HTTP 打自己的 API**——
那會繞過自己的授權檢查、把共享祕密帶進第二條路徑。正確做法是把業務邏輯抽成不帶認證的
service 函式，兩個 router 各自認證後呼叫同一個函式。

**D. 第一期範圍。** 可寫：上架／下架模型、部門 provider key（可勾選多部門一次套用）、立即同步。
唯讀：模型清單、各部門 `allowed_models`、dry-run 診斷。
**模型授權一律唯讀**——權威來源是 OpenWebUI，寫 DB 會在 2 分鐘內被 pull 覆寫。

> **這一條在第二期已經翻轉**（見下方「WP4 授權寫入」）。翻轉的方式不是關掉 pull，
> 而是寫完立刻 push 回 OpenWebUI，讓兩邊一致——之後不管什麼時候 pull 回來結果都一樣。

**E. 把「上游是誰」和「key 從哪來」解耦。** 現在這兩件事被 `openrouter/` 前綴綁在一起
（[custom_logger.py:36-39](../config/custom_logger.py#L36-L39) 只看 `data["model"]` 是否以該前綴開頭）。
要改成：

- 部門端 `departments.openrouter_api_key` 升級成 `provider_keys` JSON。
- 模型端記一筆 key 來源政策（`model` = 用模型定義自己的 key；`dept:openai` = 用部門的該 provider key）。
- **判斷從 `custom_logger` 上移到 `custom_auth`**——後者本來就在讀 SQLite（有 `db_version` +
  30 秒 TTL 快取）、本來就知道請求的模型；解析完把結果放進 metadata，`custom_logger` 退化成
  「metadata 有就套用」，前綴檢查整段刪掉。
- **做成純超集、零資料遷移**：`provider_keys` 用 `init_db` 既有的 ALTER TABLE 樣式補欄位並從舊欄位回填；
  沒有明確政策的 `openrouter/` 開頭模型預設視為 `dept:openrouter`。
- 政策存 SQLite 而非 LiteLLM 的 `model_info`：後者語意較乾淨，但會讓 `custom_auth`
  在每個請求的熱路徑上多一個 Postgres 相依，不值得。

> **決策 E 落地後，`openrouter/` 前綴退化成純命名慣例、不再是功能開關。**
> [external-models.md](external-models.md) 與 [external-models-ops.md](external-models-ops.md)
> 裡「前綴是功能開關」的敘述屆時全部要改。

## 階段拆解（第一期）

| 階段 | 內容 | 驗收 |
|---|---|---|
| 01 | service 層抽離（純重構，不加新功能） | 現有 curl 行為與回應完全不變，含 409／422 錯誤路徑 |
| 02 | 管理帳號與身分驗證 | `/whoami` 唯讀頁；管理帳號**即使不在 auth DB 裡**也能登入 |
| 03 | 唯讀管理頁 | 非白名單帳號得到清楚的 403 |
| 04 | key 來源解耦（決策 E 後端） | 現存 `openrouter/*` 行為不變；兩種新情境都能呼叫 |
| 05 | 寫入操作（表單） | 走完一次完整流程且過程中沒用到 `ADMIN_API_KEY` |
| 06 | 文件與三環境部署 | 三個環境的 login 都能進且行為一致 |

## 開工前的手動設定（唯一阻礙）

Keycloak client `firdi-admin-api-selfservice` 要補兩個欄位。**已實測確認是精確比對**：
現有的 `/api/v1/me/web/callback` 回 HTTP 200 登入表單，
`/api/v1/admin/web/callback` 回 **HTTP 400 redirect_uri 未註冊**。

1. Valid redirect URIs 加 `<ADMIN_API_PUBLIC_URL>/api/v1/admin/web/callback`
2. Valid post logout redirect URIs 加 `<ADMIN_API_PUBLIC_URL>/api/v1/admin/web/login`

post-logout 是**獨立欄位**，不會沿用 redirect URI。三個環境各設一次
（ai-x-dev 的 `ADMIN_API_PUBLIC_URL` 是 `http://10.90.20.55:30408`）。
Web origins 不用改。`user-sync-service` 這個 service account 沒有 `view-clients` 權限，
所以只能在 Keycloak 畫面上手動加。

## 實作時最容易做錯的五件事

1. **不可依賴 auth DB 的 `users` 表做身分驗證。** `firdiadm` 的 Keycloak group 是空的，
   而 `keycloak_bulk_sync` 明確跳過沒有群組的帳號——實測 DB 裡 0 筆。沿用 `me_web` 的
   `resolve_db_user` 會讓管理帳號直接撞上「查無帳號」而登不進來。
   admin-web 只需要「Keycloak 驗證通過 ＋ 在白名單內」。
2. **空的 key 欄位＝不修改，不是清除。** 把空字串當清除會把部門的 key 清掉。
3. **cookie 名稱要跟 `me_web` 的分開**（`me_session` / `me_oauth_state` / `me_id_token`），
   否則兩邊的登入狀態會互相干擾。另外 `me_web` 現在**沒有帶 `secure`**，
   而 cookie 裡存的是 access token 本身——admin-web 要補上。
4. **`model` 與 `model_name` 要檢查非空。** 現在只要求是字串，空字串會通過 admin-api
   驗證再到 LiteLLM 失敗，變成難懂的 502。
5. **擋掉以 `sk-or-CHANGE` 開頭的 key。** `custom_logger` 對這種值會靜默跳過注入、
   退回 placeholder 拿到 401，且不給任何理由。

## 已知的安全待修（既有問題，非本次新增）

- `me_web` 的 session cookie 缺 `secure`，而它存的是 Keycloak access token；admin-api 走 NodePort 純 HTTP。
- `DepartmentOut.openrouter_api_key` 是明文欄位、GET 原樣回傳。UI 必須遮罩（只顯示末四碼）
  且**不可放進 HTML 的 `value` 屬性**。
- admin-api 目前完全沒有操作稽核。UI 化之後建議補 jsonl（寫法照
  [custom_logger.py](../config/custom_logger.py)），且不記完整 key。

## 第二期：客戶回饋 v0.5 的五個工作包（已完成）

第一期上線後客戶實測 `curl` + `ADMIN_API_KEY` 流程並提出正式回饋（v0.5），三個核心
痛點：(1) curl 流程複雜易錯，使用者要自己拼 JSON、猜 provider slug、決定哪個欄位放
哪裡；(2) 失敗沒有回饋（例如漏設部門 key 就是靜默失效，一個提示都沒有）；(3) 文件
落後於程式碼。訴求歸納起來是：把整個生命週期搬到瀏覽器上。

實作成果與使用說明見 [admin-web.md](admin-web.md)，這裡只留架構決策的脈絡。

### WP1 模型的必要欄位

顯示名稱、模型類型（chat/embedding/rerank）、成本歸屬部門、額度上限、備註，加上
「常用範本」。**存 admin-api 自己的 SQLite（`model_metadata` 表），不推進 LiteLLM 的
`model_info`**——跟決策 E 的 `model_key_policies` 同一個理由：`custom_auth` 在每個
請求的熱路徑上讀的就是這顆 SQLite，狀態閘門與額度都要在那裡判斷，塞進 `model_info`
等於在熱路徑多一個 Postgres 相依。

**主鍵用 `model_name` 而不是 LiteLLM 的 deployment id**：id 在「草稿改設定＝刪除
重建」與「停用→啟用」之後都會換一個，只有 `model_name` 從頭到尾不變，而且它正是
授權（`allowed_models` / `users.models`）與 OpenWebUI `access_grants` 認的那個字串。

### WP1 補充：額度要「真的擋得下來」

原本規劃是純記錄，實作前確認要能真的限制、且可選擇是否啟用。這牽出一件事：
**LiteLLM 內建的 budget 機制在本專案沒有資料可用**——`config/litellm_config.yaml` 刻意
關掉了 `disable_spend_logs` / `disable_spend_updates`（用量走 `custom_logger` 的 jsonl
+ Langfuse，那顆 Postgres 只存模型定義）。

所以額度是自己累計的：新增 `model_spend` 表，`config/custom_logger.py` 每次成功呼叫
把 `response_cost` 加進去，`config/custom_auth.py` 在認證階段比對。三個刻意的取捨：

- **累計不 bump `db_version`**：每筆請求都 bump 會讓 `custom_auth` 的設定快取一直
  失效，整個快取就白做了。代價是額度用完後最多 30 秒（`_CACHE_TTL`）才開始擋。額度
  是成本護欄不是硬性配額，這個誤差可以接受。
- **花費記在「呼叫者請求的公開 `model_name`」上**，不是 `kwargs["model"]`（那可能已經
  是上游的 `litellm_params.model`，如 `openai/gpt-4o-mini`）。傳遞方式沿用這個專案
  已經驗證過的那條路：`custom_auth` 放進 `metadata`，`custom_logger` 的
  `async_pre_call_hook` 複製到 `data["metadata"]`——`dept_id` 就是這樣傳的。
- **算不出成本就記 `null` 而不是 0**：0 會讓「算不出成本」看起來像「這次免費」，
  額度永遠用不完卻沒人發現。`usage.jsonl` 多了 `response_cost` 欄位可以查。

### WP2 生命週期狀態機

`draft` →（測試通過）→ `published` →（停用）→ `disabled` →（重新啟用）。三個實作上
非做不可的決定：

- **LiteLLM 沒有 `/model/update`**，只有 `/model/new` 跟 `/model/delete`。所以「編輯」
  一律是刪除重建，也因此只開放給 `draft`——草稿還沒有人在用。
- **停用要能一鍵復原，就得自己留住 `api_key`**：`/model/info` 會遮罩它，撈不回來。
  存在 `model_metadata`，跟 `departments.provider_keys`、`users.api_key` 同一顆 DB、
  同樣明文，UI 與稽核一律只顯示末四碼。
- **停用刻意不動 `allowed_models` / `users.models`**。副作用是停用期間 pull 會把
  DB 裡的授權拿掉（pull 以 LiteLLM `/models` 過濾），但 OpenWebUI 的 `access_grants`
  不受影響（push 同樣跳過不在 LiteLLM 清單的模型），所以重新啟用後下一次 pull 會
  自動補回來。這個自癒路徑要記得，不然會誤以為停用弄丟了授權。
- **重新啟用回到哪個狀態看 `last_test_ok`**：沒測過的回 `draft`，不然「停用一個草稿
  再啟用」就成了繞過發布閘門的後門。

`model_name` 一律走 query string 或表單欄位，**不當路徑參數**——名稱含斜線，
百分比編碼的 `%2F` 會被 ASGI 伺服器解碼回真正的斜線，路徑就切錯段了。

### WP3 發布前的測試呼叫

依 `model_type` 送不同形狀的最小請求（chat / embeddings / rerank）——用 chat 的形狀
去測 embedding 模型只會拿到看不懂的 400，那正是客戶抱怨的那種錯誤訊息。失敗依狀態碼
分類成人話（401/403 金鑰、429 額度、400/404 找不到模型、逾時）。結果存回
`model_metadata`，並**當作 `draft` → `published` 的前置條件**。

測試走 LiteLLM master key，那條路在 `custom_auth` 是 `PROXY_ADMIN` 的早退路徑，
不經過狀態閘門，所以草稿測得起來。

### WP4 授權寫入（翻轉決策 D）

**決策 D 的「模型授權一律唯讀」在這一期翻轉。** 當初唯讀的理由是「權威來源是
OpenWebUI，寫 DB 會在下次 pull 被覆寫」；解法不是關掉 pull，而是**寫完立刻 push 回
OpenWebUI**，讓兩邊一致——之後不管什麼時候 pull 回來結果都一樣。等於把文件裡原本的
手動 SOP（pull → PATCH → push）包成一次「儲存」。

好消息是難的部分早就有了：`services/openwebui_sync_service.py` 的
`push_model_access_to_openwebui(target="a")` 是現成的全鏡像 push，CronJob 已經在用。
新增的只有 UI 與自動串接。

**風險控制**：push 是取代式的全平台鏡像，一次錯誤寫入可以清掉所有人的權限。所以
強制兩段式，沒有例外——表單 → 預覽差異（純計算，一個字都不寫）→ 確認 → 寫 DB +
push，且每次寫入都把 before/after 記進稽核。另外三個刻意的限制：`*`（不限制）的
部門不列進矩陣（編輯會把 `*` 換成逐筆清單、語意不同）、授權 LiteLLM 不存在的模型
擋成 422（不然會變成「畫面上有、實際上沒有」的鬼授權）、只 push 入口 A（B 是唯讀
鏡像，CronJob 會對齊）。

### WP5 稽核擴充

`write_audit` 的 `detail` 標準化成一定帶 `before`/`after`；新增生命週期與授權相關的
動作代碼與中文對照；加上查詢頁與 CSV 匯出（UTF-8 with BOM，欄位標題中文）。查詢直接
線性掃 jsonl，沒有另開資料表——寫入量只有管理者的手動操作，多一份資料就多一個會跟
jsonl 對不起來的地方。沒做 xlsx（要多裝 `openpyxl`，CSV 已滿足需求）。

### 這一輪刻意不做

- **GCP Vertex AI / AWS Bedrock 當上游**：認證形狀（service account JSON 上傳、
  AWS access key + secret + region）跟目前的單一 `api_key` 字串不同，不是在
  `model_upstreams.py` 加一個枚舉項就好。
- **prompt caching 的成本歸帳**（客戶回饋第十節）：cache read/write token 的
  passthrough、per-model cache 定價、用量記錄的三分法、cache-hit 率報表。整個 repo
  目前零相關程式碼。**這一節在客戶文件裡本身就標注為「我方觀察，待客戶確認」**，
  建議先跟客戶確認要不要做，再排程。

### 回溯相容

- `POST /api/v1/models/external`（curl 路徑）的 `status` 預設是 `published`，既有
  流程行為完全不變；網頁表單才明確帶 `draft`。
- 沒有 `model_metadata` 紀錄的 model_name 一律視為 `published`——YAML `model_list`
  定義的地端模型與這個功能上線前上架的外部模型都是這樣，**零資料回填**。
- `GET /api/v1/models/external` 的回應是純增加欄位（`meta`/`spend`/`registered`），
  另外會多列停用中的模型（`registered: false`、`id: null`）。

### 驗證

`scripts/test_model_lifecycle.py` 是離線整合測試：同一個 process 裡跑 admin-api 的
FastAPI app，LiteLLM 用 `httpx.MockTransport` 假造，DB 用暫存檔，**不需要任何叢集或
執行中的服務**。涵蓋狀態機、授權矩陣的取代式寫入、額度累計與強制、稽核與匯出，以及
每一頁真的渲染得出來（那些頁面是很長的 f-string，少一個變數只有送出請求時才會炸）。

```bash
python3 scripts/test_model_lifecycle.py
```

## 第三期：點數費率欄位 + 上架動線簡化（已完成）

部署前的兩項客戶要求。範圍刻意很窄，兩件都不動熱路徑（`config/` 底下那兩個檔案
**零改動**），所以不影響認證與呼叫行為。

### 點數費率：只存不算

模型多兩個欄位：每 1K 輸入 token 點數、每 1K 輸出 token 點數（`model_metadata` 的
`points_per_1k_prompt` / `points_per_1k_completion`，REAL、可 NULL）。

**這裡刻意不做累計、不做扣點、不做上限檢查。** 客戶明確說明扣點與部門／人員的總
點數上限會實作在另一套系統上，這邊只要提供填寫欄位。所以：

- `config/custom_auth.py` 與 `config/custom_logger.py` 完全沒改——沒有 `points_spend`
  表、沒有部門／人員點數上限欄位、沒有新的 429 分支。
- 外部系統要算點數的兩份資料都已經齊備：費率從 `GET /api/v1/models/external` 的
  `meta` 讀，token 數從 `usage.jsonl` 每筆 `llm_call` 的 `prompt_tokens` /
  `completion_tokens`（附 `billing_model`／`user_id`／`dept_id`）讀。
- **留空存 `NULL` 而不是 `0`**：0 在外部系統眼裡是「這個模型免費」，跟「還沒填」
  差很多。UI 顯示「未設定」。
- 費率歸在**描述性欄位**（不是 `ROUTING_FIELDS`）：它不影響請求打到哪裡去，改費率
  不該需要先把模型停用。所以 published 狀態也改得動。

介面上這兩格出現在三個地方（共用 `routers/admin_web.py` 的 `points_fields()`）：
上架表單、編輯草稿、詳情頁的「可修改的欄位」。說明文字一定要寫明「本平台不扣點」
——不然管理者會以為填了就有護欄，那是最糟的誤會：以為有，其實沒有。

### 上架動線：拿掉「key 從哪來？」

原本的第二步（各部門自己的 key ／ 共用一把）整段移除，新模型一律
`key_policy = "model"`（模型自帶 key）。要給某個部門專屬 key 的做法變成**再上架一個
模型**：同一個上游、`model_name` 加後綴（例如 `gpt-4o-deptA`）、填那個部門的 key。

關鍵是**這不是新機制**：那就是一個普通模型，走同一條草稿 → 測試 → 發布的路，
**開給誰仍然在模型授權頁決定**，可以只給一個部門也可以給多個——名稱裡的 `deptA`
只是命名慣例，不綁定授權範圍。所以刻意**不做**「複製為部門專屬模型」按鈕之類的
專屬機制，那會讓一個命名慣例看起來像一個新概念。

`model_upstreams.py` 的 `key_mode`（`choice`/`fixed_shared`）收斂成 `key_required`
布林，`name_prefix_dept`/`name_prefix_shared` 併成一個 `name_prefix`，
`suggest_model_name()` 不再吃 `key_source`。建議名稱也不再帶 `openrouter/` 前綴
——上架一定會寫一筆明確的 `key_policy`，推導不會生效，但名稱本身會讓人誤會。

**舊制完整保留、只從上架動線移除**（決策 E 的 `dept:<provider>` 模型還在跑）：

- `custom_auth._resolve_injected_key()`、`models_service` 的 `dept:*` 驗證、
  `departments.provider_keys` 一律不動；`POST /api/v1/models/external` 仍接受
  `dept:<provider>`。
- 「Provider Key」頁面留著、標成「舊制維護」——既有模型換 key 還是要它。
- **編輯草稿時政策改成「沿用原本的」而不是「依上游重新判斷」**（新增
  `models_service.get_key_policy()`）。原本的邏輯是「key 欄位留空且上游支援部門 key
  → dept:<provider>」，在新動線下會把一個明明自帶 key 的草稿改回部門 key，那個模型
  當下就會拿一把不存在的 key 去打上游。離線測試有一條專門守這件事。

### 驗證

`scripts/test_model_lifecycle.py` 加了費率往返（含小數、留空存 NULL、負數 422、
非數字的看得懂 422、範本帶入）、上架只剩兩步、表單上架的模型一律 `key_policy=model`、
以及上面那條舊制政策沿用的迴歸測試。250 項全過。

## 第二期原始評估（保留脈絡）

當初建議先不排程的理由是：委派已定案不做，OpenWebUI 本來就有一套能用且是別人維護的
授權介面，剩下的好處只有生效變快與少掉 SOP，成本則是要自己寫授權矩陣、接管正確性，
而 push 是取代式語意、一個計算錯誤就能清掉全平台權限。

實際上線後客戶回饋把「生效變快、少掉 SOP」這件事的權重拉高了（那正是他們抱怨的
「curl 流程複雜易錯」的一部分），而風險則用「強制先看 dry-run 差異 + 每筆寫入記
before/after」來對沖，於是決定做。
