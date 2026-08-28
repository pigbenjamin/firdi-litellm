"""
OpenWebUI 主導架構的權限同步，不帶認證。

刻意不吃任何 auth 相關參數/依賴——`routers/openwebui.py`（ADMIN_API_KEY）與
`admin-web` 的同步／診斷頁（Keycloak session）各自認證後呼叫這裡的函式（見
docs/admin-web-plan.md 決策 C：不要讓網頁層拿 ADMIN_API_KEY 用 HTTP 打自己的
API）。

權威來源：入口 A（OpenWebUI 畫面上的模型授權 access_grants）。pull 一律只讀 A。
  - pull（主流程）：入口 A → DB。CronJob 每 2 分鐘自動跑 + 可手動觸發，
    dry_run=True 時純讀不寫，admin-web 的診斷頁只會用這個模式。
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
  - GET  /api/v1/users/all                  → {users:[{id, email, oauth.oidc.sub, ...}]}（不分頁；/api/v1/users/ 每頁僅 30 筆）
  - GET  /api/v1/models/base                → [完整 model 記錄含 access_grants]
  - POST /api/v1/models/model/update?id=X   → 需完整 model 記錄，access_grants 陣列取代式寫入
  - POST /api/v1/models/create              → 建立新 model 記錄
  access_grant: {resource_type:"model", resource_id, principal_type:"group"|"user",
                 principal_id, permission:"read"}（id/created_at 伺服器生成）
"""
import json
import os

import httpx
from fastapi import HTTPException

from database import DB_PATH, bump_version, get_conn

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
    """openwebui_user_id → keycloak_sub（= DB user_id）。

    解析順序：oauth.oidc.sub 優先；為空時回退用 OpenWebUI 帳號的 email 比對 DB user_email
    （讓沒 SSO 登入過 OpenWebUI 的帳號也對映得到，與 custom_auth 的回退規則一致）。
    走 /api/v1/users/all（不分頁）；/api/v1/users/ 每頁只回 30 筆會漏人。
    """
    data = await _fetch_owui_json(client, owui, "/api/v1/users/all")
    users = data.get("users", data) if isinstance(data, dict) else data

    # DB email → user_id（email 回退用；只收 human 帳號、email 非空）
    email2uid: dict[str, str] = {}
    with get_conn(DB_PATH) as conn:
        for row in conn.execute(
            "SELECT user_id, user_email FROM users WHERE account_type='human'"
        ).fetchall():
            email = (row["user_email"] or "").strip().lower()
            if email:
                email2uid[email] = row["user_id"]

    mapping = {}
    for u in users:
        sub = ((u.get("oauth") or {}).get("oidc") or {}).get("sub")
        if not sub:
            sub = email2uid.get((u.get("email") or "").strip().lower())
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

