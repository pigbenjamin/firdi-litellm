"""部門管理入口的三個寫入操作（決策 D 第一期範圍）：上架/下架模型、部門
provider key、立即同步。跟 routers/admin_web.py（登入 + 唯讀頁）分開放，
共用同一個 PREFIX 與版面元件（_page/_nav/_mask_key），降低單一檔案的長度。

跟唯讀頁一樣：一律呼叫 services/ 底下不帶認證的函式，網頁層絕不用
ADMIN_API_KEY 打自己的 API（decision C／R-13）。每個寫入操作都記一筆稽核
（R-39，見 audit.py），且一律用 POST（R-41：GET 不得改變任何狀態）。
"""
import html
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException

from admin_auth import require_admin
from audit import mask_key, write_audit
from model_upstreams import FIXED_SHARED_KEY, UPSTREAMS, derive_api_base, derive_model, looks_like_ip, suggest_model_name
from models import DepartmentPatch, ExternalModelIn
from routers.admin_web import (
    PREFIX,
    SYNC_THROTTLE_SECONDS,
    _mask_key,
    _nav,
    _page,
    record_sync,
    render_sync_result,
    sync_throttle_remaining,
)
from services import departments_service, models_service, openwebui_sync_service

router = APIRouter(prefix=PREFIX)

# provider_keys 的 5 個可設定部門 key 的 provider（vLLM/Ollama 固定共用 EMPTY，不在此列）
_DEPT_KEY_PROVIDERS = [(k, u.label) for k, u in UPSTREAMS.items() if u.key_mode == "choice"]


def _looks_like_placeholder_key(key: str) -> bool:
    """R-21／R-31：擋掉未換過的 OpenRouter 共用 placeholder 前綴。"""
    return key.startswith("sk-or-CHANGE")


# ── 上架模型 ──────────────────────────────────────────────────────────────────

@router.get("/models/new")
async def new_model_form(admin: dict = Depends(require_admin), upstream: str = "", key_source: str = ""):
    if not upstream:
        items = "".join(
            f'<li><a href="{PREFIX}/models/new?upstream={key}">{html.escape(u.label)}</a></li>'
            for key, u in UPSTREAMS.items()
        )
        return _page(f"""
{_nav('models_new')}
<h2>上架新模型</h2>
<p class="hint">上架是不可逆的操作：沒有編輯功能，要改設定只能刪除後重建，
而刪除會讓所有部門立即打不通這個模型。</p>
<h3>第一步：上游是誰？</h3>
<ul>{items}</ul>
""")

    up = UPSTREAMS.get(upstream)
    if up is None:
        raise HTTPException(status_code=422, detail=f"不認得的上游 '{upstream}'")

    if up.key_mode == "choice" and key_source not in ("dept", "shared"):
        return _page(f"""
{_nav('models_new')}
<h2>上架新模型：{html.escape(up.label)}</h2>
<h3>第二步：key 從哪來？</h3>
<ul>
  <li><a href="{PREFIX}/models/new?upstream={upstream}&key_source=dept">各部門自己的 key</a>
      —— 之後到「Provider Key」頁面幫要用這個模型的部門逐一設定</li>
  <li><a href="{PREFIX}/models/new?upstream={upstream}&key_source=shared">共用一把 key</a>
      —— 現在直接輸入一把 key，全部部門共用</li>
</ul>
<p><a href="{PREFIX}/models/new">« 換一個上游</a></p>
""")

    resolved_key_source = "shared" if up.key_mode == "fixed_shared" else key_source

    api_base_field = ""
    if up.api_base_mode == "required":
        default = up.api_base_default or ""
        hint = f'<br><small class="hint">{html.escape(up.api_base_hint)}</small>' if up.api_base_hint else ""
        api_base_field = f"""<p><label>api_base（必填）<br>
  <input type="text" name="api_base" value="{html.escape(default)}" required>{hint}</label></p>"""

    key_field = ""
    dept_checklist = ""
    if up.key_mode == "fixed_shared":
        key_field = f'<p class="hint">key：固定共用 <code>{FIXED_SHARED_KEY}</code>，不需要填。</p>'
    elif resolved_key_source == "shared":
        key_field = """<p><label>共用的 API key（必填）<br>
  <input type="password" name="api_key" required autocomplete="off"></label></p>"""
    else:  # dept
        depts = departments_service.list_departments()
        rows = "".join(
            f"<tr><td>{html.escape(d['dept_id'])}</td>"
            f"<td>{'已設定' if (d['provider_keys'] or {}).get(up.provider) else '尚未設定'}</td></tr>"
            for d in depts
        )
        dept_checklist = f"""
<p class="hint">key 來源是「各部門自己」——這個模型上架後每個部門要各自到
<a href="{PREFIX}/keys?provider={up.provider}">Provider Key</a> 頁面設定 {html.escape(up.label)} 的 key，
沒設定的部門會在呼叫時打不通。目前狀態：</p>
<table><tr><th>部門</th><th>{html.escape(up.label)} key</th></tr>{rows}</table>
"""

    slug_hint = {
        "openrouter": "OpenRouter 的 model slug，例如 anthropic/claude-sonnet-4-5",
        "openai": "OpenAI 官方 model id，例如 gpt-4o-mini",
        "anthropic": "Anthropic 官方 model id，例如 claude-sonnet-4-5-20250929",
        "gemini": "Gemini 官方 model id，例如 gemini-2.0-flash-001",
        "vllm": "vLLM 的 served-model-name，例如 google/gemma-4-31B-it",
        "ollama": "Ollama 的 model tag，例如 gemma4:31b",
        "other": "該服務文件裡的 model id",
    }[upstream]

    return _page(f"""
{_nav('models_new')}
<h2>上架新模型：{html.escape(up.label)}</h2>
<form method="post" action="{PREFIX}/models">
  <input type="hidden" name="upstream" value="{upstream}">
  <input type="hidden" name="key_source" value="{resolved_key_source}">
  <p><label>模型 slug<br><input type="text" name="slug" required>
  <br><small class="hint">{html.escape(slug_hint)}</small></label></p>
  <p><label>model_name（呼叫者要打的名字；留空則自動帶入建議值）<br>
  <input type="text" name="model_name" placeholder="留空 = 自動建議"></label></p>
  {api_base_field}
  {key_field}
  {dept_checklist}
  <p><label><input type="checkbox" name="confirm" required>
  我了解上架後無法編輯，只能刪除後重建；下架會讓所有部門立即打不通這個模型</label></p>
  <button type="submit">上架</button>
</form>
<p><a href="{PREFIX}/models/new">« 重新選擇</a></p>
""")


