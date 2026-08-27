"""模型授權（WP4）：部門×模型矩陣與個人授權，存檔即生效。

這是 docs/admin-web-plan.md 決策 D「模型授權一律唯讀」的翻轉（該文件稱為第二期）。
當初唯讀的理由是「權威來源是 OpenWebUI，寫 DB 會在 2 分鐘內被 pull 覆寫」；這裡
的解法不是關掉 pull，而是**寫完立刻 push 回 OpenWebUI**，讓兩邊一致，之後不管
什麼時候 pull 回來結果都一樣。等於把文件裡的手動 SOP（pull → PATCH → push）包成
一次「儲存」。

push 是**取代式的全平台鏡像**，一次錯誤的寫入可以清掉所有人的權限（admin-web-plan.md
已列為已知風險）。所以這裡強制兩段式，沒有例外：

    表單 → /preview（純計算差異，一個字都不寫）→ 人看過按確認 → /apply（寫 DB + push）

每次 apply 都把 before/after 寫進稽核（WP5），出事回得去。
"""
import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException

from admin_auth import require_admin
from audit import write_audit
from routers.admin_web import PREFIX, _nav, _page, status_badge
from services import model_access_service, model_metadata_service, models_service

router = APIRouter(prefix=PREFIX)

_GRANT_SEP = "|"  # checkbox 值的格式是 "<dept_id>|<model_name>"；dept_id 不含 |，model_name 可能含 /


async def _known_models() -> tuple[list[str], dict[str, dict]]:
    """(LiteLLM 目前所有可用模型 id, model_name → metadata)。

    用 /models 全清單而不是只有 DB-managed 的那些——YAML model_list 定義的地端模型
    （gemma-4-31B-it 等）一樣要能授權，它們才是日常用量最大的那幾個。
    """
    listing = await models_service.list_models()
    return listing["models"], model_metadata_service.list_metadata()


def _model_header(model_id: str, metadata: dict[str, dict]) -> str:
    meta = metadata.get(model_id)
    badge = f" {status_badge(meta)}" if meta and meta.get("status") != "published" else ""
    return f'<th style="white-space:nowrap"><code>{html.escape(model_id)}</code>{badge}</th>'


def _parse_grants(grants: list[str], dept_ids: set[str]) -> dict[str, list[str]]:
    """勾選的 checkbox → dept_id → 模型清單。沒被勾的部門也要有一筆空清單，
    否則「把某部門的權限全部取消」會被當成「這個部門沒有出現在表單裡」而漏掉。
    """
    result: dict[str, list[str]] = {d: [] for d in dept_ids}
    for raw in grants:
        dept_id, sep, model_id = raw.partition(_GRANT_SEP)
        if not sep or dept_id not in result:
            continue
        result[dept_id].append(model_id)
    return result


def _diff_rows(diffs: list[dict]) -> str:
    rows = []
    for d in diffs:
        added = "".join(f'<div class="diff-add">＋ {html.escape(m)}</div>' for m in d["added"])
        removed = "".join(f'<div class="diff-del">－ {html.escape(m)}</div>' for m in d["removed"])
        rows.append(
            f'<tr><td>{html.escape(d["id"])}<br><span class="hint">{html.escape(d["name"] or "")}</span></td>'
            f'<td>{d["headcount"]} 人</td>'
            f'<td>{added or ""}{removed or ""}</td>'
            f'<td class="hint">{html.escape(", ".join(d["after"]) or "（無任何授權）")}</td></tr>'
        )
    return "".join(rows)


