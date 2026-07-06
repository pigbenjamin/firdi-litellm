# LiteLLM Custom Auth — 測試計劃

## 環境前置確認

```bash
# 確認所有服務正常運行
docker compose ps

# 確認 LiteLLM 可連線
curl -s http://localhost:4000/health

# 確認 log 目錄存在
ls logs/
```

---

## 測試項目

### T01 — 無效 API key 被拒絕

**目的**：確認 `custom_auth.py` 對不存在的 key 回傳 401，防止未授權請求進入。

**預期結果**：HTTP 401，並寫入 `auth_denied / invalid_key` log

```bash
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer invalid-key-that-does-not-exist" \
  -H "Content-Type: application/json" \
  -d '{"model":"local-qwen3.5-9b","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
# 預期輸出: 401
```

---

### T02 — 有效使用者可列出模型

**目的**：確認合法 key 可通過認證，並取得 LiteLLM 的可用模型清單。

**預期結果**：HTTP 200，`data` 陣列中包含 `local-qwen3.5-9b`

```bash
curl -s \
  http://localhost:4000/v1/models \
  -H "Authorization: Bearer dev-local-key-001" | python3 -m json.tool
```

---

### T03 — local user 可呼叫 allowlist 內的模型

**目的**：確認 `dev-local-key-001` 對 `local-qwen3.5-9b` 有存取權，且模型正確回應。

**預期結果**：HTTP 200，回傳 chat completion，並寫入 `llm_call / success` log

```bash
curl -s \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-local-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-qwen3.5-9b",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}],
    "max_tokens": 16,
    "temperature": 0
  }' | python3 -m json.tool
```

---

### T04 — local user 被擋在 allowlist 外的外部模型

**目的**：確認 `dev-local-key-001` 無法呼叫 `gpt-4o-mini`（不在其 allowlist）。model allowlist 由 `custom_auth.py` 在 auth 階段攔截，請求不會到達 LiteLLM routing。

**預期結果**：HTTP 403，並寫入 `auth_denied / model_not_allowed` log

```bash
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-local-key-001" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
# 預期輸出: 403
```

---

### T05 — paid user 可呼叫 gpt-oss-20b

**目的**：確認 `dev-paid-key-001` 可存取 GPU 1 上的 `local-gpt-oss-20b`，且路由正確。

**預期結果**：HTTP 200，回傳 chat completion，並寫入 `llm_call / success` log

```bash
curl -s \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-paid-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-gpt-oss-20b",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}],
    "max_tokens": 16,
    "temperature": 0
  }' | python3 -m json.tool
```

---

### T06 — local user 被擋在 allowlist 外的本地模型

**目的**：確認 `dev-local-key-001` 無法呼叫 `local-gpt-oss-20b`（不在其 allowlist）。與 T04 相同機制，在 auth 階段即被攔截。

**預期結果**：HTTP 403，並寫入 `auth_denied / model_not_allowed` log

```bash
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-local-key-001" \
  -H "Content-Type: application/json" \
  -d '{"model":"local-gpt-oss-20b","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
# 預期輸出: 403
```

---

### T07 — Streaming 回應正常

**目的**：確認 SSE streaming 模式可正常建立連線並收到資料片段。

**預期結果**：HTTP 200，回傳多行 `data: {...}` SSE 格式，最後一行為 `data: [DONE]`

```bash
curl -s \
  http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-local-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-qwen3.5-9b",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}],
    "max_tokens": 16,
    "temperature": 0,
    "stream": true
  }'
```

---

### T08 — Rate limit 生效

**目的**：確認 RPM=2 的使用者在短時間連續請求後收到 HTTP 429。

**預期結果**：4 次請求中，前 2 次回傳 200，第 3 次起回傳 429

```bash
for i in 1 2 3 4; do
  echo -n "request $i: "
  curl -s -o /dev/null -w "%{http_code}\n" \
    http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer dev-rate-limit-key-001" \
    -H "Content-Type: application/json" \
    -d '{"model":"local-qwen3.5-9b","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
  sleep 0.2
done
# 預期輸出:
# request 1: 200
# request 2: 200
# request 3: 429
# request 4: 429
```

---

### T09 — 使用記錄寫入確認

**目的**：確認 T01～T08 執行後，各類事件都已正確寫入 `logs/usage.jsonl`。

**預期結果**：log 檔包含 `llm_call` 及 `auth_denied` 記錄

```bash
# 查看所有 log
cat logs/usage.jsonl | python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat logs/usage.jsonl

# 統計各類事件數量
echo "=== LLM 呼叫成功 ===" && grep -c '"status": "success"' logs/usage.jsonl
echo "=== LLM 呼叫失敗 ===" && grep -c '"status": "failure"' logs/usage.jsonl || echo 0
echo "=== 無效 key ===" && grep -c '"reason": "invalid_key"' logs/usage.jsonl
echo "=== model 被擋 ===" && grep -c '"reason": "model_not_allowed"' logs/usage.jsonl
```

---

## 一鍵執行（Python 自動化測試）

### 完整測試（兩個 vLLM 皆已啟動）

```bash
python3 tests/test_litellm.py
```

### vLLM 載入中，先跳過 rate limit

```bash
python3 tests/test_litellm.py --skip-rate-limit
```

### 只有 Qwen 啟動，跳過 gpt-oss 相關項目

```bash
python3 tests/test_litellm.py --skip-gpt-oss --skip-rate-limit
```

### 也測試外部 paid 模型（需設定 OPENAI_API_KEY）

```bash
python3 tests/test_litellm.py --paid-model gpt-4o-mini
```

### 指定自訂 base URL

```bash
python3 tests/test_litellm.py --base-url http://localhost:4000
```

---

## 使用者與模型 allowlist 對照

| 使用者 | API Key | 可用模型 | RPM | TPM |
|--------|---------|----------|-----|-----|
| local-dev-user | `dev-local-key-001` | `local-qwen3.5-9b` | 60 | 100,000 |
| paid-dev-user | `dev-paid-key-001` | `local-qwen3.5-9b`、`local-gpt-oss-20b`、`gpt-4o-mini`、`gemini-2.0-flash` | 120 | 300,000 |
| rate-limit-test-user | `dev-rate-limit-key-001` | `local-qwen3.5-9b` | 2 | 100,000 |

---

## Log 格式參考

| 欄位 | 說明 |
|------|------|
| `timestamp` | ISO 8601 格式，UTC 時間 |
| `event` | `llm_call`（LLM 呼叫）或 `auth_denied`（認證拒絕） |
| `status` | `success` / `failure`（僅 llm_call 類型） |
| `reason` | `invalid_key` / `model_not_allowed`（僅 auth_denied 類型） |
| `user_id` | users.json 中的 `user_id` |
| `key_name` | users.json 中的 `key_name` |
| `model` | 請求的模型名稱 |
| `prompt_tokens` | 輸入 token 數（僅成功的 llm_call） |
| `completion_tokens` | 輸出 token 數（僅成功的 llm_call） |
| `total_tokens` | 總 token 數（僅成功的 llm_call） |
| `latency_ms` | 回應延遲（毫秒） |
| `error` | 錯誤訊息（僅失敗的 llm_call） |
