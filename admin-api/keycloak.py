"""Keycloak 連線設定與使用者 token 驗證。

由 routers/sync.py（webhook 走 client_credentials 查 Admin API）與 routers/me.py /
routers/me_web.py（自助端點驗使用者自己的 access token）共用，避免同一組環境變數在
多處各解析一次。
"""
import os

import httpx
from fastapi import HTTPException

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "").rstrip("/")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
# self-signed cert: 設 KEYCLOAK_SSL_VERIFY=false 停用驗證，或填 CA 憑證路徑
_ssl_verify_raw = os.getenv("KEYCLOAK_SSL_VERIFY", "true").strip().lower()
KEYCLOAK_SSL_VERIFY: bool | str = (
    False if _ssl_verify_raw == "false"
    else _ssl_verify_raw if _ssl_verify_raw not in ("true", "1", "yes")
    else True
)

# 自助端點網頁登入用的獨立 Client（Authorization Code flow）。
#
# 刻意不沿用上面 KEYCLOAK_CLIENT_ID（user-sync-service）：那是純 service-account
# 用的 confidential client（webhook 用 client_credentials 換 admin token），沒有、
# 也不該開放 Standard flow（瀏覽器登入導向）。混在一起會讓兩種完全不同的用途
# （自動化 webhook vs. 真人瀏覽器登入）共用同一個 client 設定，之後任何一邊要調整
# redirect URI 或權限都會怕動到另一邊。設定方式見 keycloak/SETUP.md。
KEYCLOAK_SELFSERVICE_CLIENT_ID = os.getenv("KEYCLOAK_SELFSERVICE_CLIENT_ID", "")
KEYCLOAK_SELFSERVICE_CLIENT_SECRET = os.getenv("KEYCLOAK_SELFSERVICE_CLIENT_SECRET", "")

# admin-api 本身「使用者瀏覽器看得到」的網址（例如反向代理位址或
# http://<node-ip>:30408）。Keycloak 登入完會把瀏覽器導回這個網址，跟叢集內部
# DNS（admin-api-service）是兩回事，不能互相取代。
ADMIN_API_PUBLIC_URL = os.getenv("ADMIN_API_PUBLIC_URL", "").rstrip("/")

# Keycloak「瀏覽器看到的」網址，只用在導向登入頁這一步（build_authorize_url）。
#
# 跟上面 KEYCLOAK_URL 分開的原因（2026-07-29 實測踩到）：Keycloak 有自己認定的
# 官方 hostname（realm 設定或 KC_HOSTNAME），跟我們拿來做 server-to-server 呼叫
# （webhook/token 交換/userinfo）的網址不一定是同一個——例如後端用 IP 打得通、但
# Keycloak 自己回應的登入表單 action 卻寫死指向另一個網域。如果讓瀏覽器先在 IP
# 這個 origin 收下 Keycloak 的登入態 cookie、送出表單時卻換到另一個 origin，
# cookie 對不上，Keycloak 會報「Restart login cookie not found」。
#
# 沒設定時預設等於 KEYCLOAK_URL（單一 hostname 的簡單部署不用多設這個變數）；
# 只有「後端連線位址」跟「Keycloak 官方 hostname」不同的環境才需要另外指定。
KEYCLOAK_BROWSER_URL = os.getenv("KEYCLOAK_BROWSER_URL", "").rstrip("/") or KEYCLOAK_URL

# admin-web 的 session cookie 存的也是 access token 本身，理當加 Secure；但三環境
# 目前都是 NodePort 純 HTTP（ADMIN_API_PUBLIC_URL 開頭是 http://），Secure cookie
# 在純 HTTP origin 下瀏覽器根本不會存也不會送，寫死 True 會直接讓登入壞掉。改成跟著
# ADMIN_API_PUBLIC_URL 的 scheme 走：之後這個值換成 https:// 就自動補上，不用兩邊改。
COOKIE_SECURE = ADMIN_API_PUBLIC_URL.startswith("https://")


