import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import verify_admin_key
from database import DB_PATH, bump_version, get_conn, parse_json_fields, row_to_dict
from models import UserIn, UserOut, UserPatch

router = APIRouter(prefix="/api/v1/users", dependencies=[Depends(verify_admin_key)])

_JSON_FIELDS = ["models", "aliases", "metadata"]


def _fetch_user(conn, user_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    record = row_to_dict(row)
    parse_json_fields(record, _JSON_FIELDS)
    record["blocked"] = bool(record["blocked"])
    return record


def _dept_exists(conn, dept_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM departments WHERE dept_id = ?", (dept_id,)
    ).fetchone() is not None


@router.get("", response_model=list[UserOut])
def list_users(
    dept_id: str | None = Query(default=None),
    account_type: str | None = Query(default=None),
):
    clauses, params = [], []
    if dept_id:
        clauses.append("dept_id = ?")
        params.append(dept_id)
    if account_type:
        clauses.append("account_type = ?")
        params.append(account_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT * FROM users {where} ORDER BY user_id", params
        ).fetchall()
    result = []
    for r in rows:
        record = parse_json_fields(row_to_dict(r), _JSON_FIELDS)
        record["blocked"] = bool(record["blocked"])
        result.append(record)
    return result


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserIn):
    with get_conn(DB_PATH) as conn:
        if not _dept_exists(conn, body.dept_id):
            raise HTTPException(status_code=404, detail=f"Department '{body.dept_id}' not found")
        exists = conn.execute(
            "SELECT 1 FROM users WHERE api_key = ? OR user_id = ?",
            (body.api_key, body.user_id),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="api_key or user_id already exists")
        conn.execute(
            """INSERT INTO users
               (api_key, key_name, user_id, user_email, dept_id, account_type,
                models, rpm_limit, tpm_limit, aliases, metadata, blocked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                body.api_key,
                body.key_name,
                body.user_id,
                body.user_email,
                body.dept_id,
                body.account_type,
                json.dumps(body.models, ensure_ascii=False),
                body.rpm_limit,
                body.tpm_limit,
                json.dumps(body.aliases, ensure_ascii=False),
                json.dumps(body.metadata, ensure_ascii=False),
                int(body.blocked),
            ),
        )
        bump_version(conn)
        return _fetch_user(conn, body.user_id)


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: str):
    with get_conn(DB_PATH) as conn:
        return _fetch_user(conn, user_id)


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: str, body: UserIn):
    with get_conn(DB_PATH) as conn:
        _fetch_user(conn, user_id)
        if not _dept_exists(conn, body.dept_id):
            raise HTTPException(status_code=404, detail=f"Department '{body.dept_id}' not found")
        conn.execute(
            """UPDATE users SET
               api_key=?, key_name=?, user_email=?, dept_id=?, account_type=?,
               models=?, rpm_limit=?, tpm_limit=?, aliases=?, metadata=?, blocked=?,
               updated_at=datetime('now')
               WHERE user_id=?""",
            (
                body.api_key,
                body.key_name,
                body.user_email,
                body.dept_id,
                body.account_type,
                json.dumps(body.models, ensure_ascii=False),
                body.rpm_limit,
                body.tpm_limit,
                json.dumps(body.aliases, ensure_ascii=False),
                json.dumps(body.metadata, ensure_ascii=False),
                int(body.blocked),
                user_id,
            ),
        )
        bump_version(conn)
        return _fetch_user(conn, user_id)


@router.patch("/{user_id}", response_model=UserOut)
def patch_user(user_id: str, body: UserPatch):
    with get_conn(DB_PATH) as conn:
        current = _fetch_user(conn, user_id)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return current
        if "dept_id" in updates and not _dept_exists(conn, updates["dept_id"]):
            raise HTTPException(status_code=404, detail=f"Department '{updates['dept_id']}' not found")
        fields = []
        values = []
        for key, val in updates.items():
            fields.append(f"{key}=?")
            if key in ("models", "aliases", "metadata"):
                values.append(json.dumps(val, ensure_ascii=False))
            elif key == "blocked":
                values.append(int(val))
            else:
                values.append(val)
        fields.append("updated_at=datetime('now')")
        values.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", values)
        bump_version(conn)
        return _fetch_user(conn, user_id)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str):
    with get_conn(DB_PATH) as conn:
        _fetch_user(conn, user_id)
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        bump_version(conn)


@router.post("/{user_id}/block", response_model=UserOut)
def block_user(user_id: str):
    with get_conn(DB_PATH) as conn:
        _fetch_user(conn, user_id)
        conn.execute(
            "UPDATE users SET blocked=1, updated_at=datetime('now') WHERE user_id=?",
            (user_id,),
        )
        bump_version(conn)
        return _fetch_user(conn, user_id)


@router.post("/{user_id}/unblock", response_model=UserOut)
def unblock_user(user_id: str):
    with get_conn(DB_PATH) as conn:
        _fetch_user(conn, user_id)
        conn.execute(
            "UPDATE users SET blocked=0, updated_at=datetime('now') WHERE user_id=?",
            (user_id,),
        )
        bump_version(conn)
        return _fetch_user(conn, user_id)


@router.post("/{user_id}/regenerate-key", response_model=UserOut)
def regenerate_key(user_id: str):
    new_key = f"sk-{uuid.uuid4().hex}"
    with get_conn(DB_PATH) as conn:
        _fetch_user(conn, user_id)
        conn.execute(
            "UPDATE users SET api_key=?, updated_at=datetime('now') WHERE user_id=?",
            (new_key, user_id),
        )
        bump_version(conn)
        return _fetch_user(conn, user_id)
