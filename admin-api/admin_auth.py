"""admin-web 網頁流程的身分驗證：Keycloak 登入 + 白名單，不查 auth DB。

跟 routers/me.py 的 current_user() 刻意分開：那邊驗證後要在 auth DB 的 users
表找出這個 Keycloak 身分對應的 LiteLLM api_key，admin-web 不能沿用同一套——
中央管理帳號（`firdiadm`）的 Keycloak group 是空的，`routers/sync.py` 的
keycloak_bulk_sync 明確跳過沒有群組的帳號，實測 DB 裡這個帳號 0 筆紀錄。沿用
resolve_db_user() 會讓管理帳號直接撞上「查無帳號」而登不進來。

admin-web 要驗證的只有兩件事：Keycloak token 有效、且 `preferred_username`
在白名單內（見 docs/admin-web-plan.md 決策 B——比對 preferred_username 而非
sub，因為三個環境的 firdiadm 是各自建立的、sub 不同，比 sub 會讓白名單設定沒辦法
跨環境共用）。可管理範圍固定是全部部門，不做任何部門層級的細分。
"""
import os

from fastapi import HTTPException, Request

from keycloak import fetch_userinfo

SESSION_COOKIE = "admin_session"
STATE_COOKIE = "admin_oauth_state"
ID_TOKEN_COOKIE = "admin_id_token"

LOGIN_PATH = "/api/v1/admin/web/login"
CALLBACK_PATH = "/api/v1/admin/web/callback"


def admin_whitelist() -> set[str]:
    raw = os.getenv("ADMIN_WEB_USERNAMES", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def is_admin_username(preferred_username: str) -> bool:
    return bool(preferred_username) and preferred_username in admin_whitelist()


async def require_admin(request: Request) -> dict:
    """Session cookie → Keycloak 身分 → 白名單檢查，給受保護的 admin-web 頁面當 dependency。

    401：沒有 session（沒登入過，或 cookie 已過期）。
    403：Keycloak 身分驗證通過，但 preferred_username 不在白名單——這是「登入成功
    但沒有管理權限」，跟未登入是不同狀況，故意分開兩種狀態碼。
    """
    access_token = request.cookies.get(SESSION_COOKIE, "")
    if not access_token:
        raise HTTPException(status_code=401, detail="Not logged in")

    claims = await fetch_userinfo(access_token)
    username = (claims.get("preferred_username") or "").strip()
    if not is_admin_username(username):
        raise HTTPException(
            status_code=403,
            detail=f"'{username}' is not an authorized admin account",
        )

    return {
        "preferred_username": username,
        "email": claims.get("email"),
        "sub": claims.get("sub"),
    }