def redirect_uri_for(callback_path: str) -> str:
    """把 callback path 接上 ADMIN_API_PUBLIC_URL，組成 Keycloak 要求的完整 redirect_uri。

    每個瀏覽器登入流程（me_web、admin_web……）有各自的 callback 路徑，且同一次
    登入裡 build_authorize_url／exchange_code_for_token 兩步用的 redirect_uri
    Keycloak 要求逐字相同，所以由呼叫端的路由模組決定 callback_path，這裡不寫死。
    """
    return f"{ADMIN_API_PUBLIC_URL}{callback_path}"


def build_authorize_url(state: str, redirect_uri: str) -> str:
    """回傳導向 Keycloak 登入畫面的網址（Authorization Code flow 第一步）。

    刻意用 KEYCLOAK_BROWSER_URL（瀏覽器可見的官方 hostname）而非 KEYCLOAK_URL
    （後端連線用），理由見上方 KEYCLOAK_BROWSER_URL 的註解。
    """
    if not (KEYCLOAK_BROWSER_URL and KEYCLOAK_REALM and KEYCLOAK_SELFSERVICE_CLIENT_ID and ADMIN_API_PUBLIC_URL):
        raise HTTPException(
            status_code=500,
            detail=(
                "Self-service web login is not configured: need KEYCLOAK_URL, "
                "KEYCLOAK_REALM, KEYCLOAK_SELFSERVICE_CLIENT_ID, ADMIN_API_PUBLIC_URL"
            ),
        )
    params = httpx.QueryParams({
        "response_type": "code",
        "client_id": KEYCLOAK_SELFSERVICE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
    })
    return f"{KEYCLOAK_BROWSER_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth?{params}"


def build_logout_url(post_logout_path: str, id_token_hint: str | None = None) -> str:
    """回傳登出 Keycloak SSO session 的網址（RP-Initiated Logout）。

    跟 build_authorize_url 一樣要用 KEYCLOAK_BROWSER_URL：這也是一個瀏覽器會
    導向的頁面，一樣得跟登入時同一個 origin，否則 Keycloak 自己的登出流程也可能
    重演同樣的 cookie 對不上問題。

    帶 id_token_hint 時 Keycloak 會直接登出、跳過「確定要登出嗎」的確認頁——沒有
    這個 hint，使用者體驗上「登出並切換使用者」會多一次不必要的手動確認點擊。
    """
    params = {
        "post_logout_redirect_uri": f"{ADMIN_API_PUBLIC_URL}{post_logout_path}",
        "client_id": KEYCLOAK_SELFSERVICE_CLIENT_ID,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{KEYCLOAK_BROWSER_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/logout?{httpx.QueryParams(params)}"


async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    """Authorization Code → token response（含 access_token / expires_in）。

    redirect_uri 必須跟該次 build_authorize_url 用的逐字相同，Keycloak 會比對，
    不同就換 token 失敗。
    """
    url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient(timeout=10, verify=KEYCLOAK_SSL_VERIFY) as client:
            resp = await client.post(url, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": KEYCLOAK_SELFSERVICE_CLIENT_ID,
                "client_secret": KEYCLOAK_SELFSERVICE_CLIENT_SECRET,
            })
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach Keycloak: {exc}") from exc

    if resp.status_code != 200:
        # 常見原因：code 過期/已用過（通常一分鐘內、且只能用一次），或 redirect_uri
        # 跟 Keycloak client 設定的 Valid redirect URIs 不完全相符。
        raise HTTPException(
            status_code=401, detail="Keycloak login failed or authorization code expired"
        )
    return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    """驗證使用者的 Keycloak access token 並取回 claims（含 sub / email）。

    刻意用 userinfo 端點而不是自己驗 JWT 簽章：由 Keycloak 自己判斷 token 是否有效，
    連「已登出/已撤銷」的 token 也一併擋掉（純驗簽章的話，被撤銷但還沒過期的 token
    仍會通過）。代價是每次多一次網路往返，對自助端點這種低頻用途無所謂，而且省下
    JWKS 快取與簽章驗證的相依套件。
    """
    if not KEYCLOAK_URL or not KEYCLOAK_REALM:
        raise HTTPException(status_code=500, detail="Keycloak is not configured")

    url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/userinfo"
    try:
        async with httpx.AsyncClient(timeout=10, verify=KEYCLOAK_SSL_VERIFY) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach Keycloak: {exc}") from exc

    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Invalid or expired Keycloak token")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Keycloak userinfo request failed")

    return resp.json()
