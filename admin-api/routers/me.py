"""自助端點：使用者用自己的 Keycloak token 查詢/重設自己的 LiteLLM API key。

跟 /api/v1/users 的差別是認證方式——這裡不吃 ADMIN_API_KEY，而是吃使用者自己的
Keycloak access token，並且只能操作 token 對應到的那一個帳號，拿不到別人的資料。
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import DB_PATH, bump_version, get_conn, row_to_dict
from keycloak import fetch_userinfo
from models import MeOut

router = APIRouter(prefix="/api/v1/me")

_bearer = HTTPBearer()


def _effective_models(user_models: list[str], dept_allowed: list[str]) -> list[str]:
    """部門 ∪ 個人 的聯集，任一邊含 "*" 則回 ["*"]。

    這只是給使用者看的資訊；真正的 enforcement 在 config/custom_auth.py 的
    _effective_models()，兩邊邏輯要保持一致。
    """
    if "*" in dept_allowed or "*" in user_models:
        return ["*"]
    return list(dict.fromkeys([*dept_allowed, *user_models]))


def resolve_db_user(claims: dict) -> dict | None:
    """Keycloak userinfo claims → DB 使用者，不存在時回 None（不拋例外）。

    以 sub 對 users.user_id 為主（兩者本來就是同一個值，由 Keycloak webhook 寫入）；
    沒命中時退回用 email 比對，涵蓋「Keycloak 帳號重建過導致 sub 換掉」的情況。

    回傳 None、而非直接拋 HTTPException：JSON API（`current_user`）與網頁流程
    （routers/me_web.py）對「查無帳號」要呈現的東西不同（前者回 404 JSON，後者要
    渲染一個看得懂的錯誤頁面），錯誤呈現方式留給呼叫端決定。
    """
    sub = (claims.get("sub") or "").strip()
    email = (claims.get("email") or "").strip()

    with get_conn(DB_PATH) as conn:
        row = None
        if sub:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (sub,)
            ).fetchone()
        if row is None and email:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(user_email) = lower(?)", (email,)
            ).fetchone()

    return row_to_dict(row) if row is not None else None


async def current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """Keycloak access token → DB 使用者（JSON API 用）。"""
    claims = await fetch_userinfo(credentials.credentials)
    record = resolve_db_user(claims)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Your Keycloak identity has no account on this platform yet",
        )
    if record["blocked"]:
        raise HTTPException(status_code=403, detail="Your account is blocked")
    return record


def to_me_out(conn, record: dict) -> MeOut:
    """DB record → MeOut。routers/me_web.py 也用這個組出網頁要顯示的欄位。"""
    dept = conn.execute(
        "SELECT allowed_models FROM departments WHERE dept_id = ?",
        (record["dept_id"],),
    ).fetchone()
    dept_allowed = json.loads(dept["allowed_models"]) if dept else []
    user_models = json.loads(record.get("models") or "[]")
    return MeOut(
        user_id=record["user_id"],
        key_name=record["key_name"],
        user_email=record.get("user_email"),
        dept_id=record["dept_id"],
        api_key=record["api_key"],
        allowed_models=_effective_models(user_models, dept_allowed),
        blocked=bool(record["blocked"]),
    )


def regenerate_key_for(conn, user_id: str) -> MeOut:
    """重設指定使用者的 API key 並回傳更新後的 MeOut。共用給 JSON API 與網頁流程。"""
    new_key = f"sk-{uuid.uuid4().hex}"
    conn.execute(
        "UPDATE users SET api_key=?, updated_at=datetime('now') WHERE user_id=?",
        (new_key, user_id),
    )
    bump_version(conn)
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return to_me_out(conn, row_to_dict(row))


@router.get("", response_model=MeOut)
def get_me(record: dict = Depends(current_user)):
    """查詢自己的 API key 與可用模型。"""
    with get_conn(DB_PATH) as conn:
        return to_me_out(conn, record)


@router.post("/regenerate-key", response_model=MeOut)
def regenerate_own_key(record: dict = Depends(current_user)):
    """重設自己的 API key。舊 key 立即失效，所有填了舊 key 的工具都要更新。"""
    with get_conn(DB_PATH) as conn:
        return regenerate_key_for(conn, record["user_id"])
