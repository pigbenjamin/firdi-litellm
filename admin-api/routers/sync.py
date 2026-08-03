import os
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from auth import verify_admin_key
from database import DB_PATH, bump_version, get_conn
from keycloak import (
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_REALM,
    KEYCLOAK_SSL_VERIFY,
    KEYCLOAK_URL,
)

router = APIRouter(prefix="/api/v1/sync")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

_token_cache: dict = {"access_token": None, "expires_at": 0.0}


async def _get_admin_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    async with httpx.AsyncClient(timeout=10, verify=KEYCLOAK_SSL_VERIFY) as client:
        resp = await client.post(
            f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
            },
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to get Keycloak admin token")

        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 60) - 10
        return _token_cache["access_token"]


async def _fetch_keycloak_user(user_id: str) -> dict | None:
    token = await _get_admin_token()
    base = f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=10, verify=KEYCLOAK_SSL_VERIFY) as client:
        user_resp = await client.get(f"{base}/users/{user_id}", headers=headers)
        if user_resp.status_code == 404:
            return None
        if user_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch user from Keycloak")

        groups_resp = await client.get(f"{base}/users/{user_id}/groups", headers=headers)
        if groups_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch user groups from Keycloak")

    user = user_resp.json()
    groups = groups_resp.json()

    # group path 格式: /DeptName 或 /DeptName/RoleName
    dept_id = None
    for g in groups:
        parts = [p for p in g.get("path", "").split("/") if p]
        if parts:
            dept_id = parts[0]
            break

    return {
        "user_id": user["id"],
        "user_email": user.get("email", ""),
        "key_name": user.get("username", ""),
        "dept_id": dept_id,
        "enabled": user.get("enabled", True),
    }


