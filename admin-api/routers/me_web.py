"""自助端點的瀏覽器登入頁面（Authorization Code flow）。

給不方便自己操作 curl / Keycloak token 交換的一般使用者：點連結 → 走 Keycloak
熟悉的登入畫面（跟登入 OpenWebUI 是同一套帳號）→ 登入完自動跳回這裡、直接在網頁上
看到自己的 API key。全程不需要終端機、不需要自己組 HTTP 請求。

跟 routers/me.py（JSON API，吃 Authorization: Bearer <token>）是同一份底層邏輯
（resolve_db_user / to_me_out / regenerate_key_for），只是這裡换成 cookie-based 的
瀏覽器流程。技術用途的腳本/工具請走 me.py 那組 JSON 端點。
"""
import html
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from database import DB_PATH, get_conn
from keycloak import (
    build_authorize_url,
    build_logout_url,
    exchange_code_for_token,
    fetch_userinfo,
    redirect_uri_for,
)
from routers.me import regenerate_key_for, resolve_db_user, to_me_out

router = APIRouter(prefix="/api/v1/me/web")

_CALLBACK_PATH = "/api/v1/me/web/callback"
_STATE_COOKIE = "me_oauth_state"
_SESSION_COOKIE = "me_session"
# 跟 me_session 一起設、一起清；只用來登出時當 id_token_hint 帶給 Keycloak，
# 讓登出可以跳過「確定要登出嗎」的確認頁。不作其他任何用途（身分驗證仍然是
# me_session 那把 access token 每次重新問 Keycloak）。
_ID_TOKEN_COOKIE = "me_id_token"
# session cookie 存的就是 Keycloak access token 本身；每次使用都会重新打
# fetch_userinfo() 驗證，cookie 只是「不用使用者再登入一次」的便利性，不是信任來源。
# 上限存粹是防禦性設計（避免某些 realm 把 access token 效期設得很長時，cookie 也跟著
# 存活很久）：實際過期時間仍以 Keycloak 回的 expires_in 與此上限取其小。
_SESSION_MAX_AGE_CAP = 900


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>我的 API Key</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 3rem auto; padding: 0 1rem; color: #1a1a1a; }}
  code {{ background: #f0f0f0; padding: 0.15em 0.4em; border-radius: 4px; word-break: break-all; }}
  .key {{ font-size: 1.1rem; }}
  .err {{ color: #b00020; }}
  table {{ border-collapse: collapse; margin: 1rem 0; }}
  td {{ padding: 0.3em 0.8em 0.3em 0; vertical-align: top; }}
  td:first-child {{ color: #555; white-space: nowrap; }}
  form {{ margin-top: 1.5rem; }}
  button {{ font-size: 1rem; padding: 0.5em 1.2em; cursor: pointer; }}
  a.btn {{ display: inline-block; margin-top: 1rem; }}
</style></head>
<body>{body}</body></html>"""
    resp = HTMLResponse(html_doc, status_code=status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/login")
def login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    # 未設定 KEYCLOAK_SELFSERVICE_* 時這裡會拋 500
    url = build_authorize_url(state, redirect_uri_for(_CALLBACK_PATH))
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE, state, max_age=300, httponly=True, samesite="lax"
    )
    return resp


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _page(
            f'<h2>登入失敗</h2><p class="err">Keycloak 回報：{html.escape(error)}</p>'
            f'<a class="btn" href="/api/v1/me/web/login">重新登入</a>',
            status_code=401,
        )

    expected_state = request.cookies.get(_STATE_COOKIE, "")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return _page(
            '<h2>登入失敗</h2><p class="err">驗證碼過期或不相符，請重新登入'
            '（這個連結可能已經用過，或開太久才點）。</p>'
            '<a class="btn" href="/api/v1/me/web/login">重新登入</a>',
            status_code=400,
        )
    if not code:
        return _page(
            '<h2>登入失敗</h2><p class="err">缺少 authorization code。</p>'
            '<a class="btn" href="/api/v1/me/web/login">重新登入</a>',
            status_code=400,
        )

    # 把 token 交換 / userinfo 查詢的失敗也轉成一致的錯誤頁，而不是讓 HTTPException
    # 冒泡出去變成原始 JSON 錯誤——這個頁面本來就是設計給不熟終端機/API 的使用者看的，
    # 半路噴一段 JSON 會直接破壞整個「像網頁一樣好懂」的體驗。
    try:
        token_resp = await exchange_code_for_token(code, redirect_uri_for(_CALLBACK_PATH))
        access_token = token_resp["access_token"]
        claims = await fetch_userinfo(access_token)
    except HTTPException as exc:
        body = _page(
            f'<h2>登入失敗</h2><p class="err">{html.escape(str(exc.detail))}</p>'
            f'<a class="btn" href="/api/v1/me/web/login">重新登入</a>',
            status_code=exc.status_code if exc.status_code < 500 else 502,
        )
        body.delete_cookie(_STATE_COOKIE)
        return body

    record = resolve_db_user(claims)

    if record is None:
        body = _page(
            "<h2>查無帳號</h2>"
            f"<p>你的 Keycloak 身分（{html.escape(claims.get('email') or claims.get('sub', ''))}）"
            "在這個平台上還沒有對應的帳號，請聯絡管理員。</p>",
            status_code=404,
        )
        body.delete_cookie(_STATE_COOKIE)
        return body
    if record["blocked"]:
        body = _page("<h2>帳號已被停用</h2><p>請聯絡管理員。</p>", status_code=403)
        body.delete_cookie(_STATE_COOKIE)
        return body

    with get_conn(DB_PATH) as conn:
        me = to_me_out(conn, record)

    resp = _page(_key_page_body(me))
    resp.delete_cookie(_STATE_COOKIE)
    max_age = min(int(token_resp.get("expires_in", 300)), _SESSION_MAX_AGE_CAP)
    # SameSite=Lax：瀏覽器不會在「跨站的 POST」請求附上這顆 cookie，這樣下面
    # /regenerate-key 這個 POST 端點就不需要另外做 CSRF token 也能擋掉惡意網站
    # 誘導使用者瀏覽器發出的偽造請求。
    resp.set_cookie(
        _SESSION_COOKIE, access_token, max_age=max_age, httponly=True, samesite="lax"
    )
    id_token = token_resp.get("id_token")
    if id_token:
        resp.set_cookie(
            _ID_TOKEN_COOKIE, id_token, max_age=max_age, httponly=True, samesite="lax"
        )
    return resp


@router.post("/regenerate-key")
async def regenerate(request: Request):
    access_token = request.cookies.get(_SESSION_COOKIE, "")
    if not access_token:
        return RedirectResponse("/api/v1/me/web/login", status_code=302)

    # cookie 只是免登入一次的便利性，實際有效性每次都重新問 Keycloak 一次，
    # 過期/已登出的 token 會在這裡被 fetch_userinfo 擋下來。
    claims = await fetch_userinfo(access_token)
    record = resolve_db_user(claims)
    if record is None or record["blocked"]:
        return RedirectResponse("/api/v1/me/web/login", status_code=302)

    with get_conn(DB_PATH) as conn:
        me = regenerate_key_for(conn, record["user_id"])

    return _page(_key_page_body(me, just_regenerated=True))


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    """結束 Keycloak 的 SSO session，讓下一次 /login 真的顯示登入表單（可換帳號）。

    只清我們自己這邊的 me_session cookie不夠：Keycloak 自己也有一份瀏覽器 SSO
    session，只要它還在，重新打 /login 會被無聲地用同一個帳號直接放行、根本看不到
    登入表單。要真的能「切換使用者」，得連 Keycloak 那邊的 session 也一起結束。
    """
    id_token_hint = request.cookies.get(_ID_TOKEN_COOKIE)
    url = build_logout_url("/api/v1/me/web/login", id_token_hint=id_token_hint)
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(_SESSION_COOKIE)
    resp.delete_cookie(_ID_TOKEN_COOKIE)
    return resp


def _key_page_body(me, just_regenerated: bool = False) -> str:
    notice = (
        '<p style="color:#0a7a2f">已重設，新的 key 如下（舊的已立即失效）。</p>'
        if just_regenerated else ""
    )
    models = ", ".join(me.allowed_models) if me.allowed_models else "(無)"
    return f"""
<h2>你好，{html.escape(me.key_name)}</h2>
{notice}
<table>
  <tr><td>部門</td><td>{html.escape(me.dept_id)}</td></tr>
  <tr><td>Email</td><td>{html.escape(me.user_email or '')}</td></tr>
  <tr><td>可用模型</td><td>{html.escape(models)}</td></tr>
  <tr><td>API Key</td><td class="key"><code>{html.escape(me.api_key)}</code></td></tr>
</table>
<p>把上面的 API Key 填進你的工具設定即可，詳細用法請洽管理員索取
<code>docs/api-access.md</code> 文件。</p>
<form method="post" action="/api/v1/me/web/regenerate-key"
      onsubmit="return confirm('確定要重設嗎？現在這把 key 會立即失效，所有已經填了它的工具都要更新。');">
  <button type="submit">重設我的 Key</button>
</form>
<p><a class="btn" href="/api/v1/me/web/logout">登出並切換使用者</a></p>
"""
