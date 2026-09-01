"""模型授權（WP4）：部門與個人授權，存檔即生效。

畫面分成「唯讀總覽」與「一次一個部門的編輯」兩層——原本是一頁大矩陣，改的理由
寫在 dept_edit_form 的 docstring 裡。

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


# 上游代碼 → 給人看的分組標題。沒列到的代碼直接原樣顯示（新增上游時不會漏掉模型）。
_UPSTREAM_LABELS = {
    "vllm": "地端 vLLM（config/litellm_config.yaml 定義）",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
    "ollama": "Ollama",
    "marker": "文件轉檔（Marker）",
}


def _group_key(model_id: str, meta: dict | None) -> str:
    """模型 id → 分組代碼。

    以 model_name 的第一段為準（`openrouter/anthropic/claude-sonnet-4-5` → openrouter），
    因為那是使用者在畫面上看到的名字；沒有斜線的就是地端 YAML 模型，退回 metadata
    的 upstream，再退回 vllm。刻意不優先用 metadata：地端模型多半根本沒有紀錄。
    """
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    return (meta or {}).get("upstream") or "vllm"


def _grouped_models(
    model_ids: list[str], metadata: dict[str, dict]
) -> list[tuple[str, str, list[str]]]:
    """[(分組代碼, 標題, 模型清單)]，地端排最前面，其餘按標題排序。"""
    groups: dict[str, list[str]] = {}
    for m in model_ids:
        groups.setdefault(_group_key(m, metadata.get(m)), []).append(m)
    return sorted(
        ((k, _UPSTREAM_LABELS.get(k, k), sorted(v)) for k, v in groups.items()),
        key=lambda g: (g[0] != "vllm", g[1]),
    )


def _matrix_rows(depts: list[dict], model_ids: list[str]) -> str:
    """總覽頁那份唯讀矩陣。＊ 的部門用一格橫跨帶過，不假裝它是「每個都勾了」——
    語意是「不限制」，之後新上架的模型它也會自動有，逐格打勾看起來會像凍結的快照。
    """
    rows = []
    for d in depts:
        allowed = set(d["allowed_models"])
        head = (
            f'<td style="white-space:nowrap"><b>{html.escape(d["dept_id"])}</b>'
            f'<br><span class="hint">{html.escape(d["dept_name"])}／{d["headcount"]} 人</span></td>'
        )
        if "*" in allowed:
            cells = f'<td colspan="{max(len(model_ids), 1)}"><b>＊ 不限制</b>（所有模型，含之後新上架的）</td>'
        else:
            cells = "".join(
                f'<td class="on">✓</td>' if m in allowed else '<td></td>' for m in model_ids
            )
        rows.append(f"<tr>{head}{cells}</tr>")
    return "".join(rows) or f'<tr><td colspan="{len(model_ids) + 1}">目前沒有任何部門。</td></tr>'


# 兩個編輯頁（部門、個人）共用的即時差異。純前端，只讀 data-was（伺服器渲染時
# 就寫死的原設定），
# 所以怎麼亂點都回得去，而且 JS 掛掉也只是少了顏色提示，表單本身照常能送。
# 刻意寫成普通字串而不是 f-string：JS 的大括號在 f-string 裡要全部雙寫，
# 那是 118e8e2 修過的那種語法陷阱。
_DIFF_JS = """<script>
(function () {
  var form = document.getElementById('access-form');
  if (!form) return;
  var boxes = [].slice.call(form.querySelectorAll('input[type=checkbox]'));
  var summary = document.getElementById('diff-summary');
  var submit = document.getElementById('diff-submit');

  function paint() {
    var added = 0, removed = 0;
    boxes.forEach(function (box) {
      var was = box.dataset.was === '1';
      var row = box.closest('tr');
      var mark = row.querySelector('.mark');
      row.classList.remove('row-add', 'row-del');
      if (box.checked && !was) {
        added++; row.classList.add('row-add'); mark.textContent = '＋ 這次新增';
      } else if (!box.checked && was) {
        removed++; row.classList.add('row-del'); mark.textContent = '－ 這次收回';
      } else {
        mark.textContent = '';
      }
    });
    var total = added + removed;
    summary.textContent = total
      ? '這次變更：新增 ' + added + ' 個、收回 ' + removed + ' 個模型'
      : '目前跟原設定一樣，沒有變更';
    summary.className = total ? 'warn' : 'hint';
    submit.disabled = !total;
  }

  form.addEventListener('change', paint);
  form.addEventListener('click', function (e) {
    var act = e.target.dataset && e.target.dataset.act;
    if (!act) return;
    var group = e.target.dataset.group;
    boxes.forEach(function (box) {
      if (group && box.dataset.group !== group) return;
      if (act === 'all') box.checked = true;
      else if (act === 'none') box.checked = false;
      else if (act === 'reset') box.checked = box.dataset.was === '1';
    });
    paint();
  });

  paint();
})();
</script>"""


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


def _confirm_page(
    nav_key: str, title: str, diffs: list[dict], hidden_fields: str, apply_action: str,
    back: str = f"{PREFIX}/access",
) -> object:
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
<p><a href="{back}">« 取消，回上一頁</a></p>
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


# ── 部門授權：總覽（唯讀）＋ 一次一個部門的編輯 ─────────────────────────────────

@router.get("/access")
async def access_overview(admin: dict = Depends(require_admin)):
    """部門清單（現況一眼看完）＋ 可展開的唯讀矩陣。這一頁不能改任何東西。"""
    model_ids, metadata = await _known_models()
    depts = model_access_service.dept_models()
    known = set(model_ids)

    rows = []
    for d in depts:
        allowed = set(d["allowed_models"])
        if "*" in allowed:
            # ＊ 是「不限制」的既有語意，跟逐筆清單完全不同（見下方說明），
            # 所以連編輯入口都不給，避免有人按進去按了儲存就把語意換掉。
            models_cell = '<b>＊ 不限制</b><br><span class="hint">所有模型都能用</span>'
            action = '<span class="hint">唯讀</span>'
        else:
            chips = "".join(
                f'<span class="chip">{html.escape(m)}</span>' for m in sorted(allowed & known)
            ) + "".join(
                f'<span class="chip chip-stale" title="這個名字在 LiteLLM 找不到，'
                f'下次儲存時會被清掉">{html.escape(m)}</span>'
                for m in sorted(allowed - known)
            )
            models_cell = chips or '<span class="hint">（無任何授權）</span>'
            edit_url = f'{PREFIX}/access/dept/edit?dept_id={quote(d["dept_id"], safe="")}'
            action = f'<a class="btn" href="{edit_url}">編輯</a>'
        rows.append(
            f'<tr><td style="white-space:nowrap"><b>{html.escape(d["dept_id"])}</b>'
            f'<br><span class="hint">{html.escape(d["dept_name"])}</span></td>'
            f'<td style="white-space:nowrap">{d["headcount"]} 人</td>'
            f'<td>{models_cell}</td><td>{action}</td></tr>'
        )

    return _page(f"""
{_nav('access')}
<h2>模型授權</h2>
<p class="hint">這一頁是<b>唯讀現況</b>。要改授權請按該部門的「編輯」，一次改一個部門——
存檔前一定會先給你差異預覽，確認之後才寫入，並立刻鏡像回 OpenWebUI（使用者端即時生效，
不用等 2 分鐘的排程）。個人層級的額外授權請用下方的搜尋。</p>
<table>
  <tr><th>部門</th><th>人數</th><th>已授權的模型</th><th></th></tr>
  {''.join(rows) if rows else '<tr><td colspan="4">目前沒有任何部門。</td></tr>'}
