"""模型管理面欄位（model_metadata）、用量累計（model_spend）、上架範本
（model_presets）的存取層，不帶認證。

跟其他 services/ 一樣刻意不吃 auth 參數：`routers/models.py`（ADMIN_API_KEY）與
admin-web 的模型頁（Keycloak session）各自認證後呼叫同一組函式（決策 C）。

為什麼這些欄位存這裡而不是 LiteLLM 的 model_info：跟決策 E 的 model_key_policies
同一個理由——`config/custom_auth.py` 在每個請求的熱路徑上讀的就是這顆 SQLite，
狀態閘門（draft/disabled 不放行）與額度上限都要在那裡判斷，塞進 LiteLLM 的
model_info 等於在熱路徑多一個 Postgres 相依。
"""
import json
from datetime import datetime, timezone

from fastapi import HTTPException

from database import DB_PATH, bump_version, get_conn, row_to_dict

MODEL_TYPES = {
    "chat": "對話（chat/completions）",
    "embedding": "向量（embeddings）",
    "rerank": "重排序（rerank）",
}

STATUSES = {
    "draft": "草稿",
    "published": "已發布",
    "disabled": "已停用",
}

BUDGET_PERIODS = {
    "monthly": "每月（每月 1 日 UTC 歸零）",
    "total": "累計（不歸零）",
}

# 沒有 model_metadata 紀錄的 model_name 一律套用這組預設值。status 是 published
# 而不是 draft——這個表是後來才加的，YAML model_list 定義的地端模型與決策 E 之前
# 就上架的外部模型都不會有紀錄，預設成 draft 會讓它們在下次部署後全部被
# custom_auth 的狀態閘門擋掉。零資料回填的代價是這些既有模型不受狀態機管理，
# 需要時可從 admin-web 補一筆設定。
DEFAULTS = {
    "display_name": "",
    "model_type": "chat",
    "cost_center": "",
    "budget_limit_usd": None,
    "budget_enforce": 0,
    "budget_period": "monthly",
    "points_per_1k_prompt": None,
    "points_per_1k_completion": None,
    "notes": "",
    "status": "published",
    "upstream": "",
    "litellm_model": "",
    "api_base": None,
    "api_key": "",
    "last_test_ok": None,
    "last_test_at": None,
    "last_test_result": "",
    "created_at": None,
    "updated_at": None,
}

_WRITABLE = [
    "display_name", "model_type", "cost_center", "budget_limit_usd", "budget_enforce",
    "budget_period", "points_per_1k_prompt", "points_per_1k_completion", "notes",
    "status", "upstream", "litellm_model", "api_base",
    "api_key", "last_test_ok", "last_test_at", "last_test_result",
]

# 發布之後鎖定的「路由」欄位：這些一改，使用者當下打的就是另一個上游了。
# 描述性欄位（顯示名稱／備註／成本歸屬／額度／點數費率）在 published 狀態仍可改
# ——點數費率歸在描述性那一組是刻意的：它不影響請求打到哪裡去，而且改費率不該
# 需要先把模型停用。
ROUTING_FIELDS = ["upstream", "litellm_model", "api_base", "api_key"]


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def validate_model_type(model_type: str) -> None:
    if model_type not in MODEL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"模型類型 '{model_type}' 不認得，只能是 {'／'.join(MODEL_TYPES)}",
        )


def validate_budget(limit: float | None, period: str, enforce: bool) -> None:
    if limit is not None and limit <= 0:
        raise HTTPException(status_code=422, detail="額度上限要大於 0；不設額度請留空")
    if period not in BUDGET_PERIODS:
        raise HTTPException(
            status_code=422,
            detail=f"額度週期 '{period}' 不認得，只能是 {'／'.join(BUDGET_PERIODS)}",
        )
    if enforce and limit is None:
        raise HTTPException(status_code=422, detail="要「超額擋下來」就必須填額度上限")


def validate_points(prompt_rate: float | None, completion_rate: float | None) -> None:
    """點數費率只驗證「不是負數」。

    這兩個欄位是**純記錄**：扣點與部門／人員的點數上限由外部系統處理，本平台不
    累計、不檢查、不擋（config/custom_auth.py 與 config/custom_logger.py 完全不看
    它們）。所以這裡刻意不強制必填、也不設上限——費率規則是外部系統的事，這邊
    多加規則只會擋住還沒定案的填法。

    留空是 None 而不是 0：0 在外部系統眼裡是「這個模型免費」，跟「還沒填」差很多。
    """
    for label, value in (("輸入", prompt_rate), ("輸出", completion_rate)):
        if value is not None and value < 0:
            raise HTTPException(
                status_code=422, detail=f"{label}點數費率不可為負數；不計點請留空"
            )


# ── model_metadata ────────────────────────────────────────────────────────────

def _row_to_meta(row) -> dict:
    meta = row_to_dict(row)
    meta["budget_enforce"] = int(meta.get("budget_enforce") or 0)
    if meta.get("last_test_ok") is not None:
        meta["last_test_ok"] = int(meta["last_test_ok"])
    meta["has_record"] = True
    return meta


def synthesized(model_name: str) -> dict:
    """沒有紀錄時回傳的合成值，欄位跟真的有紀錄時完全一致，呼叫端不用分辨。"""
    meta = dict(DEFAULTS)
    meta["model_name"] = model_name
    meta["has_record"] = False
    meta["yaml_managed"] = False   # 由 models_service 依 LiteLLM 的 db_model 覆寫
    return meta


def get_metadata(model_name: str) -> dict:
    with get_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT * FROM model_metadata WHERE model_name = ?", (model_name,)
        ).fetchone()
    return _row_to_meta(row) if row else synthesized(model_name)


