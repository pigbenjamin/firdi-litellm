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

Keycloak 本身可能跑在 Docker Compose 或 K8s，下面「三、部署 Provider JAR」依實際情況擇一即可，其餘章節（建立 Client、啟用 Event Listener、admin-api 端設定）兩種情況通用。

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

（這個下拉選單裡選得到 `user-sync-listener`，本身就代表 Provider JAR 已經被 Keycloak 正確載入；選不到就代表 JAR 沒進 `/opt/keycloak/providers/` 或 build 失敗，先處理第三節。）

### User events settings

```
Save events: ON
```

### Admin events settings

```
Save events: ON
Include representation: OFF
```

> ⚠️ **這兩個「Save events」開關不是只管「要不要留存稽核紀錄」**——Keycloak 只有在對應開關是 ON 的情況下，才會把事件「派送」給 Event Listener 清單裡的 provider。Admin Console 上管理員幫使用者按 SAVE 屬於 **Admin event**，`Admin events settings → Save events` 沒開，我們的 plugin 完全收不到這個事件，跟 UI 上存檔有沒有成功無關（UI 一定顯示成功）。

---

## 三、部署 Provider JAR

Provider JAR 在容器內固定要放在：

```
/opt/keycloak/providers/keycloak-user-sync-listener-1.0.0.jar
```

依 Keycloak 實際跑在 Docker Compose 還是 K8s，選其中一種方式把 JAR 放進去。

### 3.1 Docker Compose

Keycloak 的 compose 檔位於：

```
/opt/AIC/outline/docker/docker-compose.middleware.yml
```

在 keycloak service 加入：

**volumes（掛載 JAR）**

```yaml
volumes:
  - /home/ai-x/km/repo/firdi-litellm/keycloak/plugins/keycloak-user-sync-listener/target/keycloak-user-sync-listener-1.0.0.jar:/opt/keycloak/providers/keycloak-user-sync-listener-1.0.0.jar
```

**environment（通知的 webhook URL，逗號分隔可通知多個 endpoint）**

```yaml
environment:
  USER_SYNC_WEBHOOK_URL: "http://10.90.20.55:8763/keycloak/user-sync,http://admin-api:8080/api/v1/sync/keycloak"
  USER_SYNC_WEBHOOK_SECRET: "<與 admin-api .env 的 WEBHOOK_SECRET 相同>"
```

`command: start`（非 `--optimized`）會在每次啟動時自動偵測 providers 目錄變化並重新 build，不需要額外手動跑 `kc.sh build`；如果 command 是 `start --optimized`，才需要照第四節手動重建。

### 3.2 K8s

JAR 只有幾 KB，遠低於 ConfigMap 1MiB 限制，用 **ConfigMap 掛成 volume** 是最簡單的方式，不用管 Pod 排到哪個節點、也不用維護一個自訂 image。

**1. 建立 ConfigMap**（JAR 已經進這個 repo 的 git，見 [target/keycloak-user-sync-listener-1.0.0.jar](plugins/keycloak-user-sync-listener/target/keycloak-user-sync-listener-1.0.0.jar)，`.gitignore` 對這個檔案開了例外）：

```bash
kubectl create configmap keycloak-user-sync-listener \
  --from-file=keycloak-user-sync-listener-1.0.0.jar=keycloak/plugins/keycloak-user-sync-listener/target/keycloak-user-sync-listener-1.0.0.jar \
  -n <Keycloak所在namespace> \
  --dry-run=client -o yaml | kubectl apply -f -
```

**2. Deployment 加 `volumeMounts` / `volumes`**——`volumeMounts` 在 container 內（宣告這個 container 要把哪個 volume 掛到哪個路徑），`volumes` 要跟 `containers:` **同一層**（pod 層級欄位，宣告 pod 有哪些 volume 可用），**不要**縮排進 container 底下，否則 `kubectl apply` 會報 `strict decoding error: unknown field "spec.template.spec.containers[0].volumes"`：

```yaml
      containers:
      - name: keycloak
        image: keycloak/keycloak:26.1.4
        args: ["start-dev"]
        env:
          # ...原有 env...
        ports:
        - containerPort: 8080
        volumeMounts:
        - name: user-sync-listener
          mountPath: /opt/keycloak/providers/keycloak-user-sync-listener-1.0.0.jar
          subPath: keycloak-user-sync-listener-1.0.0.jar
      volumes:                        # ← 跟上面的 containers: 同一縮排層級
      - name: user-sync-listener
        configMap:
          name: keycloak-user-sync-listener
```

