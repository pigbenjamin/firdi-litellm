"""
OpenWebUI 主導架構的權限同步。

權威來源：入口 A（OpenWebUI 畫面上的模型授權 access_grants）。pull 一律只讀 A。
  - pull（主流程）：入口 A → DB。CronJob 每 2 分鐘自動跑 + 可手動觸發。
  - push（輔助工具）：DB → OpenWebUI 完整鏡像，?target=a|b 指定目標。
      target=a：DB 側 PATCH 改權限後手動跑（見下方 SOP）。
      target=b：把 DB 權限鏡像到第二入口 B（唯讀鏡像，CronJob 自動 push 使 B 對齊 A）。
                B 未設定時 no-op（回 status=skipped）。

DB 側改權限的 SOP：pull → PATCH → push（target=a）
（只 PATCH 不 push，改動會在下次 pull 被 A 的狀態還原）

範圍：只管理 LiteLLM 接口暴露的模型（以 LiteLLM /models 清單過濾），
OpenWebUI 自己的連線模型（如 local-ollama.*）與 pipe 一律不碰。

OpenWebUI API 合約（逆向確認）：
  - GET  /api/v1/groups/                    → [{id, name, ...}]，name = dept_id
  - GET  /api/v1/users/                     → {users:[{id, oauth.oidc.sub, ...}]}
  - GET  /api/v1/models/base                → [完整 model 記錄含 access_grants]
  - POST /api/v1/models/model/update?id=X   → 需完整 model 記錄，access_grants 陣列取代式寫入
  - POST /api/v1/models/create              → 建立新 model 記錄
  access_grant: {resource_type:"model", resource_id, principal_type:"group"|"user",
                 principal_id, permission:"read"}（id/created_at 伺服器生成）
"""
import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import verify_admin_key
from database import DB_PATH, bump_version, get_conn

router = APIRouter(
    prefix="/api/v1/sync/openwebui",
    dependencies=[Depends(verify_admin_key)],
)

OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "").rstrip("/")
OPENWEBUI_ADMIN_KEY = os.getenv("OPENWEBUI_ADMIN_KEY", "")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# 第二組 OpenWebUI 入口（鏡像目標）。未設定時 push target=b 會安全 no-op。
OPENWEBUI_URL_B = os.getenv("OPENWEBUI_URL_B", "").rstrip("/")
OPENWEBUI_ADMIN_KEY_B = os.getenv("OPENWEBUI_ADMIN_KEY_B", "")

# 入口 A（權威來源）：pull 一律從這裡讀。
OWUI_A = {"name": "portal-a", "url": OPENWEBUI_URL, "admin_key": OPENWEBUI_ADMIN_KEY}


def _resolve_owui_target(target: str) -> dict | None:
    """把 target=a|b 解析成入口設定。B 未設定時回 None（呼叫端據此 no-op）。"""
    t = (target or "a").lower()
    if t == "a":
        return OWUI_A
    if t == "b":
        if OPENWEBUI_URL_B and OPENWEBUI_ADMIN_KEY_B:
            return {"name": "portal-b", "url": OPENWEBUI_URL_B, "admin_key": OPENWEBUI_ADMIN_KEY_B}
        return None
    return None


def _require_config() -> None:
    if not OPENWEBUI_URL or not OPENWEBUI_ADMIN_KEY:
        raise HTTPException(
            status_code=500,
            detail="OPENWEBUI_URL / OPENWEBUI_ADMIN_KEY not configured",
        )
    if not LITELLM_MASTER_KEY:
        raise HTTPException(status_code=500, detail="LITELLM_MASTER_KEY not configured")


def _owui_headers(admin_key: str) -> dict:
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


# ── 抓取（任一失敗即中止，不做部分寫入）───────────────────────────────────────

