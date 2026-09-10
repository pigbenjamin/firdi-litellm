# 開通外部模型（給部門使用管理者）

給部門使用管理者：想讓自己部門的人使用一個目前系統裡還沒有的外部模型（例如某個
OpenAI/Claude 模型）時，怎麼操作。

> **建議改用瀏覽器介面。** 平台管理帳號現在有一個網頁版的管理入口
> （`/api/v1/admin/web`），上架、測試呼叫、發布、設定誰能用、停用、刪除全部在畫面上
> 完成，欄位是選單而不是手拼 JSON，失敗會直接告訴你是金鑰問題還是模型名稱寫錯，
> 而且授權存檔即生效、不用另外到 OpenWebUI 設定一次。見
> [admin-web.md](admin-web.md)。下面這條 curl 路徑完整保留，適合寫進腳本或批次
> 上架；**2026-09 起 OpenRouter 路線的 `api_key` 也改成必填**，見步驟 1。

整個流程分三步，全部都是 API 呼叫，**不需要任何 K8s/kubectl 存取**，也不會讓平台上
其他人正在跑的請求中斷。

## 步驟 1：把模型接進系統

先決定想要哪一種，接著呼叫 admin-api（下面兩個範例二選一）：

| 情境 | 選哪個 |
|---|---|
| 模型 OpenRouter 上就有 | OpenRouter 路線 |
| 要用供應商官方合約／額度，或 OpenRouter 沒有這個模型 | 原生 Provider API 路線 |

```bash
# OpenRouter 路線：model_name 前面的 openrouter/ 是固定慣例不要改，
# api_base 留空即可（系統自動帶入 https://openrouter.ai/api/v1）。
# api_key 必填——2026-09 起一律模型自帶 key，不再有「留空退回部門 key」這條路。
curl -X POST "http://<node-ip>:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "openrouter/anthropic/claude-sonnet-4-5",
        "model": "openai/anthropic/claude-sonnet-4-5",
        "api_key": "sk-or-v1-xxxxxxxx"
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
名字重試，或跟平台管理員確認是不是已經有人上架過同一個模型了。沒帶 `api_key`
直接 422，不會默默留下一個打不通的模型。

如果不確定要填什麼（例如 `model` 這個供應商 slug 該怎麼寫），或想用「地端已經在跑的
模型」以外的特殊串接方式，還是可以直接請平台管理員協助，見
[external-models-ops.md](external-models-ops.md)。

**要讓某個部門的花費算在自己帳號上**：不是靠留空 key 退回部門設定（這條路已經在
2026-09 拔除），而是**同上游多上架一個模型**——同一個 slug，`model_name` 加個
後綴（例如 `gpt-4o-deptA`），`api_key` 填那個部門自己的 key。它就是一個普通模型，
開給誰仍然由下面的授權步驟決定，命名後綴只是給人看的慣例，不會綁定授權範圍。

## 步驟 2：開放使用權限

模型接進系統後，預設沒有任何人能用（等同上架但沒開賣），一定要手動開通：

1. OpenWebUI → 設定 → 連線 → 編輯 LiteLLM 那條連線 → 在「模型 IDs」新增一筆，
   **字串必須跟步驟 1 的 `model_name` 逐字完全相同**（見下方警告）
2. OpenWebUI → Workspace → Models → 選這個模型 → 設定你的部門（group）或個別使用者的授權
3. 最多等 2 分鐘會自動生效；需要立即生效可以請平台管理員協助手動觸發

> 權限異動請一律透過 OpenWebUI 畫面設定，不要透過其他管道調整，否則可能在下次自動同步時被覆蓋。

### ⚠️ 最容易踩的坑：OpenWebUI 的模型 ID 必須等於 `model_name`

## 完整範例：上架 OpenRouter 的 `openai/gpt-5.6-terra`

以「把 OpenRouter 上的 `openai/gpt-5.6-terra` 開給 DE5000 使用」為例，把兩個步驟串起來
（節點位址 `10.0.220.54:30408` 請換成你環境的實際位址）：

```bash
# 步驟 1：接進系統。model_name 保留 openrouter/ 前綴，api_key 必填
curl -X POST "http://10.0.220.54:30408/api/v1/models/external" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model_name": "openrouter/openai/gpt-5.6-terra",
        "model": "openai/gpt-5.6-terra",
        "api_key": "sk-or-v1-xxxxxxxx"
      }'
# → {"model_name":"openrouter/openai/gpt-5.6-terra","status":"created"}
```

步驟 2 在 OpenWebUI 畫面上做：

1. 設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」加一筆
   `openrouter/openai/gpt-5.6-terra`（**跟上面 `model_name` 逐字相同**）
2. Workspace → Models → 選 `openrouter/openai/gpt-5.6-terra` → 授權給 group `DE5000`