**3. `USER_SYNC_WEBHOOK_URL` 要指向叢集內部能解析的位址**，不要直接拿 Keycloak 自己對外用的網域（例如 `KC_HOSTNAME`）或其他猜測的 service 名稱來湊——admin-api 是同叢集的 pod，直接用叢集內部 Service DNS 最穩，不用依賴外部 DNS/Gateway 是否有 hairpin 解析：

```yaml
      - name: USER_SYNC_WEBHOOK_URL
        value: "http://admin-api-service.ai-platform.svc.cluster.local:8080/api/v1/sync/keycloak"
      - name: USER_SYNC_WEBHOOK_SECRET
        value: "<與 admin-api .env 的 WEBHOOK_SECRET 相同>"
```

（`admin-api-service.<namespace>.svc.cluster.local` 是跨 namespace 存取的完整寫法；`<namespace>` 換成 admin-api 實際部署的 namespace，這個 repo 預設是 `ai-platform`。）

**4. 套用**：

```bash
kubectl apply -f 06-keycloak.yaml   # 檔名依實際情況
```

`args: ["start-dev"]` 一樣會在啟動時自動 build provider，不需要額外的 `kc.sh build` 步驟；但如果只是改了 ConfigMap 內容（例如換新版 JAR）而 Deployment 本身沒變，Pod 不會自動重建，要手動：

```bash
kubectl rollout restart deployment/<keycloak-deployment-name> -n <namespace>
```

---

## 四、admin-api 環境變數

在 admin-api 這台機器（或 K8s 叢集）的 `.env` 填入：

```env
WEBHOOK_SECRET=<自訂一組高強度亂數字串，須與 Keycloak 端的 USER_SYNC_WEBHOOK_SECRET 完全一致>
KEYCLOAK_URL=https://125.228.83.116:49314
KEYCLOAK_REALM=FIRDI-AI-Platform
KEYCLOAK_CLIENT_ID=user-sync-service
KEYCLOAK_CLIENT_SECRET=<從 Keycloak Credentials 頁面取得>
KEYCLOAK_SSL_VERIFY=false
```

> `KEYCLOAK_SSL_VERIFY=false` 是因為 Keycloak 使用 self-signed certificate。
> 若有正式 CA 憑證，改成憑證路徑：`KEYCLOAK_SSL_VERIFY=/app/certs/keycloak-ca.crt`

**若 admin-api 跑在 K8s、且 Keycloak 也在同一叢集**：`KEYCLOAK_URL` 同樣要填叢集內部能解析的位址（例如 `http://firdi-keycloak.default.svc.cluster.local:8080`，`http://` 不是 `https://`，視 Keycloak 那邊實際的 Service/port 而定），不要直接填 Keycloak 對外用的公開網域——admin-api 的 pod 對那個公開網域可能做不了 DNS 解析（叢集 DNS 沒有 hairpin 規則），會在呼叫 Keycloak token endpoint 時噴 `httpx.ConnectError: Name or service not known`，導致 sync API 回 500。

### 取得 CA 憑證（可選，用於正式環境）

```bash
openssl s_client -showcerts \
  -connect 125.228.83.116:49314 \
  </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > keycloak/keycloak-ca.crt
```

---

## 五、K8s Secret 設定（admin-api）

admin-api 的 `WEBHOOK_SECRET` / `KEYCLOAK_*` 這組設定是透過 `admin-api-secrets` 這個 K8s Secret 注入的，**不是**手動編輯某個 yaml 檔案——`scripts/deploy.sh` 的 `deploy_secrets()` 會直接從 repo 根目錄的 `.env` 讀值、動態產生並 apply：

```bash
./scripts/deploy.sh secrets
```

改完 `.env` 之後跑這個指令，觀察輸出：

- `secret/admin-api-secrets configured` → 有更新
- `secret/admin-api-secrets unchanged` → **`.env` 裡的值其實沒變**（常見原因：改到 `.env.example` 而不是 `.env`、`.env` 裡同一個變數重複定義兩次、或跑的目錄不是預期那份 repo checkout），要先回頭確認 `.env` 內容才有意義

**Secret 更新後 Pod 不會自動重讀環境變數**（K8s env 是 Pod 建立當下靜態注入的），一定要額外重啟：

```bash
kubectl rollout restart deployment/admin-api -n ai-platform
kubectl exec -n ai-platform deploy/admin-api -- printenv WEBHOOK_SECRET   # 確認新值真的生效
```

---

## 六、測試驗證

依序驗證，出問題時可以精準定位在哪一段：

### 6.1 Keycloak 端：Provider 是否正確載入

```bash
kubectl logs -n <namespace> deploy/<keycloak-deployment> --tail=200 | grep -i user-sync
```