</table>
<p class="hint"><code>＊ 不限制</code>的部門刻意不提供編輯：在矩陣或清單裡編輯它會把
<code>＊</code> 換成一份逐筆清單、語意完全不同（之後新上架的模型它就不會自動有了）。
真的要改請走 <code>ADMIN_API_KEY</code> 的 curl 路徑。</p>

<details>
  <summary>展開「部門 × 模型」總覽矩陣（唯讀，用來比對哪些部門有同一個模型）</summary>
  <div class="wide"><table class="matrix">
    <tr><th>部門</th>{''.join(_model_header(m, metadata) for m in model_ids)}</tr>
    {_matrix_rows(depts, model_ids)}
  </table></div>
</details>

<h3>個人授權</h3>
<p class="hint">個人授權是<b>加在部門授權之上</b>的（兩者聯集），用來處理少數需要額外模型的人。
使用者有數百位，這裡用搜尋而不是列全表——一頁幾百個 checkbox 只會更容易點錯。</p>
<form method="get" action="{PREFIX}/access/users">
  <p><label>搜尋使用者（email／user_id／key 名稱）<br>
     <input type="text" name="q" required></label></p>
  <button type="submit">搜尋</button>
</form>
""")


@router.get("/access/dept/edit")
async def dept_edit_form(dept_id: str, admin: dict = Depends(require_admin)):
    """單一部門的授權編輯：模型縱向排列、按上游分組，每一列都標「原本」是什麼。

    這裡刻意不做「全部門一起編輯」的大矩陣（第二期原本的樣子）。理由有三個：
    模型數量會隨自助上架一直往右長、部門只有個位數，矩陣因此又寬又稀疏；橫向捲動
    之後表頭與部門名都不在視線內，是誤點的主因；而 push 是取代式的全平台鏡像，
    一次只動一個部門能把寫壞的影響面積縮到最小。全局比對的需求由總覽頁那份唯讀
    矩陣負責。
    """
    model_ids, metadata = await _known_models()
    dept = model_access_service.get_dept(dept_id)
    allowed = set(dept["allowed_models"])

    if "*" in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"'{dept_id}' 的授權是 ＊（不限制），語意跟逐筆清單不同，"
            "這個畫面不提供編輯——要改請走 ADMIN_API_KEY 的 curl 路徑。",
        )

    blocks = []
    for group_key, label, members in _grouped_models(model_ids, metadata):
        rows = "".join(
            f'<tr><td><input type="checkbox" name="grants" '
            f'value="{html.escape(dept_id + _GRANT_SEP + m)}" '
            f'data-was="{"1" if m in allowed else "0"}" '
            f'data-group="{html.escape(group_key)}"'
            f'{" checked" if m in allowed else ""}></td>'
            f'<td><code>{html.escape(m)}</code> '
            f'{status_badge(metadata[m]) if m in metadata and metadata[m].get("status") != "published" else ""}</td>'
            f'<td class="hint" style="white-space:nowrap">'
            f'{"原本：已授權" if m in allowed else "原本：未授權"}</td>'
            f'<td class="mark"></td></tr>'
            for m in members
        )
        blocks.append(f"""
