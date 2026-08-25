"""部門管理的業務邏輯，不帶認證。

刻意不吃任何 auth 相關參數/依賴——`routers/departments.py`（ADMIN_API_KEY）與未來
`admin-web` 的部門管理頁（Keycloak session）各自認證後呼叫這裡的函式，避免網頁層
繞去用 ADMIN_API_KEY 打自己的 API（見 docs/admin-web-plan.md 決策 C）。
"""
import json

from fastapi import HTTPException

from database import DB_PATH, bump_version, get_conn, parse_json_fields, row_to_dict
from models import DepartmentIn, DepartmentPatch

_JSON_FIELDS = ["allowed_models", "provider_keys"]


def _fetch_dept(conn, dept_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM departments WHERE dept_id = ?", (dept_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Department '{dept_id}' not found")
    return parse_json_fields(row_to_dict(row), _JSON_FIELDS)


def _merge_provider_keys(
    current: dict, openrouter_api_key: str | None, patch: dict[str, str] | None
) -> dict:
    """決策 E：openrouter_api_key 仍是 provider_keys["openrouter"] 的來源之一，
    兩者保持同步，讓既有只認 openrouter_api_key 的 curl 呼叫者行為完全不變。

    openrouter_api_key 為 None 表示這次沒有提供該欄位（PATCH 未提及，維持原值）；
    空字串則比照該欄位本來的語意逐字寫入（PUT 全量替換時清除，見既有行為）。
    """
    merged = dict(current or {})
    if patch:
        merged.update(patch)  # 逐一覆蓋提到的 provider；沒提到的維持不變
    if openrouter_api_key is not None:
        if openrouter_api_key:
            merged["openrouter"] = openrouter_api_key
        else:
            merged.pop("openrouter", None)
    return merged


def list_departments() -> list[dict]:
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM departments ORDER BY dept_id").fetchall()
    return [parse_json_fields(row_to_dict(r), _JSON_FIELDS) for r in rows]


def create_department(body: DepartmentIn) -> dict:
    with get_conn(DB_PATH) as conn:
        exists = conn.execute(
            "SELECT 1 FROM departments WHERE dept_id = ?", (body.dept_id,)
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail=f"Department '{body.dept_id}' already exists")
        provider_keys = {"openrouter": body.openrouter_api_key} if body.openrouter_api_key else {}
        conn.execute(
            """INSERT INTO departments
               (dept_id, dept_name, openrouter_api_key, allowed_models, dept_rpm_limit, dept_tpm_limit, provider_keys)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                body.dept_id,
                body.dept_name,
                body.openrouter_api_key,
                json.dumps(body.allowed_models, ensure_ascii=False),
                body.dept_rpm_limit,
                body.dept_tpm_limit,
                json.dumps(provider_keys, ensure_ascii=False),
            ),
        )
        bump_version(conn)
        return _fetch_dept(conn, body.dept_id)


def get_department(dept_id: str) -> dict:
    with get_conn(DB_PATH) as conn:
        return _fetch_dept(conn, dept_id)


def update_department(dept_id: str, body: DepartmentIn) -> dict:
    with get_conn(DB_PATH) as conn:
        current = _fetch_dept(conn, dept_id)
        provider_keys = _merge_provider_keys(current["provider_keys"], body.openrouter_api_key, None)
        conn.execute(
            """UPDATE departments SET
               dept_name=?, openrouter_api_key=?, allowed_models=?,
               dept_rpm_limit=?, dept_tpm_limit=?, provider_keys=?, updated_at=datetime('now')
               WHERE dept_id=?""",
            (
                body.dept_name,
                body.openrouter_api_key,
                json.dumps(body.allowed_models, ensure_ascii=False),
                body.dept_rpm_limit,
                body.dept_tpm_limit,
                json.dumps(provider_keys, ensure_ascii=False),
                dept_id,
            ),
        )
        bump_version(conn)
        return _fetch_dept(conn, dept_id)


def patch_department(dept_id: str, body: DepartmentPatch) -> dict:
    with get_conn(DB_PATH) as conn:
        current = _fetch_dept(conn, dept_id)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return current

        provider_keys_patch = updates.pop("provider_keys", None)
        openrouter_api_key = updates.get("openrouter_api_key")  # None＝這次沒提供
        if provider_keys_patch is not None or openrouter_api_key is not None:
            updates["provider_keys"] = _merge_provider_keys(
                current["provider_keys"], openrouter_api_key, provider_keys_patch
            )

        fields = []
        values = []
        for key, val in updates.items():
            fields.append(f"{key}=?")
            values.append(json.dumps(val, ensure_ascii=False) if key in ("allowed_models", "provider_keys") else val)
        fields.append("updated_at=datetime('now')")
        values.append(dept_id)
        conn.execute(f"UPDATE departments SET {', '.join(fields)} WHERE dept_id=?", values)
        bump_version(conn)
        return _fetch_dept(conn, dept_id)


def delete_department(dept_id: str) -> None:
    with get_conn(DB_PATH) as conn:
        _fetch_dept(conn, dept_id)
        has_users = conn.execute(
            "SELECT 1 FROM users WHERE dept_id = ? LIMIT 1", (dept_id,)
        ).fetchone()
        if has_users:
            raise HTTPException(
                status_code=409,
                detail=f"Department '{dept_id}' still has users; remove them first",
            )
        conn.execute("DELETE FROM departments WHERE dept_id=?", (dept_id,))
        bump_version(conn)
