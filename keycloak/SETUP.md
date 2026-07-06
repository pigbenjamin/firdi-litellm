# Keycloak 使用者同步設定指南

當 Keycloak 的使用者發生新增、修改、停用、群組異動時，自動同步到 admin-api（SQLite），讓 LiteLLM 的 model 權限保持最新。

## 架構

```
Keycloak User/Admin Event
        ↓
Event Listener SPI (keycloak-user-sync-listener.jar)
        ↓  逗號分隔，同時通知兩個 endpoint
        ├──► POST http://10.90.20.55:8763/keycloak/user-sync   (docblock-rag → PostgreSQL)
        └──► POST http://admin-api:8080/api/v1/sync/keycloak   (firdi-litellm → SQLite)
```

---

## 一、Keycloak Admin Console：建立 Client

登入 Keycloak Admin Console → 選 realm `FIRDI-AI-Platform`

```
Clients → Create client
```

設定：

| 欄位 | 值 |
|------|----|
| Client ID | `user-sync-service` |
| Client authentication | ON |
| Authorization | OFF |
| Service accounts roles | ON |

儲存後進入：

```
Clients → user-sync-service → Service account roles
```

加入 `realm-management` 的以下權限：

```
view-users
query-users
view-groups
query-groups
view-realm
```

取得 secret：

```
Clients → user-sync-service → Credentials → Client Secret
```

---

## 二、Keycloak Admin Console：啟用 Event Listener

```
Realm settings → Events
```

### Event listeners

加入：

```
user-sync-listener
```

### User events settings

```
Save events: ON
```

### Admin events settings

```
Save events: ON
Include representation: OFF
```

---

## 三、Docker Compose 設定

Keycloak 的 compose 檔位於：

```
/opt/AIC/outline/docker/docker-compose.middleware.yml
```

在 keycloak service 加入：

### volumes（掛載 JAR）

```yaml
volumes:
  - /home/ai-x/km/repo/firdi-litellm/keycloak/plugins/keycloak-user-sync-listener/target/keycloak-user-sync-listener-1.0.0.jar:/opt/keycloak/providers/keycloak-user-sync-listener-1.0.0.jar
```

### environment（通知兩個 webhook URL）

```yaml
environment:
  USER_SYNC_WEBHOOK_URL: "http://10.90.20.55:8763/keycloak/user-sync,http://admin-api:8080/api/v1/sync/keycloak"
  USER_SYNC_WEBHOOK_SECRET: "<與 admin-api .env 的 WEBHOOK_SECRET 相同>"
```

> `USER_SYNC_WEBHOOK_SECRET` 必須與 admin-api 的 `WEBHOOK_SECRET` 相同。

---

## 四、重建 Keycloak Provider 並重啟

每次更新 JAR 後都要執行：

```bash
# 1. 重建 provider registry（讓 Keycloak 掃描新 JAR）
docker exec -it keycloak-outline /opt/keycloak/bin/kc.sh build

# 2. 重啟 Keycloak
docker compose -f /opt/AIC/outline/docker/docker-compose.middleware.yml up -d
```

---

## 五、admin-api 環境變數

在 `/home/ai-x/km/repo/firdi-litellm/.env` 填入：

```env
WEBHOOK_SECRET=<自訂一組高強度亂數字串>
KEYCLOAK_URL=https://125.228.83.116:49314
KEYCLOAK_REALM=FIRDI-AI-Platform
KEYCLOAK_CLIENT_ID=user-sync-service
KEYCLOAK_CLIENT_SECRET=<從 Keycloak Credentials 頁面取得>
KEYCLOAK_SSL_VERIFY=false
```

> `KEYCLOAK_SSL_VERIFY=false` 是因為 Keycloak 使用 self-signed certificate。
> 若有正式 CA 憑證，改成憑證路徑：`KEYCLOAK_SSL_VERIFY=/app/certs/keycloak-ca.crt`

### 取得 CA 憑證（可選，用於正式環境）

```bash
openssl s_client -showcerts \
  -connect 125.228.83.116:49314 \
  </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > keycloak/keycloak-ca.crt
```

---

## 六、K8s Secret 設定

更新 `k8s/admin-api/secret.yaml` 的實際值後套用：

```bash
kubectl apply -f k8s/admin-api/secret.yaml
kubectl rollout restart deployment/admin-api -n ai-platform
```

---

## 七、測試驗證

### 取得 Token（確認 client credentials 正常）

```bash
curl -k -X POST \
  "https://125.228.83.116:49314/realms/FIRDI-AI-Platform/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=user-sync-service" \
  -d "client_secret=<YOUR_SECRET>"
```

### 手動觸發 sync endpoint（確認 admin-api 正常）

```bash
curl -X POST http://localhost:8080/api/v1/sync/keycloak \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"user_id": "<keycloak-user-uuid>", "event_type": "UPDATE", "source": "admin_event"}'
```

期待回應：

```json
{"status": "updated", "user_id": "..."}
// 或新使用者：
{"status": "created", "user_id": "...", "api_key": "sk-..."}
```

---

## 注意事項

- 部門（dept_id）必須先在 admin-api 建立，使用者同步才會成功（否則回傳 `skipped`）
- Keycloak Group path level 1 會對應到 `dept_id`，例如 `/Engineering/Backend` → `dept_id = Engineering`
- `api_key` 在使用者首次建立時自動產生，之後更新不會改變
- DELETE 事件只做 `blocked=true`（軟刪除），不移除資料