def _confirm_page(nav_key: str, title: str, diffs: list[dict], hidden_fields: str, apply_action: str) -> object:
    total_people = sum(d["headcount"] for d in diffs)
    revoking = [d for d in diffs if d["removed"]]
    warning = (
        f'<p class="err"><b>注意：這次會「收回」授權。</b>有 {len(revoking)} 個對象會失去部分模型，'
        "他們在 OpenWebUI 的聊天畫面上會立刻少掉那些模型。</p>" if revoking else ""
    )
    return _page(f"""
{_nav(nav_key)}
<h2>{html.escape(title)}</h2>
<p class="hint">以下是<b>還沒寫入</b>的差異預覽。按下確認之後才會寫入資料庫，
並立刻鏡像回 OpenWebUI（取代式的全平台鏡像），使用者端即時生效。</p>
{warning}
<p>共 {len(diffs)} 個對象有變化，涵蓋 {total_people} 人。</p>
<div class="wide"><table>
  <tr><th>對象</th><th>人數</th><th>變化</th><th>變更後的完整授權</th></tr>
  {_diff_rows(diffs)}
</table></div>
<form method="post" action="{apply_action}">
  {hidden_fields}
  <button type="submit">確認並立即生效</button>
</form>
<p><a href="{PREFIX}/access">« 取消，回授權頁</a></p>
""")


def _no_change_page(nav_key: str, back: str):
    return _page(f"""
{_nav(nav_key)}
<h2>沒有任何變化</h2>
<p>送出的內容跟目前的設定一模一樣，沒有東西需要寫入。</p>
<p><a class="btn" href="{back}">« 回上一頁</a></p>
""")


async def _apply_and_push(admin: dict, action: str, diffs: list[dict], applier) -> str:
    """寫入 → push → 稽核。回傳給使用者看的結果摘要。

    先全部寫完 DB 再 push 一次，而不是每改一筆 push 一次：push 本來就是全平台
    鏡像，逐筆 push 只會把同樣的工作重做 N 次。
    """
    applied = []
    for d in diffs:
        result = applier(d)
        applied.append(result)
        write_audit(admin, action, d["id"], "success", {
            "before": result["before"],
            "after": result["after"],
            "added": result["added"],
            "removed": result["removed"],
            "headcount": d["headcount"],
        })

    push = await model_access_service.push_now(dry_run=False)
    changed = [r for r in push.get("results", []) if r.get("action") not in (None, "unchanged")]
    failed = [r for r in push.get("results", []) if r.get("error")]
    write_audit(admin, "push_model_access", "portal-a", "failed" if failed else "success", {
        "changed_models": len(changed),
        "failed_models": [r["model"] for r in failed],
        "missing_groups": push.get("missing_groups", []),
        "missing_users": push.get("missing_users", []),
    })

    summary = f"已寫入 {len(applied)} 個對象，並把 {len(changed)} 個模型的授權鏡像回 OpenWebUI。"
    if failed:
        summary += f' 其中 {len(failed)} 個模型鏡像失敗：{", ".join(r["model"] for r in failed)}。'
    if push.get("missing_groups"):
        summary += f' 這些部門在 OpenWebUI 找不到對應群組，授權沒有生效：{", ".join(push["missing_groups"])}。'
    if push.get("missing_users"):
        summary += (
            f' 這些使用者在 OpenWebUI 對映不到帳號（多半是還沒用 SSO 登入過），'
            f'個人授權沒有生效：{", ".join(push["missing_users"])}。'
        )
    return summary


def _result_page(nav_key: str, summary: str, back: str):
    return _page(f"""
{_nav(nav_key)}
<h2>已生效</h2>
<p>{html.escape(summary)}</p>
<p class="hint">第二個 OpenWebUI 入口（如果有啟用）是唯讀鏡像，由 CronJob 每 2 分鐘自動對齊，
最慢 2 分鐘後也會跟上。</p>
<p><a class="btn" href="{back}">« 回授權頁</a></p>
""")


# ── 部門 × 模型矩陣 ───────────────────────────────────────────────────────────