async def _fetch_litellm_models(client: httpx.AsyncClient) -> set[str]:
    try:
        resp = await client.get(
            f"{LITELLM_URL}/models",
            headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LiteLLM /models error: {resp.text[:200]}")
    return {item["id"] for item in resp.json().get("data", [])}


async def _fetch_owui_json(client: httpx.AsyncClient, owui: dict, path: str):
    try:
        resp = await client.get(
            f"{owui['url']}{path}", headers=_owui_headers(owui["admin_key"])
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach OpenWebUI: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"OpenWebUI {path} error: {resp.text[:200]}")
    return resp.json()


async def _fetch_groups(client: httpx.AsyncClient, owui: dict) -> dict[str, str]:
    """group_id → group_name（= dept_id）"""
    data = await _fetch_owui_json(client, owui, "/api/v1/groups/")
    return {g["id"]: g["name"] for g in data}


async def _fetch_user_mapping(client: httpx.AsyncClient, owui: dict) -> dict[str, str]:
    """openwebui_user_id → keycloak_sub（= DB user_id）；無 oidc sub 的不收"""
    data = await _fetch_owui_json(client, owui, "/api/v1/users/")
    users = data.get("users", data) if isinstance(data, dict) else data
    mapping = {}
    for u in users:
        sub = ((u.get("oauth") or {}).get("oidc") or {}).get("sub")
        if sub:
            mapping[u["id"]] = sub
    return mapping


async def _fetch_base_models(client: httpx.AsyncClient, owui: dict) -> list[dict]:
    return await _fetch_owui_json(client, owui, "/api/v1/models/base")


def _grant(model_id: str, principal_type: str, principal_id: str) -> dict:
    return {
        "resource_type": "model",
        "resource_id": model_id,
        "principal_type": principal_type,
        "principal_id": principal_id,
        "permission": "read",
    }


def _grant_key(g: dict) -> tuple:
    return (g.get("principal_type"), g.get("principal_id"), g.get("permission"))


# ── pull：OpenWebUI → DB（主流程）────────────────────────────────────────────

@router.post("/pull-models")
async def pull_openwebui_model_access(dry_run: bool = False):
    """把 OpenWebUI 各模型的 access_grants 反算回 DB：
    dept.allowed_models ← group grants；users.models ← user grants。
    只在有差異時寫入並 bump 版本。
    """
    _require_config()

    async with httpx.AsyncClient(timeout=30) as client:
        litellm_models = await _fetch_litellm_models(client)
        gid2dept = await _fetch_groups(client, OWUI_A)
        owui2sub = await _fetch_user_mapping(client, OWUI_A)
        base_models = await _fetch_base_models(client, OWUI_A)

    # 反算 OpenWebUI 授權
    dept_models: dict[str, set] = {}
    user_models: dict[str, set] = {}   # keyed by keycloak_sub (= DB user_id)
    ignored_models: list[str] = []     # 非 LiteLLM 管轄的 OpenWebUI 模型
    skipped_grants: list[dict] = []    # 對映不到的 grants

    for m in base_models:
        model_id = m.get("id", "")
        if model_id not in litellm_models:
            ignored_models.append(model_id)
            continue
        for g in (m.get("access_grants") or []):
            if g.get("permission") != "read":
                continue
            ptype, pid = g.get("principal_type"), g.get("principal_id")
            if ptype == "group":
                dept_id = gid2dept.get(pid)
                if dept_id is None:
                    skipped_grants.append({"model": model_id, "reason": "unknown_group", "principal_id": pid})
                    continue
                dept_models.setdefault(dept_id, set()).add(model_id)
            elif ptype == "user":
                sub = owui2sub.get(pid)
                if sub is None:
                    skipped_grants.append({"model": model_id, "reason": "no_oidc_sub", "principal_id": pid})
                    continue
                user_models.setdefault(sub, set()).add(model_id)

    # 寫 DB（diff 才寫）
    changed_depts: list[dict] = []
    changed_users: list[dict] = []
    unknown_depts = []   # OpenWebUI 有 group 授權但 DB 無此部門
    unknown_users = []   # OpenWebUI 有 user 授權但 DB 無此使用者

    with get_conn(DB_PATH) as conn:
        db_dept_ids = set()
        for row in conn.execute("SELECT dept_id, allowed_models FROM departments").fetchall():
            db_dept_ids.add(row["dept_id"])
            try:
                current = sorted(json.loads(row["allowed_models"] or "[]"))
            except (json.JSONDecodeError, TypeError):
                current = []
            desired = sorted(dept_models.get(row["dept_id"], set()))
            if current != desired:
                changed_depts.append({"dept_id": row["dept_id"], "from": current, "to": desired})
                if not dry_run:
                    conn.execute(
                        "UPDATE departments SET allowed_models=? WHERE dept_id=?",
                        (json.dumps(desired, ensure_ascii=False), row["dept_id"]),
                    )

        # 只同步 human 帳號；service 帳號的 models 由 admin-api 直接管理，pull 不可覆寫
        db_user_ids = set()
        for row in conn.execute(
            "SELECT user_id, models FROM users WHERE account_type='human'"
        ).fetchall():
            db_user_ids.add(row["user_id"])
            try:
                current = sorted(json.loads(row["models"] or "[]"))
            except (json.JSONDecodeError, TypeError):
                current = []
            desired = sorted(user_models.get(row["user_id"], set()))
            if current != desired:
                changed_users.append({"user_id": row["user_id"], "from": current, "to": desired})
                if not dry_run:
                    conn.execute(
                        "UPDATE users SET models=?, updated_at=datetime('now') WHERE user_id=?",
                        (json.dumps(desired, ensure_ascii=False), row["user_id"]),
                    )

        unknown_depts = sorted(set(dept_models) - db_dept_ids)
        unknown_users = sorted(set(user_models) - db_user_ids)

        if not dry_run and (changed_depts or changed_users):
            bump_version(conn)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "changed_departments": changed_depts,
        "changed_users": changed_users,
        "unknown_departments": unknown_depts,
        "unknown_users": unknown_users,
        "ignored_models": sorted(ignored_models),
        "skipped_grants": skipped_grants,
    }