def list_metadata() -> dict[str, dict]:
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM model_metadata").fetchall()
    return {r["model_name"]: _row_to_meta(r) for r in rows}


def upsert_metadata(model_name: str, **fields) -> dict:
    """只寫進來的欄位；沒提到的維持原值（新紀錄則用 DEFAULTS）。

    bump_version 讓 config/custom_auth.py 的快取立刻失效——status 與額度設定都在
    那條熱路徑上判斷，改完要能盡快生效（版本戳記命中時是下一個請求就生效，
    最差是 30 秒 TTL）。
    """
    unknown = set(fields) - set(_WRITABLE)
    if unknown:
        raise ValueError(f"不可寫入的欄位：{sorted(unknown)}")

    with get_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT * FROM model_metadata WHERE model_name = ?", (model_name,)
        ).fetchone()
        if row is None:
            merged = {k: DEFAULTS[k] for k in _WRITABLE}
            merged.update(fields)
            cols = ", ".join(["model_name"] + _WRITABLE)
            marks = ", ".join(["?"] * (len(_WRITABLE) + 1))
            conn.execute(
                f"INSERT INTO model_metadata ({cols}) VALUES ({marks})",
                [model_name] + [merged[k] for k in _WRITABLE],
            )
        elif fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE model_metadata SET {sets}, updated_at=datetime('now') WHERE model_name=?",
                list(fields.values()) + [model_name],
            )
        bump_version(conn)
        return _row_to_meta(
            conn.execute("SELECT * FROM model_metadata WHERE model_name = ?", (model_name,)).fetchone()
        )


def delete_metadata(model_name: str) -> None:
    """硬刪除模型時才呼叫；停用（disabled）一定要保留紀錄，否則重新啟用時
    upstream/litellm_model/api_key 全部拿不回來（LiteLLM 的 /model/info 會遮罩 key）。
    用量累計（model_spend）刻意保留，避免刪掉再上架同名模型就把歷史花費歸零。
    """
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM model_metadata WHERE model_name=?", (model_name,))
        bump_version(conn)


# ── model_spend ───────────────────────────────────────────────────────────────

def _empty_spend(period: str) -> dict:
    return {"period": period, "monthly": 0.0, "calls": 0, "total": 0.0}


def list_spend() -> dict[str, dict]:
    """model_name → 用量。一次撈完整張表，給模型清單頁用。

    模型清單一頁會列十幾筆，逐筆呼叫 get_spend 就是十幾次開關連線；這張表的資料量
    是「模型數 × 2」，直接全撈比較划算。
    """
    period = current_period()
    out: dict[str, dict] = {}
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT model_name, period, spend_usd, calls FROM model_spend WHERE period IN (?, 'total')",
            (period,),
        ).fetchall()
    for r in rows:
        entry = out.setdefault(r["model_name"], _empty_spend(period))
        if r["period"] == "total":
            entry["total"] = float(r["spend_usd"] or 0)
        else:
            entry["monthly"] = float(r["spend_usd"] or 0)
            entry["calls"] = int(r["calls"] or 0)
    return out


def get_spend(model_name: str) -> dict:
    """回傳 {"monthly": 本月花費, "total": 累計花費, "calls": 本月呼叫次數, "period": "YYYY-MM"}。"""
    period = current_period()
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT period, spend_usd, calls FROM model_spend WHERE model_name=? AND period IN (?, 'total')",
            (model_name, period),
        ).fetchall()
    by_period = {r["period"]: r for r in rows}
    monthly = by_period.get(period)
    total = by_period.get("total")
    return {
        "period": period,
        "monthly": float(monthly["spend_usd"]) if monthly else 0.0,
        "calls": int(monthly["calls"]) if monthly else 0,
        "total": float(total["spend_usd"]) if total else 0.0,
    }


def budget_state(meta: dict, spend: dict) -> dict:
    """把 model_metadata 的額度設定 + model_spend 的累計，算成一個給 UI 與稽核用的視圖。

    exceeded 是「已經超過」；enforced 才代表超過之後真的會被擋。兩者刻意分開，
    「只記錄不擋」的模型一樣看得到自己超額了。
    """
    limit = meta.get("budget_limit_usd")
    period = meta.get("budget_period") or "monthly"
    used = spend["total"] if period == "total" else spend["monthly"]
    enforced = bool(meta.get("budget_enforce")) and limit is not None
    return {
        "limit": limit,
        "used": used,
        "period": period,
        "enforced": enforced,
        "exceeded": limit is not None and used >= limit,
        "pct": (used / limit * 100) if limit else None,
    }


# ── model_presets（上架表單的常用範本）────────────────────────────────────────

def list_presets() -> list[dict]:
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT preset_name, payload, updated_at FROM model_presets ORDER BY preset_name"
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append({"preset_name": r["preset_name"], "payload": payload, "updated_at": r["updated_at"]})
    return out


def get_preset(preset_name: str) -> dict | None:
    for p in list_presets():
        if p["preset_name"] == preset_name:
            return p
    return None


def save_preset(preset_name: str, payload: dict) -> None:
    """範本刻意不存 api_key——範本會被列出來、會被別人套用，不該夾帶祕密。"""
    name = preset_name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="範本名稱不可留空")
    safe = {k: v for k, v in payload.items() if k != "api_key"}
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO model_presets (preset_name, payload) VALUES (?, ?) "
            "ON CONFLICT(preset_name) DO UPDATE SET payload=excluded.payload, updated_at=datetime('now')",
            (name, json.dumps(safe, ensure_ascii=False)),
        )


def delete_preset(preset_name: str) -> None:
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM model_presets WHERE preset_name=?", (preset_name,))