應該看到啟動時印出：

```
[user-sync-listener] webhookUrls=[http://admin-api-service.ai-platform.svc.cluster.local:8080/api/v1/sync/keycloak]
```

沒有這行，代表 JAR 沒進容器（K8s 環境可先確認 `kubectl exec ... -- ls -la /opt/keycloak/providers/` 裡有沒有這個 jar）或 build 失敗。

### 6.2 取得 Token（確認 client credentials 正常）

```bash
curl -k -X POST \
  "https://125.228.83.116:49314/realms/FIRDI-AI-Platform/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=user-sync-service" \
  -d "client_secret=<YOUR_SECRET>"
```

### 6.3 手動觸發 sync endpoint（確認 admin-api 正常，跳過 Keycloak 這一段）

```bash
curl http://<admin-api位址>:30408/api/v1/sync/keycloak \
  -X POST \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "<keycloak-user-uuid>", "event_type": "UPDATE", "source": "admin_event"}'
```

期待回應：

```json
{"status": "updated", "user_id": "..."}
// 或新使用者：
{"status": "created", "user_id": "...", "api_key": "sk-..."}
```

### 6.4 端對端測試：Keycloak Admin Console 觸發

Admin Console 對某個使用者按 SAVE 的同時，另開一個視窗盯 Keycloak log：

```bash
kubectl logs -n <namespace> deploy/<keycloak-deployment> -f | grep -i user-sync
```

> ⚠️ **Plugin 送 webhook 失敗會被整個吞掉**（見 `UserSyncEventListenerProvider.sendWebhook()` 的 `catch (Exception e) { System.err.println(...) }`），**不會讓 Admin Console 的 SAVE 失敗**。UI 上一定顯示存檔成功，webhook 有沒有真的送達，唯一的真相只在這個 log 裡（成功不會有額外訊息；失敗會印 `Failed to send webhook. url=... error=...`）。

最後用 admin-api 確認資料真的更新了：

```bash
curl http://<admin-api位址>:30408/api/v1/users/<user_id> -H "Authorization: Bearer $ADMIN_API_KEY"
```

看 `updated_at` 是不是變成剛剛操作的時間點。

---

## 七、疑難排解

| 現象 | 原因 | 處理 |
|------|------|------|
| curl 測 `/api/v1/sync/keycloak` 回 `{"detail":"Invalid webhook secret"}`（401） | admin-api 實際吃到的 `WEBHOOK_SECRET` 跟送出的 `X-Webhook-Secret` 不一致 | `kubectl exec -n ai-platform deploy/admin-api -- printenv WEBHOOK_SECRET` 看目前值；若跟 `.env` 對不上，見第五節（可能是 `.env` 沒真的改到、或 Secret 更新後沒重啟 Pod） |
| curl 通過 webhook secret 驗證後回 `Internal Server Error`（純文字，不是 JSON），log 顯示 `httpx.ConnectError: ... Name or service not known` | `KEYCLOAK_URL` 填的是叢集內部 DNS 解不到的位址（通常是對外的公開網域） | 改成叢集內部 Service DNS（同 namespace 用 `<svc>:<port>`，跨 namespace 用 `<svc>.<namespace>.svc.cluster.local:<port>`），見第四節 |
| Admin Console 按 SAVE 看起來成功，但 admin-api 資料沒更新 | webhook 失敗被 plugin 靜默吞掉，UI 不會顯示錯誤 | 照第 6.1、6.4 節查 Keycloak log：先確認 JAR 有載入、`webhookUrls` 印出的值是否正確可達，再確認 Admin Console 的 Event Listeners / Admin events Save events 兩個開關（見第二節） |
| `kubectl apply` 報 `strict decoding error: unknown field "spec.template.spec.containers[0].volumes"` | `volumes:` 縮排錯誤，被塞進 `containers[0]` 底下而不是 pod spec 層級 | 確認 `volumes:` 縮排跟 `containers:` 同一層，見 3.2 節範例 |
| `./scripts/deploy.sh secrets` 印出 `secret/admin-api-secrets unchanged` | `.env` 裡的值其實沒變（改錯檔案、重複行、或目錄不對） | 見第五節 |

---

## 注意事項

- 使用者必須至少屬於一個 Keycloak 群組才會同步（否則回傳 `skipped`）；dept_id 對應的部門不存在時會自動建立
- Keycloak Group path level 1 會對應到 `dept_id`，例如 `/Engineering/Backend` → `dept_id = Engineering`
- `api_key` 在使用者首次建立時自動產生，之後更新不會改變
- DELETE 事件只做 `blocked=true`（軟刪除），不移除資料