<fieldset>
  <legend>{html.escape(label)}（{len(members)} 個模型）</legend>
  <p>
    <button type="button" data-act="all" data-group="{html.escape(group_key)}">本組全選</button>
    <button type="button" data-act="none" data-group="{html.escape(group_key)}">本組全不選</button>
  </p>
  <table>
    <tr><th></th><th>模型</th><th>原本</th><th>這次的變更</th></tr>
    {rows}
  </table>
</fieldset>""")

    stale = sorted(allowed - set(model_ids))
    stale_note = (
        f'<p class="err">這些授權在 LiteLLM 找不到（已停用、已刪除或名稱拼錯），'
        f'儲存時會被一併清掉：{html.escape(", ".join(stale))}</p>' if stale else ""
    )

    return _page(f"""
{_nav('access')}
<h2>部門授權：{html.escape(dept_id)}</h2>
<table>
  <tr><td>部門名稱</td><td>{html.escape(dept["dept_name"])}</td></tr>
  <tr><td>影響人數</td><td>{dept["headcount"]} 人</td></tr>
</table>
<p class="hint">勾選＝這個部門可以使用該模型。每一列都標了「原本」是什麼，所以按了全選之後
也還看得到原設定；真的按錯就按「還原成原設定」。按「預覽變更」會先算出差異給你看，
確認之後才寫入。</p>
{stale_note}
<form method="post" action="{PREFIX}/access/departments/preview" id="access-form">
  <input type="hidden" name="scope" value="{html.escape(dept_id)}">
  {''.join(blocks) if blocks else '<p>LiteLLM 目前沒有任何可授權的模型。</p>'}
  <div class="sticky-bar">
    <p id="diff-summary" class="hint">目前跟原設定一樣，沒有變更</p>
    <button type="submit" id="diff-submit">預覽變更</button>
    <button type="button" data-act="reset">還原成原設定</button>
    <a class="btn" href="{PREFIX}/access" style="margin-left:1rem">« 取消，回授權頁</a>
  </div>
