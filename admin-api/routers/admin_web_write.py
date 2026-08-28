"""部門管理入口的寫入操作：模型的完整生命週期（上架草稿 → 測試 → 發布 → 停用 →
重新啟用 → 永久刪除）、模型的管理面欄位、上架範本、部門 provider key、立即同步。
跟 routers/admin_web.py（登入 + 唯讀頁）分開放，共用同一個 PREFIX 與版面元件
（_page/_nav/_mask_key），降低單一檔案的長度。模型授權（WP4）另外放在
routers/admin_web_access.py。

跟唯讀頁一樣：一律呼叫 services/ 底下不帶認證的函式，網頁層絕不用
ADMIN_API_KEY 打自己的 API（decision C／R-13）。每個寫入操作都記一筆稽核
（R-39，見 audit.py），且一律用 POST（R-41：GET 不得改變任何狀態）。
"""
import html
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

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
from services import (
    departments_service,
    model_metadata_service,
    models_service,
    openwebui_sync_service,
)

router = APIRouter(prefix=PREFIX)

# provider_keys 的 5 個可設定部門 key 的 provider（vLLM/Ollama 固定共用 EMPTY，不在此列）
_DEPT_KEY_PROVIDERS = [(k, u.label) for k, u in UPSTREAMS.items() if u.key_mode == "choice"]


def _looks_like_placeholder_key(key: str) -> bool:
    """R-21／R-31：擋掉未換過的 OpenRouter 共用 placeholder 前綴。"""
    return key.startswith("sk-or-CHANGE")


def _audit_key(api_key: str | None) -> str:
    """稽核紀錄裡的 key 欄位（R-40：只留末四碼）。

    沒有 key 的時候刻意寫「（無）」而不是空字串：key_policy 是 dept:* 的模型本來
    就不帶 key（執行期才由 config/custom_auth.py 依呼叫者的部門注入），記成 ""
    會讓人以為是「key 被清空了」，跟「這個模型沒有自己的 key」差很多。同一筆
    紀錄裡一定會有 key_policy，兩個一起看才完整。
    """
    return mask_key(api_key) if api_key else "（無）"


# ── WP1：管理面欄位的共用表單片段 ─────────────────────────────────────────────
#
# 客戶回饋的第一個痛點是「使用者要自己拼 JSON、猜 provider slug、決定哪個欄位放
# 哪裡」。表單把這些變成選單與具名欄位，而這一段是所有模型表單（上架、編輯草稿）
# 共用的那幾格，避免兩邊各寫一份而漸漸長歪。

def _metadata_fields(depts: list[dict], meta: dict | None = None) -> str:
    meta = meta or {}
    type_options = "".join(
        f'<option value="{k}"{" selected" if k == (meta.get("model_type") or "chat") else ""}>{html.escape(v)}</option>'
        for k, v in model_metadata_service.MODEL_TYPES.items()
    )
    dept_options = "".join(
        f'<option value="{html.escape(d["dept_id"])}"'
        f'{" selected" if d["dept_id"] == meta.get("cost_center") else ""}>'
        f'{html.escape(d["dept_id"])}｜{html.escape(d["dept_name"])}</option>'
        for d in depts
    )
    period_options = "".join(
        f'<option value="{k}"{" selected" if k == (meta.get("budget_period") or "monthly") else ""}>{html.escape(v)}</option>'
        for k, v in model_metadata_service.BUDGET_PERIODS.items()
    )
    limit_value = "" if meta.get("budget_limit_usd") is None else f'{meta["budget_limit_usd"]:g}'
    return f"""
<fieldset><legend>管理面欄位</legend>
  <p><label>顯示名稱（給人看的名字，可跟呼叫用的 model_name 不同）<br>
     <input type="text" name="display_name" value="{html.escape(meta.get('display_name') or '')}"></label></p>
  <p><label>模型類型<br><select name="model_type">{type_options}</select>
     <br><small class="hint">決定「測試呼叫」要送哪種最小請求——用 chat 的形狀去測 embedding 模型
     只會拿到看不懂的 400。</small></label></p>
  <p><label>成本歸屬部門<br><select name="cost_center">
     <option value="">（不指定）</option>{dept_options}</select>
     <br><small class="hint">只是成本歸屬的標記，不影響誰能用、也不切分額度。</small></label></p>
  <p><label>額度上限（USD，留空＝不設額度）<br>
     <input type="number" name="budget_limit_usd" step="0.01" min="0" value="{limit_value}"></label></p>
  <p><label>額度週期<br><select name="budget_period">{period_options}</select></label></p>
  <p><label><input type="checkbox" name="budget_enforce" value="1"
     {"checked" if meta.get("budget_enforce") else ""}>
     超過額度就擋下來（HTTP 429）</label>
     <br><small class="hint">不勾＝只累計用量、不影響呼叫，適合先觀察一個月再決定。
     用量是 LiteLLM 算得出成本的呼叫才會累計；地端模型沒有定價，成本一律是 0。
     額度用完後最多 30 秒才會開始擋（認證端的設定快取 TTL）。</small></p>
  <p><label>備註<br><textarea name="notes">{html.escape(meta.get('notes') or '')}</textarea></label></p>
</fieldset>"""