async def pull_openwebui_model_access(dry_run: bool = False) -> dict:
    """把 OpenWebUI 各模型的 access_grants 反算回 DB：
    dept.allowed_models ← group grants；users.models ← user grants。
    只在有差異時寫入並 bump 版本。dry_run=True 時保證不寫入任何資料。
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

async def push_model_access_to_openwebui(target: str = "a", dry_run: bool = False) -> dict:
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

# ── OpenWebUI 連線的「模型 IDs」白名單 ──────────────────────────────────────
#
# OpenWebUI 的每條 OpenAI 相容連線可以設一份 model_ids 白名單：非空時只有清單裡
# 的模型會被拉進來，**不管上游 /models 回什麼、也不管 access_grants 怎麼設**。
# 空陣列則代表「全部」。
#
# 2026-08-28 在 ai-x-dev 實際踩到：模型上架、發布、授權、push 全部成功，OpenWebUI
# 裡連模型記錄跟 group grant 都建好了，使用者的下拉選單就是看不到——因為那條
# LiteLLM 連線的白名單只列了三個舊模型。整條鏈沒有任何錯誤訊息，是最難查的那種
# 「看起來都好但沒生效」。
#
# 認哪條連線是 LiteLLM 用「model_ids 與 LiteLLM 模型清單的交集最大」：admin-api
# 沒有 OpenWebUI 的 service key 可以比對，而 URL 也對不起來（OpenWebUI 設的是
# NodePort 位址，admin-api 用的是叢集內 Service DNS）。空白名單的連線一律跳過
# ——它本來就顯示全部，不需要維護。

OPENAI_CONFIG_PATH = "/openai/config"
OPENAI_CONFIG_UPDATE_PATH = "/openai/config/update"


def _pick_litellm_connection(configs: dict, litellm_models: set[str]) -> str | None:
    """回傳最像 LiteLLM 的那條連線的 key；找不到回 None（呼叫端據此 no-op）。"""
    best, best_overlap = None, 0
    for key, cfg in (configs or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enable", True):
            continue
        ids = cfg.get("model_ids") or []
        if not ids:
            continue  # 空白名單＝顯示全部，不需要維護
        overlap = len(set(ids) & litellm_models)
        if overlap > best_overlap:
            best, best_overlap = key, overlap
    return best


async def ensure_model_listed(model_name: str, present: bool, target: str = "a") -> dict:
    """把 model_name 加進（present=True）或移出（False）OpenWebUI 連線的白名單。

    **這是盡力而為的輔助動作，不是關鍵路徑。** 呼叫端一定要容忍它失敗——模型在
    LiteLLM 的註冊與 DB 的授權才是主體，白名單沒補上頂多是「使用者暫時看不到，
    管理者手動加一筆就好」，不該讓整個發布失敗。

    回傳 {"status": updated|unchanged|skipped|failed, ...}，讓 UI 據此決定要不要
    提醒操作者手動處理。
    """
    owui = _resolve_owui_target(target)
    if owui is None or not owui["url"] or not owui["admin_key"]:
        return {"status": "skipped", "reason": f"入口 {target} 未設定", "target": target}
    if not LITELLM_MASTER_KEY:
        return {"status": "skipped", "reason": "LITELLM_MASTER_KEY 未設定", "target": target}

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            litellm_models = await _fetch_litellm_models(client)
        except HTTPException as exc:
            return {"status": "failed", "reason": f"讀不到 LiteLLM 模型清單：{exc.detail}", "target": target}

        try:
            resp = await client.get(
                f"{owui['url']}{OPENAI_CONFIG_PATH}", headers=_owui_headers(owui["admin_key"])
            )
        except httpx.RequestError as exc:
            return {"status": "failed", "reason": f"連不上 OpenWebUI：{exc}", "target": target}
        if resp.status_code != 200:
            return {"status": "failed",
                    "reason": f"讀 {OPENAI_CONFIG_PATH} 失敗（HTTP {resp.status_code}）", "target": target}

        config = resp.json()
        configs = config.get("OPENAI_API_CONFIGS") or {}
        key = _pick_litellm_connection(configs, litellm_models)
        if key is None:
            # 沒有任何連線在用白名單 → OpenWebUI 本來就會顯示全部，不需要動
            return {"status": "skipped", "reason": "沒有使用白名單的 LiteLLM 連線", "target": target}

        ids = list(configs[key].get("model_ids") or [])
        if present and model_name not in ids:
            ids.append(model_name)
        elif not present and model_name in ids:
            ids = [m for m in ids if m != model_name]
        else:
            return {"status": "unchanged", "target": target, "connection": key, "model_ids": ids}

        # 取代式寫入：一定要送回完整的設定物件，只改我們要動的那一格，
        # 不然會把其他連線一起洗掉。
        configs[key]["model_ids"] = ids
        config["OPENAI_API_CONFIGS"] = configs
        try:
            put = await client.post(
                f"{owui['url']}{OPENAI_CONFIG_UPDATE_PATH}",
                headers=_owui_headers(owui["admin_key"]), json=config,
            )
        except httpx.RequestError as exc:
            return {"status": "failed", "reason": f"寫入 OpenWebUI 失敗：{exc}", "target": target}
        if put.status_code != 200:
            return {"status": "failed",
                    "reason": f"寫入失敗（HTTP {put.status_code}）：{put.text[:200]}", "target": target}

    return {"status": "updated", "target": target, "connection": key, "model_ids": ids}


async def sync_model_listing(model_name: str, present: bool) -> list[dict]:
    """兩個 OpenWebUI 入口都同步。B 未設定時會安全回 skipped。"""
    return [await ensure_model_listed(model_name, present, t) for t in ("a", "b")]
