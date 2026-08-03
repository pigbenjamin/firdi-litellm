# 直接用 API 存取（非 OpenWebUI 入口）

給不透過 OpenWebUI 聊天介面、而是用程式／agent framework／coding 工具直接呼叫平台的人。

權限跟 OpenWebUI 完全一致：同一份部門／使用者模型授權、同一套 rate limit 設定，只是換一個
入口進來。**不需要另外開通**——你的帳號本來就有一把個人 API key。

## 1. 取得 API key

每個使用者在建立帳號時就配發了一把 `sk-{uuid}` 格式的個人 key，用自己的 Keycloak 帳號就
能查到，不用找管理員。

**用瀏覽器打開這個網址：**

```
http://<admin-api 位址>/api/v1/me/web/login
```

會導向 Keycloak 登入畫面——跟你平常登入 OpenWebUI 是同一套帳號密碼。登入完自動跳回來，
網頁上直接顯示你的部門、可用模型、還有你的 API Key：

```
你好，alice
部門         RD
Email        alice@example.com
可用模型      gemma-4-26B-A4B-it, gemma-4-31B-it
API Key      sk-xxxxxxxx
              [重設我的 Key]
```

全程只需要瀏覽器，不用碰終端機。這個頁面只會顯示**你自己**的資料，不會看到別人的。

key 外洩或想換掉時，直接在這個網頁上按「重設我的 Key」即可——**舊 key 立即失效**，所有
填過舊 key 的工具都要記得更新。

key 等同你的帳號密碼，會帶著你的全部模型權限，**不要 commit 進 git、不要貼在共用文件裡**。

> 如果你是要寫自動化腳本、需要用程式（而非瀏覽器）取得或重設 key，改用 JSON 端點
> `GET /api/v1/me` / `POST /api/v1/me/regenerate-key`（見
> [admin-api.md](admin-api.md) 的「自助端點」一節）。**但這條路目前只驗證過
> `Authorization: Bearer <Keycloak access token>` 這個介面本身沒問題，還沒有確認
> 「腳本在無瀏覽器環境下怎麼拿到那個 token」這件事怎麼做**——請先找平台管理員確認
> 可行方式，不要假設現成能用。

## 2. Base URL

平台是標準的 **OpenAI 相容 API**：

```
http://<平台位址>/v1
```

叢集內部（同一個 k8s cluster 的其他服務）用：

```
http://litellm-service.ai-platform.svc.cluster.local:4000/v1
```

叢集外部走 NodePort `30400`，前面由公司既有的反向代理終結對外連線；實際對外網址請洽平台
管理員。**直連 NodePort 是明文 HTTP**，key 與對話內容不會加密，請走反向代理提供的位址。

## 3. 基本用法

### curl

```bash
curl http://<平台位址>/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-4-31B-it",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://<平台位址>/v1", api_key="sk-你的key")

resp = client.chat.completions.create(
    model="gemma-4-31B-it",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### 環境變數（多數 agent / coding 工具吃這組）

```bash
export OPENAI_BASE_URL=http://<平台位址>/v1
export OPENAI_API_KEY=sk-你的key
```

### Anthropic Messages 格式

只認 Anthropic Messages API、不吃 OpenAI 格式的工具，可以改打 `/v1/messages`
（LiteLLM 內建轉譯，2026-07-29 實測可用）：

```bash
curl http://<平台位址>/v1/messages \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-4-26B-A4B-it",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

回應是 Anthropic 格式（`content: [{type: "text", ...}]`、`stop_reason`）。tools 也照
Anthropic 的 `input_schema` 寫法。

## 4. 可用模型

`GET /v1/models` 會列出**你的權限範圍內**的模型。常用的是：

| model | 用途 | 備註 |
|---|---|---|
| `gemma-4-31B-it` | 精品層，複雜推理 | 支援 thinking（預設關閉，見下）。context 上限 **65536** |
| `gemma-4-26B-A4B-it` | 快捷層，日常對話／工具呼叫 | MoE，回應快 |
| `embeddinggemma-300m` | 文字向量 | 768 維，context 2048 |
| `marker/pdf-to-md` | PDF → Markdown | 走檔案路徑而非上傳，需要共享儲存，用途特殊請先問管理員 |

## 5. Thinking 模式（31b）

31b 預設**不**思考。要打開，加 `reasoning_effort`：

```json
{
  "model": "gemma-4-31B-it",
  "reasoning_effort": "high",
  "messages": [{"role": "user", "content": "..."}]
}
```

思考內容回在 `choices[0].message.provider_specific_fields.reasoning`（**不是**
`reasoning_content`，也不在 `content` 裡）。OpenAI SDK 沒有這個欄位的型別，需要自己從
raw response 取。

## 6. Tool / function calling

**31b 與 26b 都完整支援**（2026-07-29 實測：單次呼叫、一次回多個 tool_call、streaming 增量
delta、tool 結果回填續談，全部通過）。用標準 OpenAI `tools` / `tool_choice` 寫法即可：

```python
resp = client.chat.completions.create(
    model="gemma-4-31B-it",
    messages=[{"role": "user", "content": "台北現在天氣如何？"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }],
)
# resp.choices[0].finish_reason == "tool_calls"
```

streaming 時 tool_call 是逐段 delta（`arguments` 會被拆成好幾塊送），照 OpenAI 的標準方式
累積即可。

## 7. 已知限制

- **Context 上限 65536 tokens**（31b）。把整個 repo 塞進 context 的 coding agent 很容易撞到；
  放大這個值要付 KV cache 的代價，需要時請提出討論。
- **目前沒有實質的 rate limit**：平台有 per-user／per-dept 的 RPM/TPM 機制，但目前幾乎沒有
  設值。這代表**一個跑掉的 agent 迴圈可以把 GPU 吃滿、影響到所有 OpenWebUI 使用者**。開放
  初期靠自律，請自己控制併發（建議 ≤ 5 併發）並避免無上限的重試迴圈。之後會依實際流量
  補上限制。
- **NodePort 直連是明文 HTTP**，請走反向代理位址。
- 用量會記錄（Langfuse + `usage.jsonl`），來源標記為 `api_key`，與 OpenWebUI 流量分開統計。

## 8. 疑難排解

| 症狀 | 原因 |
|---|---|
| `401 Invalid API key` | key 打錯，或帳號被停用（blocked） |
| `403 Model '...' is not allowed for this user` | 你的部門／個人授權沒有這個模型，找管理員開通 |
| `403 User's department is not configured` | 帳號的部門欄位有問題，找管理員 |
| `429 ... exceeded RPM/TPM limit` | 打到部門或個人的速率上限 |
| 回應很慢／卡住 | GPU 排隊中。31b 同時最多處理 20 個請求，超過的排隊 |