@router.get("/access")
async def access_matrix(admin: dict = Depends(require_admin)):
    model_ids, metadata = await _known_models()
    depts = model_access_service.dept_models()

    editable = [d for d in depts if "*" not in d["allowed_models"]]
    wildcard = [d for d in depts if "*" in d["allowed_models"]]

    header = "".join(_model_header(m, metadata) for m in model_ids)
    rows = []
    for d in editable:
        allowed = set(d["allowed_models"])
        cells = "".join(
            f'<td style="text-align:center"><input type="checkbox" name="grants" '
            f'value="{html.escape(d["dept_id"] + _GRANT_SEP + m)}"'
            f'{" checked" if m in allowed else ""}></td>'
            for m in model_ids
        )
        stale = sorted(allowed - set(model_ids))
        stale_note = (
            f'<br><span class="err" title="這些名字在 LiteLLM 找不到，儲存時會被清掉">'
            f'失效：{html.escape(", ".join(stale))}</span>' if stale else ""
        )
        rows.append(
            f'<tr><td style="white-space:nowrap"><b>{html.escape(d["dept_id"])}</b>'
            f'<br><span class="hint">{html.escape(d["dept_name"])}／{d["headcount"]} 人</span>'
            f'{stale_note}</td>{cells}</tr>'
        )

    wildcard_block = ""
    if wildcard:
        names = ", ".join(f'{d["dept_id"]}（{d["headcount"]} 人）' for d in wildcard)
        wildcard_block = f"""
<p class="warn"><b>以下部門的 allowed_models 是 <code>*</code>（不限制，所有模型都能用），
不列在矩陣裡</b>：{html.escape(names)}。<br>
在矩陣裡編輯它們會把 <code>*</code> 換成一份逐筆清單、語意完全不同，所以這裡刻意不提供——
真的要改請走 <code>ADMIN_API_KEY</code> 的 curl 路徑。</p>"""

    return _page(f"""
{_nav('access')}
<h2>模型授權</h2>
<p class="hint">勾選＝該部門可以使用該模型。按「預覽變更」會先算出差異給你看，
確認之後才寫入，並立刻鏡像回 OpenWebUI（使用者端即時生效，不用等 2 分鐘的排程）。
個人層級的額外授權請用下方的搜尋。</p>
{wildcard_block}
<form method="post" action="{PREFIX}/access/departments/preview">
  <p>
    <button type="button" onclick="document.querySelectorAll('input[name=grants]').forEach(c=>c.checked=true)">全選</button>
    <button type="button" onclick="document.querySelectorAll('input[name=grants]').forEach(c=>c.checked=false)">全不選</button>
  </p>
  <div class="wide"><table>
    <tr><th>部門</th>{header}</tr>
    {''.join(rows) if rows else f'<tr><td colspan="{len(model_ids) + 1}">沒有可編輯的部門。</td></tr>'}
  </table></div>
  <button type="submit">預覽變更</button>
</form>

<h3>個人授權</h3>
<p class="hint">個人授權是<b>加在部門授權之上</b>的（兩者聯集），用來處理少數需要額外模型的人。
使用者有數百位，這裡用搜尋而不是列全表——一頁幾百個 checkbox 只會更容易點錯。</p>
<form method="get" action="{PREFIX}/access/users">
  <p><label>搜尋使用者（email／user_id／key 名稱）<br>
     <input type="text" name="q" required></label></p>
  <button type="submit">搜尋</button>
</form>
""")