def _parse_metadata_form(
    display_name: str, model_type: str, cost_center: str, budget_limit_usd: str,
    budget_period: str, budget_enforce: str, notes: str,
) -> dict:
    """把表單字串轉成 service 層要的型別，順便做驗證。

    額度用 str 接而不是 float：留空的 number 欄位送過來是空字串，用 float 型別
    宣告會被 FastAPI 擋成 422「value is not a valid float」，使用者只會看到一句
    看不懂的錯誤——這正是客戶抱怨的無回饋失敗。
    """
    raw = (budget_limit_usd or "").strip()
    limit = None
    if raw:
        try:
            limit = float(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"額度上限要是數字，收到的是 '{raw}'")
    enforce = bool(budget_enforce)
    model_metadata_service.validate_model_type(model_type)
    model_metadata_service.validate_budget(limit, budget_period, enforce)
    return {
        "display_name": display_name.strip(),
        "model_type": model_type,
        "cost_center": cost_center.strip(),
        "budget_limit_usd": limit,
        "budget_period": budget_period,
        "budget_enforce": enforce,
        "notes": notes.strip(),
    }


def _detail_redirect(model_name: str, msg: str, level: str = "ok") -> RedirectResponse:
    """寫入完一律 302 回詳情頁（POST/Redirect/GET）：重新整理不會把同一個操作再做一次。

    level 只能是 ok／warn，詳情頁據此選 CSS class——訊息本身一律當純文字 escape，
    因為它會經過 query string，是外部可控的輸入。
    """
    return RedirectResponse(
        f"{PREFIX}/models/detail?model_name={quote(model_name, safe='')}"
        f"&msg={quote(msg)}&level={quote(level)}",
        status_code=303,
    )


def _listing_note(results: list[dict]) -> tuple[str, str]:
    """把「OpenWebUI 白名單同步」的結果翻成給操作者看的一句話。

    這是盡力而為的輔助動作：模型在 LiteLLM 的註冊與 DB 的授權才是主體。失敗時
    要明講「請手動加一筆」，不能靜默——白名單沒補上的症狀是「上架、發布、授權
    全部成功，使用者的下拉選單就是沒有它」，整條鏈沒有任何錯誤訊息，是最難查的
    那種故障。
    """
    updated = [r for r in results if r.get("status") == "updated"]
    failed = [r for r in results if r.get("status") == "failed"]
    if failed:
        reasons = "；".join(f'入口 {r["target"]}：{r.get("reason", "")}' for r in failed)
        return (
            "但 OpenWebUI 的「模型 IDs」沒有自動同步——請手動到 OpenWebUI → 管理員設定 → "
            "連線 → 編輯 LiteLLM 那條 → 模型 IDs 補上這個名稱，否則使用者的下拉選單不會"
            f"跟著變。原因：{reasons}",
            "warn",
        )
    if updated:
        return ("同時已同步 OpenWebUI 連線的「模型 IDs」，使用者重整後就會看到變化。", "ok")
    return ("", "ok")


