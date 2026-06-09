# LiteLLM + 本地 vLLM 代理

本專案以 Docker Compose 啟動一套 LLM 代理服務，透過自訂認證機制管控不同使用者對模型的存取權限，並記錄所有使用 log。

## 服務架構

```
客戶端
  │  Authorization: Bearer <api-key>
  ▼
LiteLLM Proxy（:4000）
  ├─ custom_auth.py   — 驗證 key、比對 model allowlist
  ├─ custom_logger.py — 記錄每次 LLM 呼叫至 logs/usage.jsonl
  │
  ├─► vllm-qwen      （:8001，GPU 0）— Qwen/Qwen3.5-9B
  ├─► vllm-gpt-oss-20b（:8002，GPU 1）— openai/gpt-oss-20b
  ├─► OpenAI API     （外部，需 OPENAI_API_KEY）
  └─► Gemini API     （外部，需 GEMINI_API_KEY）
```

## 檔案結構

```
├── docker-compose.yml          # 服務拓樸與 GPU 分配
├── .env                        # 實際執行時的環境變數（不進版控）
├── .env.example                # 環境變數範本
├── config/
│   ├── litellm_config.yaml     # LiteLLM 模型路由、認證、callback 設定
│   ├── custom_auth.py          # API key 驗證、model allowlist、auth deny log
│   ├── custom_logger.py        # LiteLLM callback，記錄 LLM 呼叫 log
│   └── users.json              # 使用者清單（key、模型權限、rate limit）
├── logs/
│   └── usage.jsonl             # 使用記錄（容器重啟後保留）
└── tests/
    ├── test_litellm.py         # 自動化 smoke test
    └── TEST_PLAN.md            # 測試計劃與手動測試指令
```

## 快速啟動

```bash
cp .env.example .env
# 視需要填入 OPENAI_API_KEY、GEMINI_API_KEY、HF_TOKEN
docker compose up -d
```

> 第一次啟動 vLLM 容器需要從 HuggingFace 下載模型，可能需要數分鐘。

## 可用模型

| 模型名稱 | 實際模型 | 位置 | GPU |
|---------|---------|------|-----|
| `local-qwen3.5-9b` | `Qwen/Qwen3.5-9B` | `vllm-qwen:8000` | GPU 0 |
| `local-gpt-oss-20b` | `openai/gpt-oss-20b` | `vllm-gpt-oss-20b:8000` | GPU 1 |
| `gpt-4o-mini` | OpenAI | 外部 API | — |
| `gemini-2.0-flash` | Gemini | 外部 API | — |

## 使用方式

### 本地 Qwen（一般使用者）

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-local-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-qwen3.5-9b",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}]
  }'
```

### 本地 GPT-OSS（付費使用者）

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-paid-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-gpt-oss-20b",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}]
  }'
```

### 外部 OpenAI（付費使用者）

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-paid-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "用一句話打個招呼"}]
  }'
```

## 自訂認證（Custom Auth）

### 認證流程

```
請求進入
  → custom_auth.py：比對 users.json 驗證 API key     → 不存在回傳 401
  → custom_auth.py：比對使用者的 model allowlist      → 不在清單回傳 403
  → LiteLLM 執行 rpm_limit / tpm_limit rate limiting
  → 路由至對應的 LLM 後端