# ── push：DB → OpenWebUI 完整鏡像（DB 側改動後手動使用）──────────────────────

@router.post("/models")
async def push_model_access_to_openwebui(target: str = "a", dry_run: bool = False):
    """把 DB 的權限完整鏡像到指定 OpenWebUI 入口（group + user 兩層都取代）。

    target=a（預設）：鏡像到權威入口 A。DB 側改權限的 SOP：pull → PATCH → push（target=a）。
    target=b：鏡像到第二入口 B（唯讀鏡像，由 CronJob 每 2 分鐘自動 push 使 B 對齊 A）。
              B 未設定時安全 no-op（回 status=skipped），方便 CronJob 先掛上、B 上線前不報錯。
    """
    t = (target or "a").lower()
    if t not in ("a", "b"):
        raise HTTPException(status_code=400, detail=f"invalid target '{target}' (expected 'a' or 'b')")
    if not LITELLM_MASTER_KEY:
        raise HTTPException(status_code=500, detail="LITELLM_MASTER_KEY not configured")

    owui = _resolve_owui_target(t)
    if owui is None:
        # target=b 但 B 尚未設定 → no-op，不報錯（CronJob 可先掛上）
        return {"status": "skipped", "reason": f"target '{t}' not configured", "target": t}
    if not owui["url"] or not owui["admin_key"]:
        raise HTTPException(status_code=500, detail=f"target '{t}' URL / admin key not configured")

    # DB 期望狀態
    with get_conn(DB_PATH) as conn:
        depts = []
        for r in conn.execute("SELECT dept_id, allowed_models FROM departments").fetchall():
            try:
                models = json.loads(r["allowed_models"] or "[]")
            except (json.JSONDecodeError, TypeError):
                models = []
            depts.append({"dept_id": r["dept_id"], "models": [m for m in models if m != "*"]})

        # 只鏡像 human 帳號的個人授權；service 帳號不在 OpenWebUI 管理
        db_users = []
        for r in conn.execute(
            "SELECT user_id, models FROM users WHERE blocked=0 AND account_type='human'"
        ).fetchall():
            try:
                models = json.loads(r["models"] or "[]")
            except (json.JSONDecodeError, TypeError):
                models = []
            models = [m for m in models if m != "*"]
            if models:
                db_users.append({"user_id": r["user_id"], "models": models})

    async with httpx.AsyncClient(timeout=30) as client:
        litellm_models = await _fetch_litellm_models(client)
        gid2dept = await _fetch_groups(client, owui)
        dept2gid = {name: gid for gid, name in gid2dept.items()}
        owui2sub = await _fetch_user_mapping(client, owui)
        sub2owui = {sub: oid for oid, sub in owui2sub.items()}
        base_models = {m["id"]: m for m in await _fetch_base_models(client, owui)}

        # model → 期望 grants
        desired: dict[str, list] = {}
        missing_groups: set = set()
        missing_users: set = set()

        for d in depts:
            gid = dept2gid.get(d["dept_id"])
            for model_id in d["models"]:
                if model_id not in litellm_models:
                    continue
                if gid is None:
                    missing_groups.add(d["dept_id"])
                    continue
                desired.setdefault(model_id, []).append(_grant(model_id, "group", gid))

        for u in db_users:
            owui_id = sub2owui.get(u["user_id"])
            for model_id in u["models"]:
                if model_id not in litellm_models:
                    continue
                if owui_id is None:
                    missing_users.add(u["user_id"])
                    continue
                desired.setdefault(model_id, []).append(_grant(model_id, "user", owui_id))

        # 鏡像範圍 = DB 有授權的模型 ∪ OpenWebUI 已有記錄且屬於 LiteLLM 的模型
        # （後者含「DB 已無授權」的 → grants 要清空）
        scope = set(desired) | {mid for mid in base_models if mid in litellm_models}

        results = []
        for model_id in sorted(scope):
            new_grants = desired.get(model_id, [])
            record = base_models.get(model_id)
            entry = {
                "model": model_id,
                "granted_groups": sorted(gid2dept.get(g["principal_id"], "?")
                                         for g in new_grants if g["principal_type"] == "group"),
                "granted_users": len([g for g in new_grants if g["principal_type"] == "user"]),
            }

            if record is not None:
                current_keys = {_grant_key(g) for g in (record.get("access_grants") or [])}
                if current_keys == {_grant_key(g) for g in new_grants}:
                    entry["action"] = "unchanged"
                    results.append(entry)
                    continue
                entry["action"] = "would_update" if dry_run else "updated"
                if not dry_run:
                    payload = dict(record)
                    payload["access_control"] = None
                    payload["access_grants"] = new_grants
                    resp = await client.post(
                        f"{owui['url']}/api/v1/models/model/update",
                        params={"id": model_id}, headers=_owui_headers(owui["admin_key"]), json=payload,
                    )
                    entry["http_status"] = resp.status_code
                    if resp.status_code != 200:
                        entry["error"] = resp.text[:200]
            else:
                if not new_grants:
                    continue  # 無記錄且無授權 → 不需動作
                entry["action"] = "would_create" if dry_run else "created"
                if not dry_run:
                    payload = {
                        "id": model_id, "name": model_id,
                        "meta": {}, "params": {},
                        "base_model_id": None, "is_active": True,
                        "access_control": None, "access_grants": new_grants,
                    }
                    resp = await client.post(
                        f"{owui['url']}/api/v1/models/create",
                        headers=_owui_headers(owui["admin_key"]), json=payload,
                    )
                    entry["http_status"] = resp.status_code
                    if resp.status_code != 200:
                        entry["error"] = resp.text[:200]
            results.append(entry)

    return {
        "status": "ok",
        "target": t,
        "dry_run": dry_run,
        "results": results,
        "missing_groups": sorted(missing_groups),
        "missing_users": sorted(missing_users),
    }