async def _sync_listing(model_name: str, present: bool) -> list[dict]:
    """絕不讓白名單同步的失敗擋掉主操作（發布／停用本身已經成功了）。"""
    try:
        return await openwebui_sync_service.sync_model_listing(model_name, present)
    except Exception as exc:  # noqa: BLE001 — 輔助動作，任何例外都只記錄不中斷
        return [{"status": "failed", "target": "a", "reason": f"未預期的錯誤：{exc}"}]


# ── 上架模型 ──────────────────────────────────────────────────────────────────

@router.get("/models/new")
async def new_model_form(
    admin: dict = Depends(require_admin), upstream: str = "", key_source: str = "", preset: str = "",
):
    presets = model_metadata_service.list_presets()

    if not upstream:
        items = "".join(
            f'<li><a href="{PREFIX}/models/new?upstream={key}">{html.escape(u.label)}</a></li>'
            for key, u in UPSTREAMS.items()
        )
        preset_rows = "".join(
            f'<tr><td><a href="{PREFIX}/models/new?upstream={html.escape(p["payload"].get("upstream", ""))}'
            f'&key_source={html.escape(p["payload"].get("key_source", ""))}'
            f'&preset={quote(p["preset_name"], safe="")}">{html.escape(p["preset_name"])}</a></td>'
            f'<td class="hint">{html.escape(p["payload"].get("upstream", ""))}'
            f'｜{html.escape(p["payload"].get("model_type", "chat"))}</td>'
            f'<td><form method="post" action="{PREFIX}/models/presets/delete" '
            f'onsubmit="return confirm(\'刪除這個範本？\');">'
            f'<input type="hidden" name="preset_name" value="{html.escape(p["preset_name"])}">'
            f'<button type="submit">刪除</button></form></td></tr>'
            for p in presets
        )
        preset_block = f"""
<h3>常用範本</h3>
<p class="hint">從範本開始會自動帶入上次填過的上游、類型、額度等設定（不含 key）。</p>
<table><tr><th>範本</th><th>內容</th><th></th></tr>{preset_rows}</table>
""" if presets else ""

        return _page(f"""
{_nav('models_new')}
<h2>上架新模型</h2>
<p class="hint">上架後會先進到<b>草稿</b>狀態：已經註冊到 LiteLLM（所以測得起來），
但一般使用者一律打不通（<code>custom_auth</code> 回 403），要通過「測試呼叫」之後
才發布得出去。</p>
<h3>第一步：上游是誰？</h3>
<ul>{items}</ul>
{preset_block}
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
    depts = departments_service.list_departments()

    # 範本只帶「填過就不用再填一次」的欄位，key 一律不進範本（見 save_preset）
    preset_payload = {}
    if preset:
        found = model_metadata_service.get_preset(preset)
        if found is None:
            raise HTTPException(status_code=404, detail=f"找不到範本 '{preset}'")
        preset_payload = found["payload"]

    api_base_field = ""
    if up.api_base_mode == "required":
        default = preset_payload.get("api_base") or up.api_base_default or ""
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
<p class="hint">上架後是<b>草稿</b>：一般使用者一律打不通，要先通過測試呼叫才能發布。</p>
<form method="post" action="{PREFIX}/models">
  <input type="hidden" name="upstream" value="{upstream}">
  <input type="hidden" name="key_source" value="{resolved_key_source}">
  <p><label>模型 slug<br>
  <input type="text" name="slug" required value="{html.escape(preset_payload.get('slug', ''))}">
  <br><small class="hint">{html.escape(slug_hint)}</small></label></p>
  <p><label>model_name（呼叫者要打的名字；留空則自動帶入建議值）<br>
  <input type="text" name="model_name" placeholder="留空 = 自動建議"></label></p>
  {api_base_field}
  {key_field}
  {_metadata_fields(depts, preset_payload)}
  {dept_checklist}
  <fieldset><legend>存成常用範本（選填）</legend>
    <p><label>範本名稱<br><input type="text" name="preset_name"
       placeholder="留空＝不存範本"></label>
       <br><small class="hint">會存下上游、slug、類型、額度等欄位，<b>不含 key</b>。</small></p>
  </fieldset>
  <button type="submit">上架為草稿</button>
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
    display_name: str = Form(""),
    model_type: str = Form("chat"),
    cost_center: str = Form(""),
    budget_limit_usd: str = Form(""),
    budget_period: str = Form("monthly"),
    budget_enforce: str = Form(""),
    notes: str = Form(""),
    preset_name: str = Form(""),
):
    up = UPSTREAMS.get(upstream)
    if up is None:
        raise HTTPException(status_code=422, detail=f"不認得的上游 '{upstream}'")
    if not slug.strip():
        raise HTTPException(status_code=422, detail="模型 slug 不可留空")

    resolved_key_source = "shared" if up.key_mode == "fixed_shared" else key_source
    if resolved_key_source not in ("dept", "shared"):
        raise HTTPException(status_code=422, detail=f"不認得的 key_source '{key_source}'")

    fields = _parse_metadata_form(
        display_name, model_type, cost_center, budget_limit_usd, budget_period, budget_enforce, notes
    )

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

    body = ExternalModelIn(
        model_name=name, model=model, api_key=final_api_key, api_base=resolved_api_base,
        key_policy=key_policy, upstream=upstream, status="draft", **fields,
    )

    audit_detail = {
        "before": None,
        "after": {
            "model": model, "api_base": resolved_api_base, "key_policy": key_policy,
            "api_key": _audit_key(final_api_key), "status": "draft", **fields,
        },
        "upstream": upstream,
    }
    try:
        result = await models_service.create_external_model(body)
    except HTTPException as exc:
        write_audit(admin, "create_external_model", name, "failed",
                    {**audit_detail, "status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    write_audit(admin, "create_external_model", name, "success", audit_detail)

    if preset_name.strip():
        payload = {"upstream": upstream, "key_source": resolved_key_source,
                   "slug": slug.strip(), "api_base": api_base.strip(), **fields}
        model_metadata_service.save_preset(preset_name, payload)
        write_audit(admin, "save_model_preset", preset_name.strip(), "success",
                    {"before": None, "after": payload})

    ip_warning = ""
    if up.api_base_mode == "required" and resolved_api_base and looks_like_ip(resolved_api_base):
        ip_warning = ('<p class="warn">提醒：api_base 看起來是節點 IP，建議改用 Service DNS'
                      '（換機器就不用重設），但已照你填的值上架。</p>')

    return _page(f"""
{_nav('models_new')}
<h2>已建立草稿</h2>
{ip_warning}
<p>model_name（可直接選取複製）：</p>
<p><code style="font-size:1.1rem">{html.escape(result['model_name'])}</code></p>
<p>這個模型現在是<b>草稿</b>——已經註冊到 LiteLLM，但 <code>config/custom_auth.py</code>
會擋掉所有一般使用者的呼叫（403）。接下來：</p>
<ol>
  <li>到<a href="{PREFIX}/models/detail?model_name={quote(result['model_name'], safe='')}">模型詳情頁</a>
      按「測試呼叫」，確認真的打得通</li>
  <li>通過之後按「發布」</li>
  <li>到<a href="{PREFIX}/access">模型授權</a>把它開給要用的部門或個人（存檔即生效）</li>
  <li>到 OpenWebUI：設定 → 連線 → 編輯 LiteLLM 連線 → 「模型 IDs」新增一筆，
      字串要跟上面 <code>{html.escape(result['model_name'])}</code> 逐字相同
      （這一步是讓模型出現在聊天畫面的下拉選單，授權本身已經在上一步做完了）</li>
</ol>
<p><a class="btn" href="{PREFIX}/models">回模型清單</a></p>
""")


@router.post("/models/presets/delete")
def delete_preset(admin: dict = Depends(require_admin), preset_name: str = Form(...)):
    before = model_metadata_service.get_preset(preset_name)
    model_metadata_service.delete_preset(preset_name)
    write_audit(admin, "delete_model_preset", preset_name, "success",
                {"before": before["payload"] if before else None, "after": None})
    return RedirectResponse(f"{PREFIX}/models/new", status_code=303)


# ── WP2/WP3：生命週期操作 ─────────────────────────────────────────────────────
#
# model_name 一律走 POST 表單欄位／query string，不當路徑參數——名稱含斜線
# （openrouter/anthropic/claude-sonnet-4-5），百分比編碼的 %2F 會被 ASGI 伺服器
# 解碼成真正的斜線，路徑就切錯段了。

@router.post("/models/test")
async def test_model(admin: dict = Depends(require_admin), model_name: str = Form(...)):
    """WP3：依模型類型送一次最小請求，結果存回 model_metadata 並當作發布的前置條件。"""
    try:
        result = await models_service.test_model(model_name)
    except HTTPException as exc:
        write_audit(admin, "test_model", model_name, "failed",
                    {"status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    write_audit(admin, "test_model", model_name, "success" if result["ok"] else "failed",
                {"model_type": result["model_type"], "result": result["result"]})
    return _detail_redirect(
        model_name, "測試呼叫通過，可以發布了。" if result["ok"] else "測試呼叫失敗，詳細訊息見下方。"
    )


@router.post("/models/publish")
async def publish_model(admin: dict = Depends(require_admin), model_name: str = Form(...)):
    before = model_metadata_service.get_metadata(model_name)
    try:
        after = await models_service.publish_model(model_name)
    except HTTPException as exc:
        write_audit(admin, "publish_model", model_name, "failed",
                    {"before": {"status": before.get("status")},
                     "status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    listing = await _sync_listing(model_name, present=True)
    write_audit(admin, "publish_model", model_name, "success", {
        "before": {"status": before.get("status")},
        "after": {"status": after.get("status")},
        "openwebui_listing": listing,
    })
    note, level = _listing_note(listing)
    return _detail_redirect(model_name, "已發布。授權過的使用者現在打得到了。" + (" " + note if note else ""), level)


@router.post("/models/disable")
async def disable_model(admin: dict = Depends(require_admin), model_name: str = Form(...)):
    """停用：從 LiteLLM 刪掉，但設定完整保留。授權（allowed_models）刻意不動。"""
    before = model_metadata_service.get_metadata(model_name)
    impact = models_service.model_impact(model_name)
    try:
        after = await models_service.disable_model(model_name)
    except HTTPException as exc:
        write_audit(admin, "disable_model", model_name, "failed",
                    {"before": {"status": before.get("status")},
                     "status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    listing = await _sync_listing(model_name, present=False)
    write_audit(admin, "disable_model", model_name, "success", {
        "before": {"status": before.get("status")},
        "after": {"status": after.get("status")},
        "affected_headcount": impact["total_headcount"],
        "affected_departments": [d["dept_id"] for d in impact["departments"]],
        "openwebui_listing": listing,
    })
    note, level = _listing_note(listing)
    return _detail_redirect(
        model_name,
        f"已停用，影響 {impact['total_headcount']} 人。設定完整保留，隨時可以重新啟用。"
        + (" " + note if note else ""),
        level,
    )


@router.post("/models/enable")
async def enable_model(admin: dict = Depends(require_admin), model_name: str = Form(...)):
    before = model_metadata_service.get_metadata(model_name)
    try:
        after = await models_service.enable_model(model_name)
    except HTTPException as exc:
        write_audit(admin, "enable_model", model_name, "failed",
                    {"before": {"status": before.get("status")},
                     "status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    listing = await _sync_listing(model_name, present=(after.get("status") == "published"))
    write_audit(admin, "enable_model", model_name, "success", {
        "before": {"status": before.get("status")},
        "after": {"status": after.get("status")},
        "openwebui_listing": listing,
    })
    msg = (
        "已重新啟用並回到「已發布」。"
        if after.get("status") == "published" else
        "已重新啟用，回到「草稿」（這個模型還沒通過測試呼叫）。"
    )
    note, level = _listing_note(listing)
    return _detail_redirect(
        model_name,
        msg + " 停用期間 DB 的授權可能被同步流程清掉，下一次從 OpenWebUI 拉回（≤2 分鐘）會自動補回來。"
        + (" " + note if note else ""),
        level,
    )


@router.post("/models/fields")
async def update_model_fields(
    admin: dict = Depends(require_admin),
    model_name: str = Form(...),
    display_name: str = Form(""),
    cost_center: str = Form(""),
    budget_limit_usd: str = Form(""),
    budget_period: str = Form("monthly"),
    budget_enforce: str = Form(""),
    notes: str = Form(""),
    model_type: str = Form(""),
):
    """描述性欄位（顯示名稱／成本歸屬／額度／備註），任何狀態都能改。

    model_type 不在這裡——它決定測試呼叫的形狀，改了之前那次測試就不算數了，
    所以歸在「上游設定」那組、只有草稿能改。

    對還沒有管理紀錄的「既有」模型，這一存就是把它納管（見 service 層的說明）。
    """
    before = model_metadata_service.get_metadata(model_name)
    fields = _parse_metadata_form(
        display_name, model_type or "chat", cost_center, budget_limit_usd,
        budget_period, budget_enforce, notes,
    )
    # 表單有送 model_type 才傳下去（只有地端模型的表單會有這一格，見詳情頁）；
    # DB-managed 模型的類型仍然只有草稿狀態能改。
    fields["model_type"] = model_type or None
    after = await models_service.update_descriptive_fields(model_name, **fields)
    tracked = ["display_name", "cost_center", "budget_limit_usd", "budget_enforce",
               "budget_period", "notes", "model_type"]
    write_audit(admin, "update_model_fields", model_name, "success", {
        "before": {k: before.get(k) for k in tracked},
        "after": {k: after.get(k) for k in tracked},
    })
    return _detail_redirect(model_name, "已儲存。額度設定最多 30 秒生效。")


@router.get("/models/edit")
async def edit_draft_form(model_name: str, admin: dict = Depends(require_admin)):
    """編輯草稿的上游設定。LiteLLM 沒有 /model/update，儲存時是「刪掉再用新值建一次」。"""
    meta = model_metadata_service.get_metadata(model_name)
    if not meta["has_record"]:
        raise HTTPException(
            status_code=404,
            detail=f"'{model_name}' 沒有管理紀錄，不受狀態機管理，無法從這裡編輯",
        )
    if meta["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"只有草稿能編輯上游設定，'{model_name}' 目前是 "
            f"{model_metadata_service.STATUSES.get(meta['status'], meta['status'])}。要改請先停用。",
        )

    depts = departments_service.list_departments()
    up = UPSTREAMS.get(meta["upstream"])
    upstream_label = up.label if up else (meta["upstream"] or "（未記錄）")
    key_hint = (
        f'目前的共用 key：{html.escape(_mask_key(meta["api_key"]))}；留空＝沿用不變'
        if meta["api_key"] else "這個模型用的是部門 provider key，這裡不需要填"
    )

    return _page(f"""
{_nav('models')}
<h2>編輯草稿：<code>{html.escape(model_name)}</code></h2>
<p class="hint">LiteLLM 沒有「更新模型」這個功能，儲存的實作是<b>刪掉再用新值建一次</b>。
草稿還沒有人在用，所以這樣做是安全的；也因為這樣，已發布的模型不開放編輯上游設定。
<b>存檔後上一次的測試結果會被清掉，要重新測試才能發布。</b></p>
<p>上游：{html.escape(upstream_label)}（不能改，要換上游請刪掉重新上架）</p>
<form method="post" action="{PREFIX}/models/edit">
  <input type="hidden" name="model_name" value="{html.escape(model_name)}">
  <p><label>litellm_params.model<br>
     <input type="text" name="model" required value="{html.escape(meta['litellm_model'])}"></label></p>
  <p><label>api_base（留空＝用上游預設端點）<br>
     <input type="text" name="api_base" value="{html.escape(meta['api_base'] or '')}"></label></p>
  <p><label>共用 API key<br><input type="password" name="api_key" autocomplete="off">
     <br><small class="hint">{key_hint}</small></label></p>
  {_metadata_fields(depts, meta)}
  <button type="submit">儲存（會刪除重建）</button>
</form>
<p><a href="{PREFIX}/models/detail?model_name={quote(model_name, safe='')}">« 回詳情頁</a></p>
""")


@router.post("/models/edit")
async def edit_draft(
    admin: dict = Depends(require_admin),
    model_name: str = Form(...),
    model: str = Form(...),
    api_base: str = Form(""),
    api_key: str = Form(""),
    display_name: str = Form(""),
    model_type: str = Form("chat"),
    cost_center: str = Form(""),
    budget_limit_usd: str = Form(""),
    budget_period: str = Form("monthly"),
    budget_enforce: str = Form(""),
    notes: str = Form(""),
):
    before = model_metadata_service.get_metadata(model_name)
    fields = _parse_metadata_form(
        display_name, model_type, cost_center, budget_limit_usd, budget_period, budget_enforce, notes
    )

    # 空的 key 欄位＝沿用原值，不是清除——跟 Provider Key 頁面同一個慣例
    # （見 docs/admin-web-plan.md「已定案」#5）。
    final_key = api_key.strip() or before["api_key"]
    if final_key and _looks_like_placeholder_key(final_key):
        raise HTTPException(
            status_code=422,
            detail="這把 key 看起來還是沒換過的共用 placeholder（sk-or-CHANGE 開頭），請填入真正的 key",
        )

    # key 留空且上游支援部門 key → 回到 dept:<provider> 政策；否則沿用模型自帶的 key
    up = UPSTREAMS.get(before["upstream"])
    key_policy = f"dept:{up.provider}" if (up and up.key_mode == "choice" and not final_key) else "model"

    try:
        after = await models_service.update_draft_model(
            model_name, model=model.strip(), api_base=api_base.strip() or None,
            api_key=final_key or None, key_policy=key_policy, upstream=before["upstream"], **fields,
        )
    except HTTPException as exc:
        write_audit(admin, "update_draft_model", model_name, "failed",
                    {"status_code": exc.status_code, "detail": str(exc.detail)})
        raise

    tracked = ["litellm_model", "api_base", "display_name", "model_type", "cost_center",
               "budget_limit_usd", "budget_enforce", "budget_period", "notes"]
    write_audit(admin, "update_draft_model", model_name, "success", {
        "before": {**{k: before.get(k) for k in tracked}, "api_key": _audit_key(before.get("api_key"))},
        "after": {**{k: after.get(k) for k in tracked}, "api_key": _audit_key(after.get("api_key"))},
        "key_policy": key_policy,   # 沒有這個，光看 api_key 是「（無）」判斷不出為什麼
    })
    return _detail_redirect(model_name, "已重建。上一次的測試結果已清除，請重新測試後再發布。")


@router.post("/models/hard-delete")
async def hard_delete_model(
    admin: dict = Depends(require_admin), model_name: str = Form(...), confirm: str = Form(""),
):
    """永久刪除。呼叫前詳情頁已經把影響範圍算給操作者看過（客戶回饋明確要求這件事）。"""
    if not confirm:
        raise HTTPException(status_code=422, detail="必須勾選確認才能永久刪除")
    before = model_metadata_service.get_metadata(model_name)
    impact = models_service.model_impact(model_name)
    try:
        await models_service.hard_delete_model(model_name)
    except HTTPException as exc:
        write_audit(admin, "delete_external_model", model_name, "failed",
                    {"status_code": exc.status_code, "detail": str(exc.detail)})
        raise
    listing = await _sync_listing(model_name, present=False)
    write_audit(admin, "delete_external_model", model_name, "success", {
        "openwebui_listing": listing,
        "before": {"status": before.get("status"), "litellm_model": before.get("litellm_model"),
                   "api_base": before.get("api_base"), "api_key": _audit_key(before.get("api_key"))},
        "after": None,
        "affected_headcount": impact["total_headcount"],
        "affected_departments": [d["dept_id"] for d in impact["departments"]],
    })

    return _page(f"""
{_nav('models')}
<h2>已永久刪除</h2>
<p><code>{html.escape(model_name)}</code> 已從 LiteLLM 與管理紀錄中移除，
影響 {impact['total_headcount']} 人。用量累計（model_spend）刻意保留，
避免同名模型重新上架時歷史花費被歸零。</p>
<p class="hint">OpenWebUI 那邊的授權記錄不會自動清除；下一次同步會把它列進
「模型 ID 對不上 LiteLLM」的診斷清單。</p>
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