@router.post("/models")
async def create_model(
    admin: dict = Depends(require_admin),
    upstream: str = Form(...),
    key_source: str = Form(...),
    slug: str = Form(...),
    model_name: str = Form(""),
    api_base: str = Form(""),
    api_key: str = Form(""),
    confirm: str = Form(""),
):
    up = UPSTREAMS.get(upstream)
    if up is None:
        raise HTTPException(status_code=422, detail=f"不認得的上游 '{upstream}'")
    if not confirm:
        raise HTTPException(status_code=422, detail="必須勾選確認才能上架（上架不可逆）")
    if not slug.strip():
        raise HTTPException(status_code=422, detail="模型 slug 不可留空")

    resolved_key_source = "shared" if up.key_mode == "fixed_shared" else key_source
    if resolved_key_source not in ("dept", "shared"):
        raise HTTPException(status_code=422, detail=f"不認得的 key_source '{key_source}'")

    model = derive_model(up, slug.strip())
    name = model_name.strip() or suggest_model_name(up, slug.strip(), resolved_key_source)

    if up.key_mode == "fixed_shared":
        key_policy = "model"
        final_api_key = FIXED_SHARED_KEY
    elif resolved_key_source == "shared":
        if not api_key.strip():
            raise HTTPException(status_code=422, detail="key 來源是「共用一把」時，key 必填")
        if _looks_like_placeholder_key(api_key.strip()):
            raise HTTPException(
                status_code=422,
                detail="這把 key 看起來還是沒換過的共用 placeholder（sk-or-CHANGE 開頭），"
                "請填入真正的 key",
            )
        key_policy = "model"
        final_api_key = api_key.strip()
    else:  # dept
        key_policy = f"dept:{up.provider}"
        final_api_key = None

    resolved_api_base = derive_api_base(up, api_base)
    ip_warning = ""
    if up.api_base_mode == "required" and resolved_api_base and looks_like_ip(resolved_api_base):
        ip_warning = "<p class=\"hint\">提醒：api_base 看起來是節點 IP，建議改用 Service DNS（換機器就不用重設），但已照你填的值上架。</p>"

    body = ExternalModelIn(
        model_name=name, model=model, api_key=final_api_key, api_base=resolved_api_base, key_policy=key_policy
    )

    try:
        result = await models_service.create_external_model(body)
    except HTTPException as exc:
        write_audit(admin, "create_external_model", name, "failed", {"status": exc.status_code, "detail": str(exc.detail)})
        raise

    write_audit(admin, "create_external_model", name, "success", {"key_policy": key_policy, "upstream": upstream})

    return _page(f"""
{_nav('models_new')}
<h2>上架成功</h2>
{ip_warning}
<p>model_name（可直接選取複製）：</p>
<p><code style="font-size:1.1rem">{html.escape(result['model_name'])}</code></p>
<p>這個模型現在<b>還沒有任何人能用</b>——上架跟「開放使用權限」是分開的兩件事，
一定要到 OpenWebUI 完成後半段：</p>
<ol>
  <li>設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」新增一筆，字串要跟上面
      <code>{html.escape(result['model_name'])}</code> 逐字相同</li>
  <li>Workspace → Models → 選這個模型 → 設定部門（group）或使用者的授權</li>
</ol>
<p><a class="btn" href="{PREFIX}/models">回模型清單</a></p>
""")


