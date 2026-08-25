"""部門管理入口（admin-web）。

登入沿用 routers/me_web.py 同一套 Authorization Code flow（keycloak.py 的
build_authorize_url / exchange_code_for_token / fetch_userinfo /
build_logout_url），但認證邏輯換成 admin_auth.py 的白名單檢查而不是
routers/me.py 的 auth DB 查詢，且 cookie 名稱（admin_session / admin_oauth_state /
admin_id_token）刻意跟 me_web 的分開（見 docs/admin-web-plan.md「容易做錯的五件事」#3）。

所有頁面（含 routers/admin_web_write.py 的寫入表單）一律呼叫 services/ 底下不帶
認證的函式（decision C／R-13）：網頁層絕不用 ADMIN_API_KEY 打自己的 API。

模型授權（allowed_models）依 decision D 一律唯讀，權威來源是 OpenWebUI，這裡的
模型清單／同步診斷都只讀不寫；可寫的三件事（上架/下架模型、部門 provider key、
立即同步）在 routers/admin_web_write.py，跟這裡的唯讀頁分開放，共用同一個
PREFIX、同一套 _page/_nav/_mask_key 版面元件。
"""
import html
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from admin_auth import (
    CALLBACK_PATH,
    ID_TOKEN_COOKIE,
    LOGIN_PATH,
    SESSION_COOKIE,
    STATE_COOKIE,
    is_admin_username,
    require_admin,
)
from keycloak import (
    COOKIE_SECURE,
    build_authorize_url,
    build_logout_url,
    exchange_code_for_token,
    fetch_userinfo,
    redirect_uri_for,
)
from services import departments_service, models_service, openwebui_sync_service

PREFIX = "/api/v1/admin/web"

router = APIRouter(prefix=PREFIX)

_SESSION_MAX_AGE_CAP = 900

_NAV_ITEMS = [
    ("overview", "", "總覽"),
    ("models", "/models", "模型清單"),
    ("models_new", "/models/new", "上架模型"),
    ("keys", "/keys", "Provider Key"),
    ("sync", "/sync", "同步與診斷"),
]

# R-37：同步按鈕節流，同一使用者 30 秒內只允許一次；單一 process 記憶體內狀態即可
# （跟 config/custom_auth.py 的部門 rate limit 一樣，僅適用單 replica 部署）。
SYNC_THROTTLE_SECONDS = 30
_last_sync_monotonic: dict[str, float] = {}
_last_sync_wallclock: dict[str, str] = {}


def sync_throttle_remaining(username: str) -> float:
    """回傳距離下次可同步還要等幾秒；0 表示現在可以同步。"""
    last = _last_sync_monotonic.get(username)
    if last is None:
        return 0.0
    remaining = SYNC_THROTTLE_SECONDS - (time.monotonic() - last)
    return max(0.0, remaining)


def record_sync(username: str, wallclock: str) -> None:
    _last_sync_monotonic[username] = time.monotonic()
    _last_sync_wallclock[username] = wallclock


