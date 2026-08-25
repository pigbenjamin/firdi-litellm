"""admin-web 寫入操作的稽核紀錄（R-39）。

admin-api 目前完全沒有操作稽核——反正原本只有一把共享 ADMIN_API_KEY、一個人在用。
UI 化之後會有具名身分（Keycloak 帳號）在操作，「誰改了什麼」才開始有意義。
寫法照 config/custom_logger.py 的 jsonl 樣式；R-40：不記 key 內容，只記末四碼。
"""
import json
import os
from datetime import datetime, timezone
from threading import Lock

_WRITE_LOCK = Lock()


def write_audit(actor: dict, action: str, target: str, result: str, detail: dict | None = None) -> None:
    path = os.getenv("ADMIN_AUDIT_LOG_PATH", "/app/logs/admin-web-audit.jsonl")
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