@router.post("/models/{model_id}/delete")
async def delete_model(model_id: str, admin: dict = Depends(require_admin)):
    external = await models_service.list_external_models()
    matched = next((m for m in external["models"] if m["id"] == model_id), None)
    model_name = matched["model_name"] if matched else model_id

    try:
        await models_service.delete_external_model(model_id)
    except HTTPException as exc:
        write_audit(admin, "delete_external_model", model_name, "failed", {"status": exc.status_code, "detail": str(exc.detail)})
        raise

    write_audit(admin, "delete_external_model", model_name, "success", {})

    return _page(f"""
{_nav('models')}
<h2>已下架</h2>
<p><code>{html.escape(model_name)}</code> 已下架，所有部門立即打不通這個模型。</p>
<p><a class="btn" href="{PREFIX}/models">回模型清單</a></p>
""")


# ── Provider Key 設定 ─────────────────────────────────────────────────────────

@router.get("/keys")
def keys_form(admin: dict = Depends(require_admin), provider: str = ""):
    if not provider:
        items = "".join(
            f'<li><a href="{PREFIX}/keys?provider={key}">{html.escape(label)}</a></li>'
            for key, label in _DEPT_KEY_PROVIDERS
        )
        return _page(f"""
{_nav('keys')}
<h2>Provider Key</h2>
<p class="hint">地端 vLLM／Ollama 固定共用 <code>{FIXED_SHARED_KEY}</code>，不需要、也不能設定部門 key。</p>
<h3>選一個 provider</h3>
<ul>{items}</ul>
""")

    label = dict(_DEPT_KEY_PROVIDERS).get(provider)
    if label is None:
        raise HTTPException(status_code=422, detail=f"'{provider}' 不是可以設定部門 key 的 provider")

    depts = departments_service.list_departments()
    rows = "".join(
        f"""<tr>
  <td><input type="checkbox" name="dept_ids" value="{html.escape(d['dept_id'])}"></td>
  <td>{html.escape(d['dept_id'])}</td><td>{html.escape(d['dept_name'])}</td>
  <td>{html.escape(_mask_key((d['provider_keys'] or {}).get(provider, '')))}</td>
</tr>"""
        for d in depts
    )

    return _page(f"""
{_nav('keys')}
<h2>Provider Key：{html.escape(label)}</h2>
<p class="hint">生效時間 ≤30 秒（enforcement 端有 30 秒 TTL 快取）。留空的部門不會被勾選；
空白的 key 欄位一律代表「不修改」，這個表單不提供清除功能——真的要清除請走
<code>ADMIN_API_KEY</code> 的 curl 路徑。</p>
<form method="post" action="{PREFIX}/keys">
  <input type="hidden" name="provider" value="{provider}">
  <p><label>要套用的 key（必填，會套用到下面勾選的所有部門）<br>
  <input type="password" name="api_key" required autocomplete="off"></label></p>
  <p>
    <button type="button" onclick="document.querySelectorAll('input[name=dept_ids]').forEach(c=>c.checked=true)">全選</button>
    <button type="button" onclick="document.querySelectorAll('input[name=dept_ids]').forEach(c=>c.checked=false)">全不選</button>
  </p>
  <table><tr><th></th><th>部門</th><th>名稱</th><th>目前的 {html.escape(label)} key</th></tr>{rows}</table>
  <button type="submit">套用到勾選的部門</button>
</form>
""")


