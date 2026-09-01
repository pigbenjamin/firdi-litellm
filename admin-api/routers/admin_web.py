"""部門管理入口（admin-web）。

登入沿用 routers/me_web.py 同一套 Authorization Code flow（keycloak.py 的
build_authorize_url / exchange_code_for_token / fetch_userinfo /
build_logout_url），但認證邏輯換成 admin_auth.py 的白名單檢查而不是
routers/me.py 的 auth DB 查詢，且 cookie 名稱（admin_session / admin_oauth_state /
admin_id_token）刻意跟 me_web 的分開（見 docs/admin-web-plan.md「容易做錯的五件事」#3）。

所有頁面（含 routers/admin_web_write.py 的寫入表單）一律呼叫 services/ 底下不帶
認證的函式（decision C／R-13）：網頁層絕不用 ADMIN_API_KEY 打自己的 API。

檔案分工（全部共用同一個 PREFIX 與 _page/_nav/_mask_key 版面元件）：
  - 這裡：登入流程 + 唯讀頁（總覽、模型清單／詳情、同步診斷、稽核紀錄查詢）。
  - routers/admin_web_write.py：模型生命週期與 provider key 的寫入操作。
  - routers/admin_web_access.py：模型授權矩陣（WP4）。

**decision D 的「模型授權一律唯讀」已於第二期翻轉**（見 docs/admin-web-plan.md
「第二期：翻轉授權權威來源」）。當時唯讀的理由是「寫 DB 會在下次 pull 被
OpenWebUI 覆寫」；第二期的解法不是關掉 pull，而是寫完立刻 push 回 OpenWebUI，
讓兩邊狀態一致，之後不管什麼時候 pull 結果都一樣。細節見
services/model_access_service.py。
"""
import html
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
import audit
from services import (
    departments_service,
    model_metadata_service,
    models_service,
    openwebui_sync_service,
)

PREFIX = "/api/v1/admin/web"

router = APIRouter(prefix=PREFIX)

_SESSION_MAX_AGE_CAP = 900

