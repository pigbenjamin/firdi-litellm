"""admin-web 寫入操作的稽核紀錄（R-39）。

admin-api 目前完全沒有操作稽核——反正原本只有一把共享 ADMIN_API_KEY、一個人在用。
UI 化之後會有具名身分（Keycloak 帳號）在操作，「誰改了什麼」才開始有意義。
寫法照 config/custom_logger.py 的 jsonl 樣式；R-40：不記 key 內容，只記末四碼。
"""
import csv
import io
import json
import os
from datetime import datetime, timezone
from threading import Lock

_WRITE_LOCK = Lock()

# WP5：動作代碼 → 中文標籤。查詢頁的下拉選單與匯出的「動作」欄都用這份對照，
# 查不到的代碼（例如 set_provider_key:openai 這種帶參數的）原樣顯示。
ACTIONS = {
    "create_external_model": "上架模型",
    "update_draft_model": "編輯草稿模型",
    "update_model_fields": "修改模型描述欄位",
    "publish_model": "發布模型",
    "disable_model": "停用模型",
    "enable_model": "重新啟用模型",
    "delete_external_model": "下架（硬刪除）模型",
    "test_model": "測試呼叫",
    "save_model_preset": "儲存上架範本",
    "delete_model_preset": "刪除上架範本",
    "set_dept_models": "設定部門模型授權",
    "set_user_models": "設定個人模型授權",
    "push_model_access": "推送授權到 OpenWebUI",
    "sync_permissions": "從 OpenWebUI 拉回授權",
}


def action_label(action: str) -> str:
    if action in ACTIONS:
        return ACTIONS[action]
    base, _, param = action.partition(":")
    if base == "set_provider_key":
        return f"設定部門 Provider Key（{param}）"
    return action


def write_audit(actor: dict, action: str, target: str, result: str, detail: dict | None = None) -> None:
    """WP5：detail 一律要帶 before／after（沒有變更前狀態的操作才可以省略），
    查詢頁與 CSV 匯出把這兩個鍵拆成獨立欄位（見 detail_parts）。
    """
    path = _audit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor_username": actor.get("preferred_username"),
        "actor_sub": actor.get("sub"),
        "actor_email": actor.get("email"),
        "action": action,
        "target": target,
        "result": result,
    }
    if detail:
        record["detail"] = detail
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def mask_key(key: str) -> str:
    """R-40：稽核紀錄裡的 key 只留末四碼，不記內容。"""
    if not key:
        return ""
    return f"...{key[-4:]}" if len(key) > 4 else "****"


# ── WP5：查詢與匯出 ───────────────────────────────────────────────────────────
#
# 寫入量很低（只有管理者的手動操作會寫），直接線性掃描 jsonl 就夠，不值得為了
# 查詢另開一張表——多一份資料就多一個會跟 jsonl 對不起來的地方。

def _audit_path() -> str:
    return os.getenv("ADMIN_AUDIT_LOG_PATH", "/app/logs/admin-web-audit.jsonl")


def read_audit(
    start: str = "", end: str = "", actor: str = "", action: str = "",
    target: str = "", limit: int = 500,
) -> tuple[list[dict], int]:
    """回傳 (符合條件的紀錄, 符合條件的總筆數)，新的在前。

    start／end 是 YYYY-MM-DD（含當天；end 比對到當天 23:59:59）。時間戳是 UTC
    ISO 字串，字典序跟時間序一致，所以直接用字串前綴比大小就對了。
    """
    path = _audit_path()
    if not os.path.exists(path):
        return [], 0

    lo = f"{start}T00:00:00" if start else ""
    hi = f"{end}T23:59:59.999999+00:00" if end else ""
    actor_q, target_q = actor.strip().lower(), target.strip().lower()

    matched = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 壞掉的一行不該讓整頁查不出來
            ts = rec.get("timestamp") or ""
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
            if actor_q and actor_q not in (rec.get("actor_username") or "").lower():
                continue
            if action and rec.get("action") != action:
                continue
            if target_q and target_q not in (rec.get("target") or "").lower():
                continue
            matched.append(rec)

    matched.reverse()
    return matched[:limit], len(matched)


CSV_HEADERS = ["時間（UTC）", "操作者", "Email", "動作", "目標", "結果", "變更前", "變更後", "其他細節"]


def detail_parts(detail) -> tuple[str, str, str]:
    """把 detail 拆成「變更前 / 變更後 / 其他」三欄。before/after 是 WP5 的標準
    欄位（每個寫入操作都要帶），其餘鍵原樣塞進第三欄。
    """
    if not isinstance(detail, dict):
        return "", "", json.dumps(detail, ensure_ascii=False) if detail else ""
    before = detail.get("before")
    after = detail.get("after")
    rest = {k: v for k, v in detail.items() if k not in ("before", "after")}

    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    return _fmt(before), _fmt(after), json.dumps(rest, ensure_ascii=False) if rest else ""


def to_csv(records: list[dict]) -> bytes:
    """UTF-8 with BOM——Excel 開沒有 BOM 的 UTF-8 CSV 會把中文變亂碼。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for r in records:
        before, after, rest = detail_parts(r.get("detail"))
        writer.writerow([
            r.get("timestamp", ""),
            r.get("actor_username", ""),
            r.get("actor_email", ""),
            action_label(r.get("action", "")),
            r.get("target", ""),
            r.get("result", ""),
            before, after, rest,
        ])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