@router.post("/keys")
def apply_keys(
    admin: dict = Depends(require_admin),
    provider: str = Form(...),
    api_key: str = Form(...),
    dept_ids: list[str] = Form(default=[]),
):
    label = dict(_DEPT_KEY_PROVIDERS).get(provider)
    if label is None:
        raise HTTPException(status_code=422, detail=f"'{provider}' 不是可以設定部門 key 的 provider")
    if not api_key.strip():
        raise HTTPException(status_code=422, detail="key 必填——這個表單不提供清除功能，空白不會送出")
    if _looks_like_placeholder_key(api_key.strip()):
        raise HTTPException(
            status_code=422,
            detail="這把 key 看起來還是沒換過的共用 placeholder（sk-or-CHANGE 開頭），請填入真正的 key",
        )
    if not dept_ids:
        raise HTTPException(status_code=422, detail="至少要勾選一個部門")

    key = api_key.strip()
    results = []
    for dept_id in dept_ids:
        try:
            departments_service.patch_department(dept_id, DepartmentPatch(provider_keys={provider: key}))
        except HTTPException as exc:
            results.append({"dept_id": dept_id, "ok": False, "detail": str(exc.detail)})
            write_audit(
                admin, f"set_provider_key:{provider}", dept_id, "failed",
                {"key_last4": mask_key(key), "status": exc.status_code, "detail": str(exc.detail)},
            )
        else:
            results.append({"dept_id": dept_id, "ok": True, "detail": ""})
            write_audit(admin, f"set_provider_key:{provider}", dept_id, "success", {"key_last4": mask_key(key)})

    # R-30：逐一 PATCH，部分失敗要逐筆回報，不可只顯示「失敗」也不可讓人以為全部沒生效。
    rows = "".join(
        f"<tr><td>{html.escape(r['dept_id'])}</td>"
        f"<td>{'成功' if r['ok'] else '失敗：' + html.escape(r['detail'])}</td></tr>"
        for r in results
    )
    ok_count = sum(1 for r in results if r["ok"])

    return _page(f"""
{_nav('keys')}
<h2>套用結果：{html.escape(label)}</h2>
<p>{ok_count} / {len(results)} 個部門套用成功。生效時間 ≤30 秒。</p>
<table><tr><th>部門</th><th>結果</th></tr>{rows}</table>
<p><a class="btn" href="{PREFIX}/keys?provider={provider}">回這個 provider 的設定頁</a></p>
""")


# ── 立即同步（真正寫入，R-35～R-37）─────────────────────────────────────────────

@router.post("/sync")
async def trigger_sync(admin: dict = Depends(require_admin)):
    username = admin["preferred_username"]
    remaining = sync_throttle_remaining(username)
    if remaining > 0:
        return _page(f"""
{_nav('sync')}
<h2>請稍候</h2>
<p>節流中：同一帳號 {SYNC_THROTTLE_SECONDS} 秒內只能觸發一次同步，還要等 {int(remaining) + 1} 秒。</p>
<p><a class="btn" href="{PREFIX}/sync">回同步與診斷頁</a></p>
""", status_code=429)

    result = await openwebui_sync_service.pull_openwebui_model_access(dry_run=False)
    now = datetime.now(timezone.utc).isoformat()
    record_sync(username, now)
    write_audit(admin, "sync_permissions", "platform", "success", {
        "changed_departments": len(result["changed_departments"]),
        "changed_users": len(result["changed_users"]),
    })

    return _page(f"""
{_nav('sync')}
<h2>同步完成</h2>
<p>已依 OpenWebUI 現況重算全平台的模型權限。</p>
{render_sync_result(result)}
<p><a class="btn" href="{PREFIX}/sync">回同步與診斷頁</a></p>
""")