def last_sync_display(username: str) -> str:
    return _last_sync_wallclock.get(username) or "（本次啟動後尚未同步過）"


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>部門管理入口</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 3rem auto; padding: 0 1rem; color: #1a1a1a; }}
  .err {{ color: #b00020; }}
  .hint {{ color: #555; }}
  table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; }}
  td, th {{ padding: 0.3em 0.8em 0.3em 0; vertical-align: top; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ color: #555; font-weight: 600; }}
  a.btn {{ display: inline-block; margin-top: 1rem; }}
  .nav {{ color: #555; }}
  .nav b {{ color: #1a1a1a; }}
</style></head>
<body>{body}</body></html>"""
    resp = HTMLResponse(html_doc, status_code=status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _nav(active: str) -> str:
    links = " · ".join(
        f"<b>{label}</b>" if key == active else f'<a href="{PREFIX}{path}">{label}</a>'
        for key, path, label in _NAV_ITEMS
    )
    return f'<p class="nav">{links} · <a href="{PREFIX}/logout">登出</a></p>'


def _mask_key(key: str) -> str:
    """R-34：key 一律遮罩，只顯示末四碼，且絕不放進 HTML 的 value 屬性（這裡只當純文字顯示）。"""
    if not key:
        return "（未設定）"
    return f"...{key[-4:]}" if len(key) > 4 else "••••"


def render_error_page(exc: HTTPException) -> HTMLResponse:
    """把 admin-web 路徑底下冒出的任何 HTTPException 轉成看得懂的 HTML 頁面（R-43）。

    require_admin 的 401/403、LiteLLM／OpenWebUI／Keycloak 連線失敗的 502/500，
    都不該以原始 JSON 冒出去——這些頁面是給不熟終端機的人看的，跟 me_web 的原則一樣。
    由 main.py 的 exception handler 呼叫，只套用在 PREFIX 底下的路徑。
    """
    status = exc.status_code
    detail = html.escape(str(exc.detail))
    if status == 401:
        heading = "尚未登入"
        cta = f'<a class="btn" href="{LOGIN_PATH}">登入</a>'
    elif status == 403:
        heading = "沒有管理權限"
        cta = (
            f'<a class="btn" href="{LOGIN_PATH}">重新登入</a>'
            '<p>如果你要查詢／重設自己的 API Key，請改用'
            ' <a href="/api/v1/me/web/login">一般使用者自助頁</a>。</p>'
        )
    else:
        heading = "發生錯誤"
        cta = f'<a class="btn" href="{LOGIN_PATH}">回登入頁</a>'
    return _page(f"<h2>{heading}</h2><p class=\"err\">{detail}</p>{cta}", status_code=status)


@router.get("/login")
def login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(state, redirect_uri_for(CALLBACK_PATH))
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE, state, max_age=300, httponly=True, samesite="lax", secure=COOKIE_SECURE
    )
    return resp


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return _page(
            f'<h2>登入失敗</h2><p class="err">Keycloak 回報：{html.escape(error)}</p>'
            f'<a class="btn" href="{LOGIN_PATH}">重新登入</a>',
            status_code=401,
        )

    expected_state = request.cookies.get(STATE_COOKIE, "")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return _page(
            '<h2>登入失敗</h2><p class="err">驗證碼過期或不相符，請重新登入'
            '（這個連結可能已經用過，或開太久才點）。</p>'
            f'<a class="btn" href="{LOGIN_PATH}">重新登入</a>',
            status_code=400,
        )
    if not code:
        return _page(
            '<h2>登入失敗</h2><p class="err">缺少 authorization code。</p>'
            f'<a class="btn" href="{LOGIN_PATH}">重新登入</a>',
            status_code=400,
        )

    try:
        token_resp = await exchange_code_for_token(code, redirect_uri_for(CALLBACK_PATH))
        access_token = token_resp["access_token"]
        claims = await fetch_userinfo(access_token)
    except HTTPException as exc:
        body = _page(
            f'<h2>登入失敗</h2><p class="err">{html.escape(str(exc.detail))}</p>'
            f'<a class="btn" href="{LOGIN_PATH}">重新登入</a>',
            status_code=exc.status_code if exc.status_code < 500 else 502,
        )
        body.delete_cookie(STATE_COOKIE)
        return body

    username = (claims.get("preferred_username") or "").strip()
    if not is_admin_username(username):
        body = _page(
            "<h2>沒有管理權限</h2>"
            f"<p>Keycloak 帳號 <code>{html.escape(username or claims.get('sub', ''))}</code> "
            "不在管理白名單內，無法使用部門管理入口。如果你認為這是錯誤，請聯絡平台管理員。</p>"
            '<p>如果你要查詢／重設自己的 API Key，請改用'
            ' <a href="/api/v1/me/web/login">一般使用者自助頁</a>。</p>',
            status_code=403,
        )
        body.delete_cookie(STATE_COOKIE)
        return body

    resp = RedirectResponse(PREFIX, status_code=302)
    resp.delete_cookie(STATE_COOKIE)
    max_age = min(int(token_resp.get("expires_in", 300)), _SESSION_MAX_AGE_CAP)
    resp.set_cookie(
        SESSION_COOKIE, access_token, max_age=max_age,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    id_token = token_resp.get("id_token")
    if id_token:
        resp.set_cookie(
            ID_TOKEN_COOKIE, id_token, max_age=max_age,
            httponly=True, samesite="lax", secure=COOKIE_SECURE,
        )
    return resp


@router.get("")
def overview(admin: dict = Depends(require_admin)):
    """總覽：身分、可管理範圍、各部門 provider key 是否已設定、待處理提示。"""
    depts = departments_service.list_departments()

    pending = [d["dept_id"] for d in depts if not d["openrouter_api_key"]]
    rows = "".join(
        f"<tr><td>{html.escape(d['dept_id'])}</td><td>{html.escape(d['dept_name'])}</td>"
        f"<td>{len(d['allowed_models'])} 個{'（含 *）' if '*' in d['allowed_models'] else ''}</td>"
        f"<td>{html.escape(_mask_key(d['openrouter_api_key']))}</td></tr>"
        for d in depts
    )
    pending_html = (
        f"<p>尚未設定 OpenRouter key 的部門：{html.escape(', '.join(pending))}</p>"
        if pending else "<p>所有部門都已設定 OpenRouter key。</p>"
    )

    return _page(f"""
{_nav('overview')}
<h2>你好，{html.escape(admin['preferred_username'])}</h2>
<table>
  <tr><td>Email</td><td>{html.escape(admin.get('email') or '')}</td></tr>
  <tr><td>可管理範圍</td><td>全部部門（共 {len(depts)} 個）</td></tr>
</table>
<h3>部門總覽</h3>
<table>
  <tr><th>部門</th><th>名稱</th><th>已授權模型</th><th>OpenRouter Key</th></tr>
  {rows or '<tr><td colspan="4">目前沒有部門。</td></tr>'}
</table>
<h3>待處理</h3>
{pending_html}
""")


@router.get("/models")
async def model_list(admin: dict = Depends(require_admin)):
    """模型清單：DB-managed 模型的名稱、上游、key 來源（決策 E 的明確政策欄位）、已授權部門。第一期唯讀。"""
    external = await models_service.list_external_models()
    depts = departments_service.list_departments()

    dept_by_model: dict[str, list[str]] = {}
    wildcard_depts: list[str] = []
    for d in depts:
        allowed = d["allowed_models"]
        if "*" in allowed:
            wildcard_depts.append(d["dept_id"])
            continue
        for m in allowed:
            dept_by_model.setdefault(m, []).append(d["dept_id"])

    rows = []
    for m in external["models"]:
        name = m["model_name"] or ""
        # 決策 E 已落地：key 來源是 model_key_policies 的明確政策欄位，
        # openrouter/ 前綴現在只是命名慣例，不再是判斷依據。
        policy = m.get("key_policy", "model")
        key_source = (
            f"部門 {policy.split(':', 1)[1]} key（{policy}）"
            if policy.startswith("dept:") else "模型自帶 key"
        )
        authorized = sorted(set(dept_by_model.get(name, [])) | set(wildcard_depts))
        authorized_display = ", ".join(authorized) if authorized else "（尚無部門授權）"
        delete_form = f"""<form method="post" action="{PREFIX}/models/{html.escape(m['id'] or '')}/delete"
      onsubmit="return confirm('確定要下架 {html.escape(name)} 嗎？所有部門會立即打不通這個模型，且無法復原。');">
  <button type="submit">下架</button>
</form>"""
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(m['model'] or '')}</td>"
            f"<td>{html.escape(key_source)}</td><td>{html.escape(authorized_display)}</td>"
            f"<td>{delete_form}</td></tr>"
        )

    return _page(f"""
{_nav('models')}
<h2>模型清單</h2>
<p class="hint">第一期唯讀：模型授權（哪些部門能用）一律在 OpenWebUI 設定。
上架/下架在這裡做——<a href="{PREFIX}/models/new">上架新模型</a>。</p>
<table>
  <tr><th>名稱</th><th>上游</th><th>Key 來源</th><th>已授權部門</th><th></th></tr>
  {''.join(rows) if rows else '<tr><td colspan="5">目前沒有 DB-managed 模型。</td></tr>'}
</table>
""")


def render_sync_result(result: dict) -> str:
    """把 pull_openwebui_model_access 的回應轉成看得懂的區塊，dry-run 預覽與真正
    同步後的結果共用這份渲染邏輯，差別只在標題文案（呼叫端自己加）。
    """
    def _diff_rows(changes: list[dict], id_field: str) -> str:
        return "".join(
            f"<tr><td>{html.escape(c[id_field])}</td>"
            f"<td>{html.escape(', '.join(c['from']) or '（無）')}</td>"
            f"<td>{html.escape(', '.join(c['to']) or '（無）')}</td></tr>"
            for c in changes
        )

    dept_changes = _diff_rows(result["changed_departments"], "dept_id")
    user_changes = _diff_rows(result["changed_users"], "user_id")

    # R-38：把三種異常翻成人話，不能只印原始 JSON。
    issues = []
    if result["ignored_models"]:
        issues.append(
            "<li><b>模型 ID 對不上 LiteLLM</b>（最常見的故障，通常是拼字錯誤或模型已下架）："
            + html.escape(", ".join(result["ignored_models"])) + "</li>"
        )
    if result["unknown_departments"]:
        issues.append(
            "<li><b>OpenWebUI 群組對應的部門在 DB 尚不存在</b>："
            + html.escape(", ".join(result["unknown_departments"])) + "</li>"
        )
    if result["unknown_users"]:
        issues.append(
            "<li><b>OpenWebUI 使用者授權對應的身分在 DB 尚無帳號</b>："
            + html.escape(", ".join(result["unknown_users"])) + "</li>"
        )
    no_sso = sorted({g["model"] for g in result["skipped_grants"] if g["reason"] == "no_oidc_sub"})
    if no_sso:
        issues.append(
            "<li><b>有使用者授權，但該帳號還沒用 SSO 登入過 OpenWebUI</b>（拿不到 oidc.sub，"
            "暫時對映不到部門帳號），涉及模型：" + html.escape(", ".join(no_sso)) + "</li>"
        )
    unknown_group = sorted({g["model"] for g in result["skipped_grants"] if g["reason"] == "unknown_group"})
    if unknown_group:
        issues.append(
            "<li><b>有群組授權對不到任何已知部門</b>，涉及模型："
            + html.escape(", ".join(unknown_group)) + "</li>"
        )
    issues_html = f"<ul>{''.join(issues)}</ul>" if issues else "<p>沒有發現異常。</p>"

    return f"""
<h3>異常診斷</h3>
{issues_html}

<h3>部門權限的變化</h3>
<table>
  <tr><th>部門</th><th>之前</th><th>之後</th></tr>
  {dept_changes or '<tr><td colspan="3">沒有變化。</td></tr>'}
</table>

<h3>個人權限的變化</h3>
<table>
  <tr><th>使用者</th><th>之前</th><th>之後</th></tr>
  {user_changes or '<tr><td colspan="3">沒有變化。</td></tr>'}
</table>
"""


@router.get("/sync")
async def sync_diagnostics(admin: dict = Depends(require_admin)):
    """同步與診斷：dry-run 預覽「如果現在同步會變成怎樣」，不寫入任何資料；
    另附「立即同步」按鈕（真正寫入，R-35～R-37，處理邏輯在 admin_web_write.py）。
    """
    result = await openwebui_sync_service.pull_openwebui_model_access(dry_run=True)
    remaining = sync_throttle_remaining(admin["preferred_username"])
    sync_button = (
        f'<p class="hint">節流中，還要等 {int(remaining) + 1} 秒才能再次同步。</p>'
        if remaining > 0 else
        f"""<form method="post" action="{PREFIX}/sync"
      onsubmit="return confirm('確定要立即同步嗎？這會依 OpenWebUI 現況重算「全平台」所有部門的模型權限。');">
  <button type="submit">立即同步</button>
</form>"""
    )

    return _page(f"""
{_nav('sync')}
<h2>同步與診斷</h2>
<p class="hint">「立即同步」是 pull：依 OpenWebUI 現況重算全平台所有部門／使用者的模型權限
（第一期方向；OpenWebUI 目前仍是權威來源）。CronJob 已經每 2 分鐘自動跑一次，這顆按鈕只是
不想等的時候手動觸發，30 秒內只能按一次。上次同步時間：{html.escape(last_sync_display(admin['preferred_username']))}。</p>
{sync_button}

<p class="hint">以下是 dry-run 預覽：如果現在按下去會變成怎樣，<b>本頁載入過程沒有寫入任何資料</b>。</p>
{render_sync_result(result)}
""")


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    id_token_hint = request.cookies.get(ID_TOKEN_COOKIE)
    url = build_logout_url(LOGIN_PATH, id_token_hint=id_token_hint)
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(ID_TOKEN_COOKIE)
    return resp