@router.post("/keycloak/bulk")
async def keycloak_bulk_sync(
    _: None = Depends(verify_admin_key),
):
    """從 Keycloak 拉取全部使用者並同步到 DB。
    用於 realm 設定修正後重新建立使用者清單。
    """

    token = await _get_admin_token()
    base = f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}"
    headers = {"Authorization": f"Bearer {token}"}

    # 分頁取得所有使用者
    all_kc_users: list[dict] = []
    first = 0
    page_size = 100
    async with httpx.AsyncClient(timeout=30, verify=KEYCLOAK_SSL_VERIFY) as client:
        while True:
            resp = await client.get(
                f"{base}/users",
                headers=headers,
                params={"first": first, "max": page_size, "briefRepresentation": "false"},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Keycloak error: {resp.text}")
            page = resp.json()
            if not page:
                break
            all_kc_users.extend(page)
            if len(page) < page_size:
                break
            first += page_size

    created = updated = skipped = 0

    async with httpx.AsyncClient(timeout=10, verify=KEYCLOAK_SSL_VERIFY) as client:
        for kc_user in all_kc_users:
            user_id = kc_user.get("id", "")
            if not user_id:
                continue

            # 取得使用者群組 → dept_id
            groups_resp = await client.get(
                f"{base}/users/{user_id}/groups", headers=headers
            )
            groups = groups_resp.json() if groups_resp.status_code == 200 else []
            dept_id = None
            for g in groups:
                parts = [p for p in g.get("path", "").split("/") if p]
                if parts:
                    dept_id = parts[0]
                    break

            if not dept_id:
                skipped += 1
                continue  # 無群組（如 Keycloak admin）不匯入

            blocked = int(not kc_user.get("enabled", True))

            with get_conn(DB_PATH) as conn:
                if dept_id:
                    dept_exists = conn.execute(
                        "SELECT 1 FROM departments WHERE dept_id = ?", (dept_id,)
                    ).fetchone()
                    if not dept_exists:
                        conn.execute(
                            """INSERT INTO departments
                               (dept_id, dept_name, allowed_models, dept_rpm_limit, dept_tpm_limit)
                               VALUES (?, ?, '[]', NULL, NULL)""",
                            (dept_id, dept_id),
                        )
                        bump_version(conn)

                existing = conn.execute(
                    "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()

                if existing:
                    conn.execute(
                        """UPDATE users SET
                           key_name=?, user_email=?, dept_id=?, blocked=?,
                           updated_at=datetime('now')
                           WHERE user_id=?""",
                        (
                            kc_user.get("username", ""),
                            kc_user.get("email", ""),
                            dept_id,
                            blocked,
                            user_id,
                        ),
                    )
                    bump_version(conn)
                    updated += 1
                else:
                    api_key = f"sk-{uuid.uuid4().hex}"
                    # user.models 留空 → custom_auth 執行期繼承部門 allowed_models
                    conn.execute(
                        """INSERT INTO users
                           (api_key, key_name, user_id, user_email, dept_id,
                            models, rpm_limit, tpm_limit, aliases, metadata, blocked)
                           VALUES (?, ?, ?, ?, ?, '[]', NULL, NULL, '{}', '{}', ?)""",
                        (
                            api_key,
                            kc_user.get("username", ""),
                            user_id,
                            kc_user.get("email", ""),
                            dept_id,
                            blocked,
                        ),
                    )
                    bump_version(conn)
                    created += 1

    return {
        "status": "ok",
        "total_keycloak_users": len(all_kc_users),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


@router.post("/keycloak")
async def keycloak_sync(
    request: Request,
    x_webhook_secret: str = Header(default=""),
):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET not configured")
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    body = await request.json()
    user_id: str = body.get("user_id", "").strip()
    event_type: str = body.get("event_type", "").upper()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id is required")

    # Keycloak DELETE 事件 → 封鎖使用者
    if event_type == "DELETE":
        with get_conn(DB_PATH) as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE users SET blocked=1, updated_at=datetime('now') WHERE user_id=?",
                    (user_id,),
                )
                bump_version(conn)
        return {"status": "blocked", "user_id": user_id}

    kc_user = await _fetch_keycloak_user(user_id)
    if kc_user is None:
        return {"status": "skipped", "user_id": user_id, "reason": "user not found in Keycloak"}

    dept_id = kc_user["dept_id"]
    blocked = int(not kc_user["enabled"])

    # 與 bulk 一致：無群組（推不出 dept_id）的使用者不匯入也不更新，
    # 否則 INSERT/UPDATE 會撞 users.dept_id 的 NOT NULL
    if not dept_id:
        return {"status": "skipped", "user_id": user_id, "reason": "user has no group in Keycloak"}

    with get_conn(DB_PATH) as conn:
        dept_exists = conn.execute(
            "SELECT 1 FROM departments WHERE dept_id = ?", (dept_id,)
        ).fetchone()
        if not dept_exists:
            conn.execute(
                """INSERT INTO departments
                   (dept_id, dept_name, allowed_models, dept_rpm_limit, dept_tpm_limit)
                   VALUES (?, ?, '[]', NULL, NULL)""",
                (dept_id, dept_id),
            )
            bump_version(conn)

        existing = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE users SET
                   key_name=?, user_email=?, dept_id=?, blocked=?,
                   updated_at=datetime('now')
                   WHERE user_id=?""",
                (
                    kc_user["key_name"],
                    kc_user["user_email"],
                    dept_id,
                    blocked,
                    user_id,
                ),
            )
            bump_version(conn)
            return {"status": "updated", "user_id": user_id}
        else:
            api_key = f"sk-{uuid.uuid4().hex}"
            # user.models 留空 → custom_auth 執行期繼承部門 allowed_models
            conn.execute(
                """INSERT INTO users
                   (api_key, key_name, user_id, user_email, dept_id,
                    models, rpm_limit, tpm_limit, aliases, metadata, blocked)
                   VALUES (?, ?, ?, ?, ?, '[]', NULL, NULL, '{}', '{}', ?)""",
                (
                    api_key,
                    kc_user["key_name"],
                    user_id,
                    kc_user["user_email"],
                    dept_id,
                    blocked,
                ),
            )
            bump_version(conn)
            return {"status": "created", "user_id": user_id, "api_key": api_key}
