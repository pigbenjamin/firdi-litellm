import json
from fastapi import APIRouter, Depends, HTTPException

from auth import verify_admin_key
from database import DB_PATH, bump_version, get_conn, parse_json_fields, row_to_dict
from models import DepartmentIn, DepartmentOut, DepartmentPatch

router = APIRouter(prefix="/api/v1/departments", dependencies=[Depends(verify_admin_key)])

_JSON_FIELDS = ["allowed_models"]


def _fetch_dept(conn, dept_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM departments WHERE dept_id = ?", (dept_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Department '{dept_id}' not found")
    return parse_json_fields(row_to_dict(row), _JSON_FIELDS)


@router.get("", response_model=list[DepartmentOut])
def list_departments():
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM departments ORDER BY dept_id").fetchall()
    return [parse_json_fields(row_to_dict(r), _JSON_FIELDS) for r in rows]


@router.post("", response_model=DepartmentOut, status_code=201)
def create_department(body: DepartmentIn):
    with get_conn(DB_PATH) as conn:
        exists = conn.execute(
            "SELECT 1 FROM departments WHERE dept_id = ?", (body.dept_id,)
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail=f"Department '{body.dept_id}' already exists")
        conn.execute(
            """INSERT INTO departments
               (dept_id, dept_name, openrouter_api_key, allowed_models, dept_rpm_limit, dept_tpm_limit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                body.dept_id,
                body.dept_name,
                body.openrouter_api_key,
                json.dumps(body.allowed_models, ensure_ascii=False),
                body.dept_rpm_limit,
                body.dept_tpm_limit,
            ),
        )
        bump_version(conn)
        return _fetch_dept(conn, body.dept_id)


@router.get("/{dept_id}", response_model=DepartmentOut)
def get_department(dept_id: str):
    with get_conn(DB_PATH) as conn:
        return _fetch_dept(conn, dept_id)


@router.put("/{dept_id}", response_model=DepartmentOut)
def update_department(dept_id: str, body: DepartmentIn):
    with get_conn(DB_PATH) as conn:
        _fetch_dept(conn, dept_id)
        conn.execute(
            """UPDATE departments SET
               dept_name=?, openrouter_api_key=?, allowed_models=?,
               dept_rpm_limit=?, dept_tpm_limit=?, updated_at=datetime('now')
               WHERE dept_id=?""",
            (
                body.dept_name,
                body.openrouter_api_key,
                json.dumps(body.allowed_models, ensure_ascii=False),
                body.dept_rpm_limit,
                body.dept_tpm_limit,
                dept_id,
            ),
        )
        bump_version(conn)
        return _fetch_dept(conn, dept_id)


@router.patch("/{dept_id}", response_model=DepartmentOut)
def patch_department(dept_id: str, body: DepartmentPatch):
    with get_conn(DB_PATH) as conn:
        current = _fetch_dept(conn, dept_id)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return current
        fields = []
        values = []
        for key, val in updates.items():
            fields.append(f"{key}=?")
            values.append(json.dumps(val, ensure_ascii=False) if key == "allowed_models" else val)
        fields.append("updated_at=datetime('now')")
        values.append(dept_id)
        conn.execute(f"UPDATE departments SET {', '.join(fields)} WHERE dept_id=?", values)
        bump_version(conn)
        return _fetch_dept(conn, dept_id)


@router.delete("/{dept_id}", status_code=204)
def delete_department(dept_id: str):
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
