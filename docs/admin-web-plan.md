# 部門管理入口（admin-web）實作交接

把現在只能用 curl 做的平台管理動作（上架外部模型、設定部門 provider key、觸發權限同步），
變成中央管理帳號在網頁上就能完成的流程。掛在 admin-api 的 `/api/v1/admin/web`。

**狀態：規劃與規格完成，程式碼一行都還沒動。** 本文件是給實作者的交接摘要；
完整內容在下面三份文件裡。

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

## 第二期（翻轉授權權威來源）：建議先不排程

原本支持第二期最有力的論點是「OpenWebUI 的授權編輯是管理員全有或全無，表達不了部門委派」。
既然已定案不做委派，這個能力缺口就不存在了，而 OpenWebUI 本來就有一套能用、
且是別人維護的授權介面。

剩下的理由只有生效變快（2 分鐘 → 30 秒）與少掉 pull → PATCH → push 的 SOP；
成本則是要自己寫授權矩陣、為約 435 名使用者寫個人授權介面、並接管正確性——
而 push 是**取代式**語意，一個計算錯誤就能清掉全平台權限。

建議第一期上線後先實際用一段時間，再決定。細節保留在規劃文件裡。