@router.post("/access/departments/preview")
async def preview_departments(admin: dict = Depends(require_admin), grants: list[str] = Form(default=[])):
    model_ids, _ = await _known_models()
    depts = model_access_service.dept_models()
    editable = {d["dept_id"] for d in depts if "*" not in d["allowed_models"]}
    desired = _parse_grants(grants, editable)

    # 刻意不先用「& 已知模型」把未知名稱濾掉——那會讓 validate_models 的 422 變成
    # 永遠觸發不到的死碼，未知的授權就被靜默吞掉了。矩陣只為已知模型渲染 checkbox，
    # 所以正常操作不會踩到這裡；會踩到就代表表單被改過，或這個模型在頁面開著的
    # 期間被刪掉了，兩種都該在預覽階段就講清楚，而不是讓人按下確認才發現。
    known = set(model_ids)
    for models in desired.values():
        model_access_service.validate_models(models, known)

    diffs = [
        diff for dept_id, models in sorted(desired.items())
        if (diff := model_access_service.preview_dept(dept_id, sorted(set(models))))["changed"]
    ]
    if not diffs:
        return _no_change_page("access", f"{PREFIX}/access")

    hidden = "".join(
        f'<input type="hidden" name="grants" value="{html.escape(dept_id + _GRANT_SEP + m)}">'
        for dept_id, models in sorted(desired.items()) for m in sorted(models)
    )
    # 全部取消勾選的部門在上面產不出任何 hidden 欄位，apply 時會分不出「這個部門
    # 沒送出」跟「這個部門要清空」——用一個明確的名單把範圍帶過去。
    hidden += "".join(
        f'<input type="hidden" name="scope" value="{html.escape(d)}">' for d in sorted(editable)
    )
    return _confirm_page("access", "確認部門授權變更", diffs, hidden, f"{PREFIX}/access/departments/apply")


@router.post("/access/departments/apply")
async def apply_departments(
    admin: dict = Depends(require_admin),
    grants: list[str] = Form(default=[]),
    scope: list[str] = Form(default=[]),
):
    model_ids, _ = await _known_models()
    known = set(model_ids)
    desired = _parse_grants(grants, set(scope))

    for models in desired.values():
        model_access_service.validate_models(models, known)

    diffs = [
        diff for dept_id, models in sorted(desired.items())
        if (diff := model_access_service.preview_dept(dept_id, sorted(set(models))))["changed"]
    ]
    if not diffs:
        return _no_change_page("access", f"{PREFIX}/access")

    summary = await _apply_and_push(
        admin, "set_dept_models", diffs,
        lambda d: model_access_service.apply_dept(d["id"], d["after"], known),
    )
    return _result_page("access", summary, f"{PREFIX}/access")


# ── 個人授權 ──────────────────────────────────────────────────────────────────

@router.get("/access/users")
def user_search(admin: dict = Depends(require_admin), q: str = ""):
    results = model_access_service.search_users(q)
    rows = "".join(
        f'<tr><td><a href="{PREFIX}/access/users/edit?user_id={quote(u["user_id"], safe="")}">'
        f'{html.escape(u["user_email"] or u["user_id"])}</a></td>'
        f'<td>{html.escape(u["dept_id"])}</td>'
        f'<td>{html.escape(u["account_type"])}{"／已停權" if u["blocked"] else ""}</td>'
        f'<td class="hint">{html.escape(", ".join(u["models"]) or "（無個人授權）")}</td></tr>'
        for u in results
    )
    return _page(f"""
{_nav('access')}
<h2>個人授權：搜尋結果</h2>
<form method="get" action="{PREFIX}/access/users">
  <p><label>搜尋使用者（email／user_id／key 名稱）<br>
     <input type="text" name="q" value="{html.escape(q)}" required></label></p>
  <button type="submit">搜尋</button>
</form>
<p class="hint">最多顯示 50 筆。「個人授權」欄位只列個人額外授權，不含部門本來就有的。</p>
<table>
  <tr><th>使用者</th><th>部門</th><th>類型</th><th>個人授權</th></tr>
  {rows or '<tr><td colspan="4">沒有符合的使用者。</td></tr>'}
</table>
<p><a href="{PREFIX}/access">« 回授權頁</a></p>
""")