```

### 使用者設定（users.json）

| 使用者 | API Key | 可用模型 | RPM | TPM |
|--------|---------|----------|-----|-----|
| local-dev-user | `dev-local-key-001` | `local-qwen3.5-9b` | 60 | 100,000 |
| paid-dev-user | `dev-paid-key-001` | `local-qwen3.5-9b`、`local-gpt-oss-20b`、`gpt-4o-mini`、`gemini-2.0-flash` | 120 | 300,000 |
| rate-limit-test-user | `dev-rate-limit-key-001` | `local-qwen3.5-9b` | 2 | 100,000 |

### 重要設計說明

- **Model allowlist** 由 `custom_auth.py` 在 auth 階段直接攔截，不依賴 LiteLLM 的內部機制
- **Rate limiting（RPM/TPM）** 由 LiteLLM in-memory 計數器執行，不需要 DB
- `custom_auth_run_common_checks` 設為 `false`，因為 common checks 需要 Postgres DB，本專案未使用
- `team_id` / `team_alias` 儲存在 `metadata` 欄位，不傳入 `UserAPIKeyAuth` 頂層（避免 LiteLLM 觸發 DB 查詢）

### 串接真實認證系統

將 `config/custom_auth.py` 中的 `_find_user()` 替換為對應你的認證服務或資料庫的查詢邏輯，再將結果映射至 `UserAPIKeyAuth` 即可，其他邏輯不需改動。

## 使用記錄（Logging）

每次請求都會記錄至 `logs/usage.jsonl`（JSON Lines 格式，每行一筆），容器重啟後保留。

### 記錄類型

**成功的 LLM 呼叫**
```json
{"timestamp": "2025-06-09T12:00:00+00:00", "event": "llm_call", "status": "success", "user_id": "user-local-dev", "key_name": "local-dev-user", "model": "local-qwen3.5-9b", "prompt_tokens": 10, "completion_tokens": 16, "total_tokens": 26, "latency_ms": 1234}
```

**無效 key 被拒絕**
```json
{"timestamp": "2025-06-09T12:00:01+00:00", "event": "auth_denied", "reason": "invalid_key"}
```

**model 不在 allowlist**
```json
{"timestamp": "2025-06-09T12:00:02+00:00", "event": "auth_denied", "reason": "model_not_allowed", "user_id": "user-local-dev", "key_name": "local-dev-user", "model": "gpt-4o-mini"}
```

**LLM 呼叫失敗**
```json
{"timestamp": "2025-06-09T12:00:03+00:00", "event": "llm_call", "status": "failure", "user_id": "user-local-dev", "key_name": "local-dev-user", "model": "local-qwen3.5-9b", "error": "...", "latency_ms": 500}
```

### 查看 log

```bash
# 查看所有記錄
cat logs/usage.jsonl

# 即時追蹤
tail -f logs/usage.jsonl

# 只看 LLM 呼叫成功的記錄
grep '"status": "success"' logs/usage.jsonl

# 只看被拒絕的記錄
grep '"event": "auth_denied"' logs/usage.jsonl
```

## GPU 設定

每個模型透過 `deploy.resources.reservations.devices` 分配專屬 GPU，不依賴 `NVIDIA_VISIBLE_DEVICES` 環境變數：

- **Qwen** → GPU 0
- **GPT-OSS** → GPU 1

若需要 Tensor Parallelism（模型跨多張 GPU）：

```bash
QWEN_TENSOR_PARALLEL_SIZE=2
GPT_OSS_TENSOR_PARALLEL_SIZE=2
```

若 VRAM 不足，調低以下參數：

```bash
QWEN_GPU_MEMORY_UTILIZATION=0.5   # 預設 0.55
QWEN_MAX_MODEL_LEN=16384          # 預設 32768
```

## 環境變數說明

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `LITELLM_MASTER_KEY` | LiteLLM 管理員 key（可繞過 custom auth） | `sk-firdi-master-change-me` |
| `USER_AUTH_CONFIG_PATH` | users.json 在容器內的路徑 | `/app/config/users.json` |
| `LOG_PATH` | usage log 在容器內的路徑 | `/app/logs/usage.jsonl` |
| `HF_TOKEN` | HuggingFace token（下載 gated 模型用） | 空 |
| `LITELLM_PORT` | LiteLLM 對外 port | `4000` |
| `QWEN_VLLM_PORT` | Qwen vLLM 對外 port | `8001` |
| `GPT_OSS_VLLM_PORT` | GPT-OSS vLLM 對外 port | `8002` |
| `VLLM_SHM_SIZE` | vLLM 容器的 shared memory 大小 | `32gb` |

## 測試

詳細測試計劃請參考 [tests/TEST_PLAN.md](tests/TEST_PLAN.md)。

```bash
# 完整測試（兩個 vLLM 皆已啟動）
python3 tests/test_litellm.py

# vLLM 載入中，先跳過 rate limit
python3 tests/test_litellm.py --skip-rate-limit

# 只有 Qwen 啟動
python3 tests/test_litellm.py --skip-gpt-oss --skip-rate-limit
```
