"""模型授權（哪些部門／使用者能用哪個模型）的寫入邏輯，不帶認證。

這是 docs/admin-web-plan.md 裡「第二期：翻轉授權權威來源」的落地。第一期刻意把
模型授權做成唯讀，理由是 OpenWebUI 是權威來源、寫 DB 會在下次 pull（≤2 分鐘）
被覆寫。第二期的做法不是把 pull 關掉，而是**寫完立刻 push 回 OpenWebUI**：

    PATCH DB → push_model_access_to_openwebui(target="a")

push 完 OpenWebUI 的狀態就等於 DB，之後不管 CronJob 什麼時候 pull 回來，結果都
一樣。原本文件裡的手動 SOP（pull → PATCH → push）就是這個順序，這裡只是把它包成
一次操作，讓「儲存」真的等於「生效」，而不是還要記得再去按一次同步。

**風險（admin-web-plan.md 已列為已知風險）**：push 是取代式的全平台鏡像，一次
錯誤的寫入可以清掉所有人的權限。所以這裡強制兩段式：呼叫端一定要先拿
`preview()` 的 dry-run 差異給人看過、確認之後才呼叫 `apply()`，且每次寫入都必須
帶 before/after 進稽核紀錄（WP5）。
"""
import json

from fastapi import HTTPException

from database import DB_PATH, bump_version, get_conn
from services import openwebui_sync_service


def _loads(raw) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


# ── 讀取現況 ──────────────────────────────────────────────────────────────────

def dept_models() -> list[dict]:
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT dept_id, dept_name, allowed_models FROM departments ORDER BY dept_id"
        ).fetchall()
        headcount = {
            r["dept_id"]: r["n"]
            for r in conn.execute(
                "SELECT dept_id, COUNT(*) AS n FROM users WHERE blocked=0 GROUP BY dept_id"
            ).fetchall()
        }
    return [
        {
            "dept_id": r["dept_id"],
            "dept_name": r["dept_name"],
            "allowed_models": _loads(r["allowed_models"]),
            "headcount": headcount.get(r["dept_id"], 0),
        }
        for r in rows
    ]


def get_dept(dept_id: str) -> dict:
    for d in dept_models():
        if d["dept_id"] == dept_id:
            return d
    raise HTTPException(status_code=404, detail=f"Department '{dept_id}' not found")


def search_users(query: str, limit: int = 50) -> list[dict]:
    """用 user_id／email／key_name 模糊比對。DB 裡有數百名使用者，個人授權頁刻意
    不做全表列出——先搜到人再編輯，避免一頁塞幾百個 checkbox 反而更容易點錯。
    """
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id, user_email, key_name, dept_id, models, blocked, account_type "
            "FROM users WHERE user_id LIKE ? OR user_email LIKE ? OR key_name LIKE ? "
            "ORDER BY user_email, user_id LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "user_email": r["user_email"],
            "key_name": r["key_name"],
            "dept_id": r["dept_id"],
            "models": _loads(r["models"]),
            "blocked": bool(r["blocked"]),
            "account_type": r["account_type"],
        }
        for r in rows
    ]


def get_user(user_id: str) -> dict:
    with get_conn(DB_PATH) as conn:
        r = conn.execute(
            "SELECT user_id, user_email, key_name, dept_id, models, blocked, account_type "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {
        "user_id": r["user_id"],
        "user_email": r["user_email"],
        "key_name": r["key_name"],
        "dept_id": r["dept_id"],
        "models": _loads(r["models"]),
        "blocked": bool(r["blocked"]),
        "account_type": r["account_type"],
    }


# ── 差異預覽（寫入前一定要先給人看）────────────────────────────────────────────

def _diff(before: list[str], after: list[str]) -> dict:
    b, a = set(before), set(after)
    return {
        "before": sorted(before),
        "after": sorted(after),
        "added": sorted(a - b),
        "removed": sorted(b - a),
        "changed": b != a,
    }


def preview_dept(dept_id: str, models: list[str]) -> dict:
    current = get_dept(dept_id)
    return {"kind": "department", "id": dept_id, "name": current["dept_name"],
            "headcount": current["headcount"], **_diff(current["allowed_models"], models)}


def preview_user(user_id: str, models: list[str]) -> dict:
    current = get_user(user_id)
    return {"kind": "user", "id": user_id, "name": current["user_email"] or user_id,
            "headcount": 1, **_diff(current["models"], models)}


# ── 寫入 + 立刻鏡像回 OpenWebUI ───────────────────────────────────────────────

def validate_models(models: list[str], known: set[str]) -> list[str]:
    """"*" 是「不限制」的既有語意，一律放行；其餘一定要對得上 LiteLLM 的模型清單。

    寫進去一個 LiteLLM 不存在的字串不會報錯，但 push 會靜默跳過它（push 以
    LiteLLM /models 過濾），變成「畫面上有、實際上沒有」的鬼授權——這正是客戶
    抱怨的那種無回饋失敗，所以在這裡就擋下來。

    **呼叫端不可以先用 `& known` 把未知的名稱濾掉再進來**，那會讓這個檢查變成
    永遠不會觸發的死碼。授權矩陣只會為 known 的模型渲染 checkbox，所以正常操作
    送上來的一定全是 known；真的出現未知名稱只有兩種情況——表單被改過，或這個
    模型在頁面開著的期間被別人刪掉了——兩種都該明講，不該靜默吞掉。
    （DB 裡本來就有的失效授權不會走到這裡：它沒有對應的 checkbox，所以不會出現
    在送上來的清單裡，會因為「不在變更後的清單中」而被正常清理。）
    """
    cleaned = [m.strip() for m in models if m and m.strip()]
    unknown = [m for m in cleaned if m != "*" and m not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"這些模型在 LiteLLM 找不到，無法授權：{', '.join(sorted(unknown))}"
            "（可能是已停用、已刪除，或名稱拼錯；如果你開著這個頁面很久了，請重新整理再試）",
        )
    return sorted(dict.fromkeys(cleaned))


def apply_dept(dept_id: str, models: list[str], known: set[str]) -> dict:
    diff = preview_dept(dept_id, validate_models(models, known))
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "UPDATE departments SET allowed_models=?, updated_at=datetime('now') WHERE dept_id=?",
            (json.dumps(diff["after"], ensure_ascii=False), dept_id),
        )
        bump_version(conn)
    return diff


def apply_user(user_id: str, models: list[str], known: set[str]) -> dict:
    diff = preview_user(user_id, validate_models(models, known))
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET models=?, updated_at=datetime('now') WHERE user_id=?",
            (json.dumps(diff["after"], ensure_ascii=False), user_id),
        )
        bump_version(conn)
    return diff


async def push_now(dry_run: bool = False) -> dict:
    """把 DB 的授權鏡像回權威入口 A。

    只 push A：入口 B 是唯讀鏡像，既有的 CronJob 每 2 分鐘會自動把 B 對齊，
    這裡不重複做（見 services/openwebui_sync_service.py 的模組註解）。
    """
    return await openwebui_sync_service.push_model_access_to_openwebui(target="a", dry_run=dry_run)