@router.get("/access/users/edit")
async def user_edit_form(user_id: str, admin: dict = Depends(require_admin)):
    model_ids, metadata = await _known_models()
    user = model_access_service.get_user(user_id)
    dept = model_access_service.get_dept(user["dept_id"])
    dept_allowed = set(dept["allowed_models"])
    personal = set(user["models"])

    if "*" in personal:
        raise HTTPException(
            status_code=409,
            detail=f"'{user_id}' 的個人授權是 *（不限制），語意跟逐筆清單不同，"
            "這個畫面不提供編輯——要改請走 ADMIN_API_KEY 的 curl 路徑。",
        )

    dept_note = (
        "這個部門的授權是 <code>*</code>（不限制），下面所有模型他本來就能用，個人授權沒有實際差別。"
        if "*" in dept_allowed else ""
    )
    rows = "".join(
        f'<tr><td><input type="checkbox" name="models" value="{html.escape(m)}"'
        f'{" checked" if m in personal else ""}></td>'
        f'<td><code>{html.escape(m)}</code> '
        f'{status_badge(metadata[m]) if m in metadata and metadata[m].get("status") != "published" else ""}</td>'
        f'<td class="hint">{"部門已授權" if m in dept_allowed or "*" in dept_allowed else ""}</td></tr>'
        for m in model_ids
    )
    stale = sorted(personal - set(model_ids))
    stale_note = (
        f'<p class="err">這些個人授權在 LiteLLM 找不到，儲存時會被清掉：{html.escape(", ".join(stale))}</p>'
        if stale else ""
    )

    return _page(f"""
{_nav('access')}
<h2>個人授權：{html.escape(user["user_email"] or user_id)}</h2>
<table>
  <tr><td>user_id</td><td><code>{html.escape(user_id)}</code></td></tr>
  <tr><td>部門</td><td>{html.escape(user["dept_id"])}｜{html.escape(dept["dept_name"])}</td></tr>
  <tr><td>部門已授權的模型</td><td class="hint">{html.escape(", ".join(sorted(dept_allowed)) or "（無）")}</td></tr>
</table>
<p class="hint">{dept_note or "個人授權跟部門授權是聯集：這裡勾的是「部門沒有、但這個人要額外拿到」的模型。取消勾選部門本來就有的模型不會讓他失去存取權。"}</p>
{stale_note}
<form method="post" action="{PREFIX}/access/users/preview">
  <input type="hidden" name="user_id" value="{html.escape(user_id)}">
  <table><tr><th></th><th>模型</th><th></th></tr>{rows}</table>
  <button type="submit">預覽變更</button>
</form>
<p><a href="{PREFIX}/access">« 回授權頁</a></p>
""")


@router.post("/access/users/preview")
async def preview_user(
    admin: dict = Depends(require_admin), user_id: str = Form(...), models: list[str] = Form(default=[]),
):
    model_ids, _ = await _known_models()
    desired = model_access_service.validate_models(models, set(model_ids))
    diff = model_access_service.preview_user(user_id, desired)
    back = f'{PREFIX}/access/users/edit?user_id={quote(user_id, safe="")}'
    if not diff["changed"]:
        return _no_change_page("access", back)

    hidden = f'<input type="hidden" name="user_id" value="{html.escape(user_id)}">' + "".join(
        f'<input type="hidden" name="models" value="{html.escape(m)}">' for m in desired
    )
    return _confirm_page("access", "確認個人授權變更", [diff], hidden, f"{PREFIX}/access/users/apply")


@router.post("/access/users/apply")
async def apply_user(
    admin: dict = Depends(require_admin), user_id: str = Form(...), models: list[str] = Form(default=[]),
):
    model_ids, _ = await _known_models()
    known = set(model_ids)
    desired = model_access_service.validate_models(models, known)
    diff = model_access_service.preview_user(user_id, desired)
    back = f'{PREFIX}/access/users/edit?user_id={quote(user_id, safe="")}'
    if not diff["changed"]:
        return _no_change_page("access", back)

    summary = await _apply_and_push(
        admin, "set_user_models", [diff],
        lambda d: model_access_service.apply_user(d["id"], d["after"], known),
    )
    return _result_page("access", summary, back)