_NAV_ITEMS = [
    ("overview", "", "總覽"),
    ("models", "/models", "模型清單"),
    ("models_new", "/models/new", "上架模型"),
    ("access", "/access", "模型授權"),
    ("keys", "/keys", "Provider Key"),
    ("sync", "/sync", "同步與診斷"),
    ("audit", "/audit", "稽核紀錄"),
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
  body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 3rem auto; padding: 0 1rem; color: #1a1a1a; }}
  .err {{ color: #b00020; }}
  .hint {{ color: #555; }}
  table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; }}
  td, th {{ padding: 0.3em 0.8em 0.3em 0; vertical-align: top; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ color: #555; font-weight: 600; }}
  a.btn {{ display: inline-block; margin-top: 1rem; }}
  .nav {{ color: #555; }}
  .nav b {{ color: #1a1a1a; }}
  /* 授權矩陣、稽核紀錄這類寬表格自己橫向捲動，不讓整頁被撐開 */
  .wide {{ overflow-x: auto; }}
  .badge {{ display: inline-block; padding: 0.1em 0.5em; border-radius: 0.8em; font-size: 0.85em; white-space: nowrap; }}
  .badge-draft {{ background: #fff3cd; color: #7a5b00; }}
  .badge-published {{ background: #d7f0dd; color: #14622c; }}
  .badge-disabled {{ background: #eee; color: #555; }}
  .badge-legacy {{ background: #e7edff; color: #2a3f77; }}
  .ok {{ color: #14622c; }}
  .warn {{ color: #7a5b00; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 4px; margin: 1.2rem 0; padding: 0.6rem 1rem 1rem; }}
  legend {{ color: #555; font-weight: 600; padding: 0 0.4em; }}
  label {{ display: inline-block; }}
  input[type=text], input[type=number], input[type=password], textarea, select {{ min-width: 22rem; max-width: 100%; }}
  textarea {{ height: 4rem; }}
  .diff-add {{ color: #14622c; }}
  .diff-del {{ color: #b00020; }}
  /* 授權編輯頁：動過的那一列立刻上色，讓「我改了什麼」不用靠記憶 */
  tr.row-add > td {{ background: #eaf7ee; }}
  tr.row-del > td {{ background: #fdecef; }}
  .mark {{ font-size: 0.9em; white-space: nowrap; }}
  tr.row-add .mark {{ color: #14622c; }}
  tr.row-del .mark {{ color: #b00020; }}
  /* 授權總覽的模型清單：一眼看出「這個部門現在有什麼」 */
  .chip {{
    display: inline-block; margin: 0 0.3em 0.3em 0; padding: 0.1em 0.6em;
    border-radius: 0.8em; background: #eef1f6; font-size: 0.85em; white-space: nowrap;
  }}
  .chip-stale {{ background: #fdecef; color: #b00020; }}
  /* 唯讀總覽矩陣：橫向捲到右邊時表頭與部門名要還在視線內 */
  .matrix th {{ position: sticky; top: 0; background: #fff; z-index: 2; }}
  .matrix td:first-child, .matrix th:first-child {{
    position: sticky; left: 0; background: #fff; z-index: 3;
  }}
  .matrix .on {{ text-align: center; color: #14622c; font-weight: 700; }}
  /* 編輯頁底部的變更摘要：捲到哪裡都看得到還沒送出的差異 */
  .sticky-bar {{
    position: sticky; bottom: 0; background: #fff; border-top: 1px solid #ddd;
    padding: 0.7rem 0; margin-top: 1rem;
  }}
  /* 稽核的變更內容：短的直接顯示，長的收進 details，完整內容一定打得開 */
  details.diff-add, details.diff-del, details.hint {{ margin: 0; }}
  details > summary {{ cursor: pointer; }}
  details[open] > summary {{ color: #555; }}
  pre.audit-full {{
    white-space: pre-wrap; word-break: break-all; margin: 0.3em 0 0.6em;
    padding: 0.5em 0.7em; background: #f6f7f9; border-radius: 4px;
    font-size: 0.85em; color: #1a1a1a;
  }}
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


# ── 模型頁的共用小元件 ────────────────────────────────────────────────────────

def status_badge(meta: dict) -> str:
    """狀態徽章。不受狀態機管理的模型標成「既有」而不是冒充成已發布——看到的人
    要知道差別。兩種情況：

      - YAML model_list 定義的地端模型：**永遠**是「既有」，即使已經被納管
        （有 model_metadata 紀錄）也一樣。它沒有草稿／發布／停用可言，改它要動
        config/litellm_config.yaml 並重啟 litellm pod。
      - 這個功能上線前就用 curl 上架、還沒補過設定的外部模型。
    """
    if meta.get("yaml_managed") or not meta.get("has_record"):
        return '<span class="badge badge-legacy">既有</span>'
    status = meta.get("status") or "published"
    label = model_metadata_service.STATUSES.get(status, status)
    return f'<span class="badge badge-{html.escape(status)}">{html.escape(label)}</span>'


def money(value) -> str:
    return "—" if value is None else f"${value:,.4f}"


def budget_cell(meta: dict, spend: dict) -> str:
    state = model_metadata_service.budget_state(meta, spend)
    used = money(state["used"])
    if state["limit"] is None:
        return f'{used} <span class="hint">／未設額度</span>'
    pct = f"{state['pct']:.0f}%"
    mode = "會擋" if state["enforced"] else "只記錄"
    cls = "err" if state["exceeded"] else ("warn" if (state["pct"] or 0) >= 80 else "")
    return (
        f'<span class="{cls}">{used} / {money(state["limit"])}（{pct}）</span>'
        f' <span class="hint">{mode}</span>'
    )


def test_cell(meta: dict) -> str:
    if not meta.get("has_record"):
        return "—"
    if meta.get("last_test_ok") is None:
        return '<span class="hint">未測試</span>'
    if meta["last_test_ok"] == 1:
        return f'<span class="ok">通過</span> <span class="hint">{html.escape((meta.get("last_test_at") or "")[:16])}</span>'
    return f'<span class="err">失敗</span> <span class="hint">{html.escape((meta.get("last_test_at") or "")[:16])}</span>'


def detail_url(model_name: str) -> str:
    return f"{PREFIX}/models/detail?model_name={quote(model_name, safe='')}"


def access_model_url(model_name: str) -> str:
    """「把這一個模型一次開給多個部門」那一頁。定義在這裡而不是 admin_web_access，
    是因為模型清單與詳情頁都要用它，而 admin_web_access 反過來 import 這個模組。
    """
    return f"{PREFIX}/access/model/edit?model_name={quote(model_name, safe='')}"


def key_source_display(policy: str) -> str:
    """model_key_policies 的政策字串翻成人話（決策 E 落地後 openrouter/ 前綴只是命名慣例）。"""
    if policy.startswith("dept:"):
        return f"部門 {policy.split(':', 1)[1]} key"
    return "模型自帶 key"


def authorized_departments(depts: list[dict]) -> tuple[dict[str, list[str]], list[str]]:
    """回傳 (model_name → 授權它的部門清單, 含 * 的部門清單)。"""
    by_model: dict[str, list[str]] = {}
    wildcard: list[str] = []
    for d in depts:
        allowed = d["allowed_models"]
        if "*" in allowed:
            wildcard.append(d["dept_id"])
            continue
        for m in allowed:
            by_model.setdefault(m, []).append(d["dept_id"])
    return by_model, wildcard


@router.get("/models")
async def model_list(admin: dict = Depends(require_admin)):
    """模型清單：狀態、類型、key 來源、用量／額度、測試結果、已授權部門。

    含停用中的模型（它們在 LiteLLM 裡已經不存在，只剩 model_metadata 那筆紀錄），
    否則停用之後就再也找不到地方按「重新啟用」。
    """
    external = await models_service.list_external_models(include_yaml=True)
    depts = departments_service.list_departments()
    dept_by_model, wildcard_depts = authorized_departments(depts)

    rows = []
    for m in external["models"]:
        name = m["model_name"] or ""
        meta = m["meta"]
        authorized = sorted(set(dept_by_model.get(name, [])) | set(wildcard_depts))
        display = meta.get("display_name") or ""
        name_cell = f'<a href="{detail_url(name)}"><code>{html.escape(name)}</code></a>'
        if display:
            name_cell += f'<br><span class="hint">{html.escape(display)}</span>'
        rows.append(
            f"<tr><td>{name_cell}</td>"
            f"<td>{status_badge(meta)}</td>"
            f"<td>{html.escape(model_metadata_service.MODEL_TYPES.get(meta.get('model_type') or 'chat', ''))}</td>"
            f"<td><code>{html.escape(m['model'] or '')}</code></td>"
            f"<td>{html.escape(key_source_display(m['key_policy']))}</td>"
            f"<td>{budget_cell(meta, m['spend'])}</td>"
            f"<td>{test_cell(meta)}</td>"
            f'<td>{html.escape(", ".join(authorized)) if authorized else "（尚無部門授權）"}'
            f'<br><a href="{access_model_url(name)}">改授權 »</a></td></tr>'
        )

    draft_count = sum(1 for m in external["models"] if m["meta"].get("status") == "draft")
    draft_hint = (
        f'<p class="warn">有 {draft_count} 個模型還在草稿狀態，使用者看不到也打不通，'
        "測試通過後才發布得出去。</p>" if draft_count else ""
    )

    return _page(f"""
{_nav('models')}
<h2>模型清單</h2>
<p class="hint">點模型名稱進去可以測試呼叫、發布、停用、修改欄位與刪除。
「已授權部門」那一欄的<b>改授權</b>可以把該模型一次開給多個部門；
以部門為主的檢視在 <a href="{PREFIX}/access">模型授權</a>。
標<span class="badge badge-legacy">既有</span>的是 YAML <code>model_list</code> 定義的地端模型，
上游設定在 <code>config/litellm_config.yaml</code>、改了要重啟 litellm pod，所以這裡不提供
停用／刪除／編輯上游，但顯示名稱、類型、成本歸屬、備註仍可設定。
<a href="{PREFIX}/models/new">上架新模型 »</a></p>
{draft_hint}
<div class="wide"><table>
  <tr><th>名稱</th><th>狀態</th><th>類型</th><th>上游</th><th>Key 來源</th>
      <th>本期用量／額度</th><th>測試</th><th>已授權部門</th></tr>
  {''.join(rows) if rows else '<tr><td colspan="8">目前沒有 DB-managed 模型。</td></tr>'}
</table></div>
""")


@router.get("/models/detail")
async def model_detail(
    model_name: str, admin: dict = Depends(require_admin), msg: str = "", level: str = "ok",
):
    """單一模型的詳情頁：所有欄位 + 生命週期操作 + 影響範圍。

    用 query string 而不是路徑參數傳 model_name——model_name 本身就含斜線
    （openrouter/anthropic/claude-sonnet-4-5），放進路徑會跟路由規則打架。
    """
    external = await models_service.list_external_models(include_yaml=True)
    entry = next((m for m in external["models"] if m["model_name"] == model_name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"找不到模型 '{model_name}'")

    meta, spend = entry["meta"], entry["spend"]
    status = meta.get("status") if meta.get("has_record") else "legacy"
    impact = models_service.model_impact(model_name)
    depts = departments_service.list_departments()
    enc = quote(model_name, safe="")

    # msg 來自 query string（使用者可控），一律 escape、絕不當 HTML 渲染。
    # 要醒目就靠 level 這個受控的旗標選 class，不是讓呼叫端塞標籤進來。
    banner_class = "err" if level == "warn" else "ok"
    banner = f'<p class="{banner_class}">{html.escape(msg)}</p>' if msg else ""

    state = model_metadata_service.budget_state(meta, spend)
    budget_line = (
        f"{money(state['used'])} / {money(state['limit'])}"
        f"（{model_metadata_service.BUDGET_PERIODS.get(state['period'], state['period'])}，"
        f"{'超額會擋下來' if state['enforced'] else '只記錄、不擋'}）"
        if state["limit"] is not None else f"{money(state['used'])}，未設額度"
    )

    test_result = meta.get("last_test_result") or ""
    test_block = (
        f'<pre class="hint" style="white-space:pre-wrap">{html.escape(test_result)}</pre>'
        if test_result else '<p class="hint">還沒測試過。</p>'
    )

    dept_names = ", ".join(f"{d['dept_id']}（{d['headcount']} 人）" for d in impact["departments"]) or "（無）"
    wildcard_names = ", ".join(
        f"{d['dept_id']}（{d['headcount']} 人）" for d in impact["wildcard_departments"]
    )
    wildcard_line = (
        f'<tr><td>不限制模型的部門</td><td>{html.escape(wildcard_names)}'
        '<br><span class="hint">這些部門的 allowed_models 含 *，沒有逐一列出這個模型但一樣打得到</span></td></tr>'
        if wildcard_names else ""
    )

    # ── 生命週期按鈕：每個狀態只給當下合法的動作，不給了再擋 ──
    # model_name 一律用 POST 表單欄位／query string 傳，不放進路徑——名稱本身含
    # 斜線（openrouter/anthropic/claude-sonnet-4-5），百分比編碼過的 %2F 會在
    # ASGI 伺服器解碼後變回真正的斜線，路徑參數就切錯段了。
    hidden = f'<input type="hidden" name="model_name" value="{html.escape(model_name)}">'
    yaml_managed = bool(meta.get("yaml_managed"))
    actions = []
    if yaml_managed:
        # 地端模型只給測試呼叫（確認 vLLM／Ollama 服務還活著很有用）。發布／停用／
        # 刪除都不適用——那些要改 config/litellm_config.yaml 並重啟 litellm pod。
        if meta.get("has_record"):
            actions.append(f"""<form method="post" action="{PREFIX}/models/test" style="display:inline">
  {hidden}<button type="submit">測試呼叫</button></form>""")
        else:
            actions.append('<span class="hint">先在下方存一次「可修改的欄位」建立管理紀錄，'
                           '就能測試呼叫、設額度、寫備註。</span>')
    elif status in ("draft", "published"):
        actions.append(f"""<form method="post" action="{PREFIX}/models/test" style="display:inline">
  {hidden}<button type="submit">測試呼叫</button></form>""")
    if not yaml_managed and status == "draft":
        gate = "" if meta.get("last_test_ok") == 1 else " disabled title='要先通過測試呼叫才能發布'"
        actions.append(f"""<form method="post" action="{PREFIX}/models/publish" style="display:inline"
  onsubmit="return confirm('確定發布？發布後使用者就打得到，且上游設定會鎖定。');">
  {hidden}<button type="submit"{gate}>發布</button></form>""")
    if not yaml_managed and status in ("draft", "published"):
        actions.append(f"""<form method="post" action="{PREFIX}/models/disable" style="display:inline"
  onsubmit="return confirm('確定停用？使用者會立刻打不通，但設定完整保留、可隨時重新啟用。');">
  {hidden}<button type="submit">停用</button></form>""")
    if not yaml_managed and status == "disabled":
        actions.append(f"""<form method="post" action="{PREFIX}/models/enable" style="display:inline">
  {hidden}<button type="submit">重新啟用</button></form>""")
    actions_html = " ".join(actions) or '<span class="hint">這個模型不受狀態機管理（見上方「既有」標記）。</span>'

    # ── 編輯表單：draft 可改全部；published 只能改描述性欄位 ──
    dept_options = "".join(
        f'<option value="{html.escape(d["dept_id"])}"'
        f'{" selected" if d["dept_id"] == meta.get("cost_center") else ""}>'
        f'{html.escape(d["dept_id"])}｜{html.escape(d["dept_name"])}</option>'
        for d in depts
    )
    budget_period_options = "".join(
        f'<option value="{k}"{" selected" if k == (meta.get("budget_period") or "monthly") else ""}>'
        f'{html.escape(v)}</option>'
        for k, v in model_metadata_service.BUDGET_PERIODS.items()
    )
    limit_value = "" if meta.get("budget_limit_usd") is None else f'{meta["budget_limit_usd"]:g}'
    type_field = ""
    if yaml_managed:
        # 地端模型沒有「草稿」可以編輯上游設定，但類型一定要設得對，測試呼叫才會
        # 用對的請求形狀（embeddinggemma 用 chat 的形狀去測只會拿到看不懂的 400）。
        opts = "".join(
            f'<option value="{k}"{" selected" if k == (meta.get("model_type") or "chat") else ""}>'
            f'{html.escape(v)}</option>'
            for k, v in model_metadata_service.MODEL_TYPES.items()
        )
        type_field = f'<p><label>模型類型<br><select name="model_type">{opts}</select></label></p>'

    descriptive_form = f"""
<form method="post" action="{PREFIX}/models/fields">
  {hidden}
  {type_field}
  <p><label>顯示名稱<br><input type="text" name="display_name"
     value="{html.escape(meta.get('display_name') or '')}"></label></p>
  <p><label>成本歸屬部門<br><select name="cost_center">
     <option value="">（不指定）</option>{dept_options}</select></label></p>
  <p><label>額度上限（USD，留空＝不設額度）<br>
     <input type="number" name="budget_limit_usd" step="0.01" min="0" value="{limit_value}"></label></p>
  <p><label>額度週期<br><select name="budget_period">{budget_period_options}</select></label></p>
  <p><label><input type="checkbox" name="budget_enforce" value="1"
     {"checked" if meta.get("budget_enforce") else ""}>
     超過額度就擋下來（不勾＝只累計用量、不影響呼叫）</label></p>
  <p><label>備註<br><textarea name="notes">{html.escape(meta.get('notes') or '')}</textarea></label></p>
  <button type="submit">儲存</button>
</form>"""

    # 反斜線不能出現在 f-string 的 {} 表達式裡（Python 3.11 限制，容器跑的就是
    # 3.11）。這段確認訊息有 \n，所以先算成變數，外層只插入變數名。
    delete_form = "" if yaml_managed else f"""<form method="post" action="{PREFIX}/models/hard-delete"
  onsubmit="return confirm('永久刪除 {html.escape(model_name)}？\n\n影響 {impact['total_headcount']} 人、{len(impact['departments']) + len(impact['wildcard_departments'])} 個部門。\n所有設定與額度紀錄都會消失，無法復原。\n\n只是想暫停的話請用「停用」。');">
  {hidden}
  <p><label><input type="checkbox" name="confirm" required>
     我確認要永久刪除，且知道這會影響上面列出的 {impact['total_headcount']} 個人</label></p>
  <button type="submit">永久刪除</button>
</form>"""
    yaml_delete_note = (
        '<p class="hint">地端模型不提供刪除——要下架請改 <code>config/litellm_config.yaml</code> '
        "的 model_list 並重啟 litellm pod。</p>" if yaml_managed else ""
    )

    routing_block = f"""
<fieldset><legend>上游設定{'（已鎖定）' if status != 'draft' else ''}</legend>
<table>
  <tr><td>litellm_params.model</td><td><code>{html.escape(meta.get('litellm_model') or entry['model'] or '')}</code></td></tr>
  <tr><td>api_base</td><td><code>{html.escape(meta.get('api_base') or entry['api_base'] or '（用上游預設端點）')}</code></td></tr>
  <tr><td>Key 來源</td><td>{html.escape(key_source_display(entry['key_policy']))}
      {f'（{html.escape(_mask_key(meta.get("api_key") or ""))}）' if meta.get('api_key') else ''}</td></tr>
  <tr><td>模型類型</td><td>{html.escape(model_metadata_service.MODEL_TYPES.get(meta.get('model_type') or 'chat', ''))}</td></tr>
</table>
{'<p class="hint">這是 YAML <code>model_list</code> 定義的地端模型，上游設定在 <code>config/litellm_config.yaml</code>——要改請改那個檔案並重啟 litellm pod，這個畫面不提供（也不提供停用與刪除）。</p>' if yaml_managed else ''}
{f'<p class="hint">要改上游設定請先「停用」→ 刪除 → 重新上架；已發布的模型改上游等於偷換成另一個服務。</p>' if not yaml_managed and status == 'published' else ''}
{f'<p><a href="{PREFIX}/models/edit?model_name={enc}">編輯上游設定 »</a>（草稿才能改；改完要重新測試）</p>' if not yaml_managed and status == 'draft' else ''}
</fieldset>"""

    return _page(f"""
{_nav('models')}
{banner}
<h2><code>{html.escape(model_name)}</code> {status_badge(meta)}</h2>
<p><a href="{PREFIX}/models">« 回模型清單</a></p>

<fieldset><legend>目前狀態</legend>
<table>
  <tr><td>顯示名稱</td><td>{html.escape(meta.get('display_name') or '（未設定）')}</td></tr>
  <tr><td>在 LiteLLM 註冊</td><td>{'是' if entry['registered'] else '否（停用中）'}</td></tr>
  <tr><td>本期用量／額度</td><td>{html.escape(budget_line)}</td></tr>
  <tr><td>累計用量</td><td>{money(spend['total'])}｜本月 {spend['calls']} 次呼叫</td></tr>
  <tr><td>備註</td><td>{html.escape(meta.get('notes') or '')}</td></tr>
</table>
<p>{actions_html}</p>
</fieldset>

<fieldset><legend>測試呼叫結果</legend>
{test_block}
</fieldset>

{routing_block}

<fieldset><legend>可修改的欄位</legend>
<p class="hint">這幾個欄位在任何狀態都能改——它們不影響請求打到哪裡去。</p>
{descriptive_form}
</fieldset>

<fieldset><legend>影響範圍</legend>
<table>
  <tr><td>已授權的部門</td><td>{html.escape(dept_names)}</td></tr>
  {wildcard_line}
  <tr><td>個別授權的使用者</td><td>{len(impact['users'])} 人</td></tr>
  <tr><td>總影響人數</td><td><b>{impact['total_headcount']}</b> 人</td></tr>
</table>
<p><a class="btn" href="{access_model_url(model_name)}">編輯這個模型的部門授權 »</a>
<span class="hint">（一次勾多個部門，存檔前會先給差異預覽）</span></p>
{yaml_delete_note}
{delete_form}
</fieldset>
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


# ── WP5：稽核紀錄查詢與匯出 ───────────────────────────────────────────────────

_AUDIT_PAGE_LIMIT = 500
_AUDIT_INLINE_CHARS = 160   # 超過這個長度就收進 <details>，不是直接砍掉


def _audit_value(text: str, cls: str) -> str:
    """稽核的變更內容常常很長（一個模型的完整欄位就將近 300 字元）。

    直接截斷是不行的：稽核頁的意義就是看得到「誰改了什麼」，砍掉一半又不講，
    等於讓人以為那段就是全部——實測過 api_key 的末四碼正好落在截斷點後面，
    畫面上完全看不到。所以長的收進可展開的 <details>，完整內容一定拿得到。
    """
    if not text:
        return ""
    safe = html.escape(text)
    if len(text) <= _AUDIT_INLINE_CHARS:
        return f'<div class="{cls}">{safe}</div>'
    return (
        f'<details class="{cls}"><summary>{html.escape(text[:_AUDIT_INLINE_CHARS])}…</summary>'
        f'<pre class="audit-full">{safe}</pre></details>'
    )


def _audit_filters(start: str, end: str, actor: str, action: str, target: str) -> str:
    action_options = "".join(
        f'<option value="{html.escape(k)}"{" selected" if k == action else ""}>{html.escape(v)}</option>'
        for k, v in audit.ACTIONS.items()
    )
    return f"""
<form method="get" action="{PREFIX}/audit">
  <p>
    <label>起（YYYY-MM-DD）<input type="date" name="start" value="{html.escape(start)}" style="min-width:auto"></label>
    <label>迄<input type="date" name="end" value="{html.escape(end)}" style="min-width:auto"></label>
  </p>
  <p>
    <label>操作者<input type="text" name="actor" value="{html.escape(actor)}" style="min-width:12rem"></label>
    <label>目標（模型／部門／使用者）<input type="text" name="target" value="{html.escape(target)}" style="min-width:14rem"></label>
  </p>
  <p><label>動作<select name="action" style="min-width:16rem">
     <option value="">（全部）</option>{action_options}</select></label></p>
  <button type="submit">查詢</button>
</form>"""


@router.get("/audit")
def audit_page(
    admin: dict = Depends(require_admin),
    start: str = "", end: str = "", actor: str = "", action: str = "", target: str = "",
):
    """稽核紀錄查詢。直接線性掃 jsonl——寫入量只有管理者的手動操作，量很小。"""
    records, total = audit.read_audit(start, end, actor, action, target, limit=_AUDIT_PAGE_LIMIT)

    rows = []
    for r in records:
        before, after, rest = audit.detail_parts(r.get("detail"))
        result = r.get("result", "")
        result_cell = (
            f'<span class="ok">成功</span>' if result == "success"
            else f'<span class="err">{html.escape(result)}</span>'
        )
        changed = ""
        if before or after:
            changed = _audit_value(before, "diff-del") + _audit_value(after, "diff-add")
        elif rest:
            changed = _audit_value(rest, "hint")
        rows.append(
            f'<tr><td style="white-space:nowrap">{html.escape((r.get("timestamp") or "")[:19])}</td>'
            f'<td>{html.escape(r.get("actor_username") or "")}</td>'
            f'<td>{html.escape(audit.action_label(r.get("action") or ""))}</td>'
            f'<td><code>{html.escape(r.get("target") or "")}</code></td>'
            f'<td>{result_cell}</td><td>{changed}</td></tr>'
        )

    truncated = (
        f'<p class="warn">符合條件的有 {total} 筆，這裡只顯示最新的 {_AUDIT_PAGE_LIMIT} 筆；'
        "要完整資料請用下面的 CSV 匯出（匯出不受這個上限影響）。</p>"
        if total > len(records) else ""
    )
    export_qs = f"start={quote(start)}&end={quote(end)}&actor={quote(actor)}&action={quote(action)}&target={quote(target)}"

    return _page(f"""
{_nav('audit')}
<h2>稽核紀錄</h2>
<p class="hint">每一次寫入操作都會留一筆，含操作者的 Keycloak 帳號與變更前／後的值。
Key 內容一律只記末四碼。紀錄檔在 <code>ADMIN_AUDIT_LOG_PATH</code>（掛在 PVC 上，pod 重啟不會遺失）。</p>
{_audit_filters(start, end, actor, action, target)}
{truncated}
<p>共 {total} 筆　<a href="{PREFIX}/audit/export?{export_qs}">下載 CSV（Excel 可直接開）</a></p>
<div class="wide"><table>
  <tr><th>時間（UTC）</th><th>操作者</th><th>動作</th><th>目標</th><th>結果</th><th>變更前／後</th></tr>
  {''.join(rows) if rows else '<tr><td colspan="6">沒有符合條件的紀錄。</td></tr>'}
</table></div>
""")


@router.get("/audit/export")
def audit_export(
    admin: dict = Depends(require_admin),
    start: str = "", end: str = "", actor: str = "", action: str = "", target: str = "",
):
    """CSV 匯出（UTF-8 with BOM，欄位標題是中文，Excel 直接開不會亂碼）。

    刻意不做 xlsx：那要多裝 openpyxl，而 CSV 已經滿足「丟進 Excel 看」這個需求。
    上限拉高到 100000 筆，讓匯出拿得到查詢頁顯示不下的完整資料。
    """
    records, _ = audit.read_audit(start, end, actor, action, target, limit=100_000)
    filename = f"admin-web-audit-{start or 'all'}_{end or 'all'}.csv"
    return Response(
        content=audit.to_csv(records),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    id_token_hint = request.cookies.get(ID_TOKEN_COOKIE)
    url = build_logout_url(LOGIN_PATH, id_token_hint=id_token_hint)
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(ID_TOKEN_COOKIE)
    return resp