</form>
{_DIFF_JS}
""")


def _dept_back(scope_ids: list[str]) -> str:
    """差異預覽／結果頁的「回上一頁」要回到剛剛編輯的那個部門，而不是總覽。

    只動一個部門時才回編輯頁；curl 一次帶多個部門的情況沒有單一來源頁，回總覽。
    """
    if len(scope_ids) == 1:
        return f'{PREFIX}/access/dept/edit?dept_id={quote(scope_ids[0], safe="")}'
    return f"{PREFIX}/access"


def _resolve_scope(scope: list[str], depts: list[dict]) -> list[str]:
    """這次寫入的「取代範圍」：範圍內沒被勾的模型就是要收回。

    沒帶 scope 的呼叫（既有的 curl 路徑）維持原語意＝所有可編輯的部門；帶了就
    只動那幾個，逐部門編輯頁靠這個才不會把別的部門一起清空。＊ 的部門一律排除。
    """
    editable = [d["dept_id"] for d in depts if "*" not in d["allowed_models"]]
    if not scope:
        return editable
    unknown = sorted(set(scope) - set(editable))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"這些部門不存在或授權是 ＊（不限制），不能用這個畫面編輯：{', '.join(unknown)}",
        )
    return sorted(set(scope))


def _dept_diffs(desired: dict[str, list[str]], known: set[str]) -> list[dict]:
    # 刻意不先用「& 已知模型」把未知名稱濾掉——那會讓 validate_models 的 422 變成
    # 永遠觸發不到的死碼，未知的授權就被靜默吞掉了。編輯頁只為 known 的模型渲染
    # checkbox，所以正常操作不會踩到這裡；會踩到就代表表單被改過，或這個模型在
    # 頁面開著的期間被刪掉了，兩種都該在預覽階段就講清楚。
    for models in desired.values():
        model_access_service.validate_models(models, known)
    return [
        diff for dept_id, models in sorted(desired.items())
        if (diff := model_access_service.preview_dept(dept_id, sorted(set(models))))["changed"]
    ]


@router.post("/access/departments/preview")
async def preview_departments(
    admin: dict = Depends(require_admin),
    grants: list[str] = Form(default=[]),
    scope: list[str] = Form(default=[]),
):
    model_ids, _ = await _known_models()
    scope_ids = _resolve_scope(scope, model_access_service.dept_models())
    back = _dept_back(scope_ids)
    desired = _parse_grants(grants, set(scope_ids))
    diffs = _dept_diffs(desired, set(model_ids))
    if not diffs:
        return _no_change_page("access", back)

    hidden = "".join(
        f'<input type="hidden" name="grants" value="{html.escape(dept_id + _GRANT_SEP + m)}">'
        for dept_id, models in sorted(desired.items()) for m in sorted(set(models))
    )
    # 全部取消勾選的部門在上面產不出任何 hidden 欄位，apply 時會分不出「這個部門
    # 沒送出」跟「這個部門要清空」——用一個明確的名單把範圍帶過去。
    hidden += "".join(
        f'<input type="hidden" name="scope" value="{html.escape(d)}">' for d in scope_ids
    )
    return _confirm_page(
        "access", "確認部門授權變更", diffs, hidden, f"{PREFIX}/access/departments/apply", back,
    )


@router.post("/access/departments/apply")
async def apply_departments(
    admin: dict = Depends(require_admin),
    grants: list[str] = Form(default=[]),
    scope: list[str] = Form(default=[]),
):
    model_ids, _ = await _known_models()
    known = set(model_ids)
    scope_ids = _resolve_scope(scope, model_access_service.dept_models())
    back = _dept_back(scope_ids)
    diffs = _dept_diffs(_parse_grants(grants, set(scope_ids)), known)
    if not diffs:
        return _no_change_page("access", back)

    summary = await _apply_and_push(
        admin, "set_dept_models", diffs,
        lambda d: model_access_service.apply_dept(d["id"], d["after"], known),
    )
    return _result_page("access", summary, back)


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
    # 跟部門編輯頁同一個形狀：按上游分組、每列標「原本」、動過的列上色。
    blocks = []
    for group_key, label, members in _grouped_models(model_ids, metadata):
        rows = "".join(
            f'<tr><td><input type="checkbox" name="models" value="{html.escape(m)}" '
            f'data-was="{"1" if m in personal else "0"}" '
            f'data-group="{html.escape(group_key)}"'
            f'{" checked" if m in personal else ""}></td>'
            f'<td><code>{html.escape(m)}</code> '
            f'{status_badge(metadata[m]) if m in metadata and metadata[m].get("status") != "published" else ""}</td>'
            f'<td class="hint" style="white-space:nowrap">'
            f'{"原本：已授權" if m in personal else "原本：未授權"}</td>'
            f'<td class="hint" style="white-space:nowrap">'
            f'{"部門已授權" if m in dept_allowed or "*" in dept_allowed else ""}</td>'
            f'<td class="mark"></td></tr>'
            for m in members
        )
        blocks.append(f"""
<fieldset>
  <legend>{html.escape(label)}（{len(members)} 個模型）</legend>
  <p>
    <button type="button" data-act="all" data-group="{html.escape(group_key)}">本組全選</button>
    <button type="button" data-act="none" data-group="{html.escape(group_key)}">本組全不選</button>
  </p>
  <table>
    <tr><th></th><th>模型</th><th>原本</th><th>部門</th><th>這次的變更</th></tr>
    {rows}
  </table>
</fieldset>""")
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
<form method="post" action="{PREFIX}/access/users/preview" id="access-form">
  <input type="hidden" name="user_id" value="{html.escape(user_id)}">
  {''.join(blocks) if blocks else '<p>LiteLLM 目前沒有任何可授權的模型。</p>'}
  <div class="sticky-bar">
    <p id="diff-summary" class="hint">目前跟原設定一樣，沒有變更</p>
    <button type="submit" id="diff-submit">預覽變更</button>
    <button type="button" data-act="reset">還原成原設定</button>
    <a class="btn" href="{PREFIX}/access" style="margin-left:1rem">« 取消，回授權頁</a>
  </div>
</form>
{_DIFF_JS}
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
    return _confirm_page(
        "access", "確認個人授權變更", [diff], hidden, f"{PREFIX}/access/users/apply", back,
    )


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
