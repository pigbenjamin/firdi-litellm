# 開通外部模型（給部門使用管理者）

給部門使用管理者：想讓自己部門的人使用一個目前系統裡還沒有的外部模型（例如某個
OpenAI/Claude 模型）時，怎麼操作。

整個流程分三步，全部都是 API 呼叫，**不需要任何 K8s/kubectl 存取**，也不會讓平台上
其他人正在跑的請求中斷。

## 步驟 1：把模型接進系統

先決定想要哪一種，接著呼叫 admin-api（下面兩個範例二選一）：

| 情境 | 選哪個 |
|---|---|
| 模型 OpenRouter 上就有、想讓費用算在自己部門的預算裡 | OpenRouter 路線 |
| 要用供應商官方合約／額度，或 OpenRouter 沒有這個模型 | 原生 Provider API 路線 |

```bash
# OpenRouter 路線：model_name 前面的 openrouter/ 是固定慣例不要改，
# api_key/api_base 都留空即可，系統會自動處理
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "openrouter/anthropic/claude-sonnet-4-5",
        "model": "openai/anthropic/claude-sonnet-4-5"
      }'

# 原生 Provider 路線：api_key 必填（你跟供應商申請到的官方 API key）
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "gpt-4o-mini",
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-xxxxxxxx"
      }'
```

回應 201 代表成功，立即生效不需要等待或重啟。如果回 409（模型名稱已存在），換一個
名字重試，或跟平台管理員確認是不是已經有人上架過同一個模型了。

如果不確定要填什麼（例如 `model` 這個供應商 slug 該怎麼寫），或想用「地端已經在跑的
模型」以外的特殊串接方式，還是可以直接請平台管理員協助，見
[external-models-ops.md](external-models-ops.md)。

## 步驟 2（僅 OpenRouter 路線需要）：設定部門的 OpenRouter key

如果走 OpenRouter 路線、且希望這個模型的花費算在自己部門的 OpenRouter 帳號上（而不是
共用額度），把你的 OpenRouter API key 設定進去：

```bash
curl -X PATCH "http://<node-ip>:30408/api/v1/departments/<你的部門代碼>" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"openrouter_api_key": "sk-or-v1-xxxxxxxx"}'
```

沒設定 key 的話這個模型會打不通，實際是否有共用額度可用請跟平台管理員確認。

原生 Provider API 路線不需要這一步。

## 步驟 3：開放使用權限

模型接進系統後，預設沒有任何人能用（等同上架但沒開賣），一定要手動開通：

1. 到 OpenWebUI → Workspace → Models → 選這個模型 → 設定你的部門（group）或個別使用者的授權
2. 最多等 2 分鐘會自動生效；需要立即生效可以請平台管理員協助手動觸發

> 權限異動請一律透過 OpenWebUI 畫面設定，不要透過其他管道調整，否則可能在下次自動同步時被覆蓋。
