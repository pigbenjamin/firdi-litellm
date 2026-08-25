import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from threading import Lock

import httpx
from fastapi import Request
from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth

_WRITE_LOCK = Lock()


def write_log(record: dict) -> None:
    path = os.getenv("LOG_PATH", "/app/logs/usage.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ── OpenWebUI 整合設定 ────────────────────────────────────────────────────────

OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "").rstrip("/")
OPENWEBUI_ADMIN_KEY = os.getenv("OPENWEBUI_ADMIN_KEY", "")
OPENWEBUI_SERVICE_KEY = os.getenv("OPENWEBUI_SERVICE_KEY", "")

# 第二組 OpenWebUI 入口（未啟用時三值皆空 → 不會納入 OPENWEBUI_INSTANCES，行為與單入口完全相同）
OPENWEBUI_URL_B = os.getenv("OPENWEBUI_URL_B", "").rstrip("/")
OPENWEBUI_ADMIN_KEY_B = os.getenv("OPENWEBUI_ADMIN_KEY_B", "")
OPENWEBUI_SERVICE_KEY_B = os.getenv("OPENWEBUI_SERVICE_KEY_B", "")


def _build_openwebui_instances() -> dict[str, dict]:
    """service_key → {name, url, admin_key}。只收設定齊全的入口；空 key 不納入。

    用 service key 區分請求來自哪個 OpenWebUI 入口，據以決定用哪組 admin key、
    去哪個實例把 OpenWebUI UUID 解析成 Keycloak sub。兩入口共用同一份 DB 權限。
    """
    instances: dict[str, dict] = {}
    if OPENWEBUI_SERVICE_KEY:
        instances[OPENWEBUI_SERVICE_KEY] = {
            "name": "portal-a", "url": OPENWEBUI_URL, "admin_key": OPENWEBUI_ADMIN_KEY,
        }
    if OPENWEBUI_SERVICE_KEY_B and OPENWEBUI_URL_B and OPENWEBUI_ADMIN_KEY_B:
        instances[OPENWEBUI_SERVICE_KEY_B] = {
            "name": "portal-b", "url": OPENWEBUI_URL_B, "admin_key": OPENWEBUI_ADMIN_KEY_B,
        }
    return instances


OPENWEBUI_INSTANCES = _build_openwebui_instances()

# openwebui_uuid → keycloak_uuid，永不過期（mapping 不會改變）
# UUIDv4 跨實例不會撞號，故 A / B 共用單一快取即可。
_OPENWEBUI_USER_CACHE: dict[str, str] = {}


async def _resolve_keycloak_sub(openwebui_id: str, url: str, admin_key: str) -> str | None:
    """OpenWebUI UUID → DB user_id（= Keycloak sub），in-memory 永久快取，miss 時查該入口 Admin API。

    解析順序：
      1. OpenWebUI 帳號的 oauth.oidc.sub —— 該人用 Keycloak SSO 登入過 OpenWebUI 才會有。
      2. 回退：oidc.sub 為空時，改用該 OpenWebUI 帳號的 email 比對 DB 的 user_email，
         命中就回該使用者的 user_id。讓「還沒用 SSO 登入過 OpenWebUI」的帳號也對應得到
         DB 使用者（前提：OpenWebUI 與 DB 的 email 一致且唯一）。

    走 GET /api/v1/users/all（admin 權限、不分頁）；不可用 /api/v1/users/，那支每頁只回
    30 筆，使用者一多就會把排在後面的人漏掉、永遠 resolve 不到。
    """
    if openwebui_id in _OPENWEBUI_USER_CACHE:
        return _OPENWEBUI_USER_CACHE[openwebui_id]

    if not url or not admin_key:
        print("[custom_auth] OpenWebUI URL or admin key not configured, cannot resolve user")
        return None

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/v1/users/all",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            print(f"[custom_auth] OpenWebUI API response status: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            # 支援 {"users": [...]} 或直接 [...]
            users = data.get("users", data) if isinstance(data, dict) else data
            owui_user = next((u for u in users if u.get("id") == openwebui_id), None)
    except Exception as e:
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "openwebui_resolve_error",
            "openwebui_id": openwebui_id,
            "error": str(e),
        })
        return None

    if owui_user is None:
        return None

    # 路線 1：OpenWebUI 已綁定的 Keycloak sub
    keycloak_sub = ((owui_user.get("oauth") or {}).get("oidc") or {}).get("sub")

    # 路線 2（回退）：oidc.sub 為空 → 用 email 對映 DB user
    if not keycloak_sub:
        email = (owui_user.get("email") or "").strip()
        db_user = _find_user_by_email(email) if email else None
        if db_user:
            keycloak_sub = db_user["user_id"]

    if keycloak_sub:
        _OPENWEBUI_USER_CACHE[openwebui_id] = keycloak_sub

    return keycloak_sub


# ── SQLite 設定載入（db_version 版本戳記 + TTL 雙重快取）────────────────────────

DEFAULT_DB_PATH = "/app/data/users.db"
_DB_LOCK = Lock()
_CACHE_DATA: dict | None = None       # {"departments": [...], "users": [...]}
_CACHE_VERSION: int = -1
_CACHE_LOADED_AT: float = 0.0
_CACHE_TTL: float = 30.0


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _load_config() -> dict:
    global _CACHE_DATA, _CACHE_VERSION, _CACHE_LOADED_AT

    db_path = os.getenv("USER_AUTH_DB_PATH", DEFAULT_DB_PATH)
    now = time.monotonic()

    with _DB_LOCK:
        try:
            conn = _get_conn(db_path)
            row = conn.execute("SELECT version FROM db_version WHERE id=1").fetchone()
            current_version = row["version"] if row else -1
            conn.close()
        except Exception:
            if _CACHE_DATA is not None:
                return _CACHE_DATA
            raise

        ttl_expired = (now - _CACHE_LOADED_AT) > _CACHE_TTL
        if _CACHE_DATA is not None and current_version == _CACHE_VERSION and not ttl_expired:
            return _CACHE_DATA

        conn = _get_conn(db_path)
        try:
            depts = []
            for r in conn.execute("SELECT * FROM departments").fetchall():
                d = dict(r)
                d["allowed_models"] = json.loads(d.get("allowed_models") or "[]")
                # 決策 E：provider → key 的 dict（見 docs/admin-web-plan.md）。舊 DB
                # 可能還沒有這個欄位（尚未跑過 admin-api 的 ALTER TABLE），.get 讓它
                # 安全退化成空 dict，而不是讓整個熱路徑的設定載入失敗。
                d["provider_keys"] = json.loads(d.get("provider_keys") or "{}")
                depts.append(d)

            users = []
            for r in conn.execute("SELECT * FROM users WHERE blocked=0").fetchall():
                u = dict(r)
                u["models"] = json.loads(u.get("models") or "[]")
                u["aliases"] = json.loads(u.get("aliases") or "{}")
                u["metadata"] = json.loads(u.get("metadata") or "{}")
                u["blocked"] = bool(u.get("blocked", 0))
                users.append(u)

            # 決策 E：model_name → key_policy。表可能還不存在（admin-api 尚未跑過
            # 建表的 ALTER），比照上面同樣退化成空 dict，不讓認證整個掛掉。
            try:
                model_key_policies = {
                    row["model_name"]: row["key_policy"]
                    for row in conn.execute("SELECT model_name, key_policy FROM model_key_policies").fetchall()
                }
            except sqlite3.OperationalError:
                model_key_policies = {}

            _CACHE_DATA = {"departments": depts, "users": users, "model_key_policies": model_key_policies}
            _CACHE_VERSION = current_version
            _CACHE_LOADED_AT = now
            return _CACHE_DATA
        finally:
            conn.close()


def _normalize_api_key(api_key: str) -> str:
    if not api_key:
        return ""
    if api_key.startswith("Bearer "):
        return api_key.removeprefix("Bearer ").strip()
    return api_key.strip()


def _find_user(api_key: str) -> dict | None:
    normalized = _normalize_api_key(api_key)
    for user in _load_config().get("users", []):
        if user.get("api_key") == normalized and not user.get("blocked", False):
            return user
    return None


def _find_user_by_user_id(user_id: str) -> dict | None:
    for user in _load_config().get("users", []):
        if user.get("user_id") == user_id and not user.get("blocked", False):
            return user
    return None


def _find_user_by_email(email: str) -> dict | None:
    """以 email 比對 DB 使用者（大小寫不敏感、排除 blocked）。OpenWebUI 無 oidc.sub 時的回退。"""
    target = email.strip().lower()
    if not target:
        return None
    for user in _load_config().get("users", []):
        if (user.get("user_email") or "").strip().lower() == target and not user.get("blocked", False):
            return user
    return None


def _find_dept(dept_id: str) -> dict | None:
    for dept in _load_config().get("departments", []):
        if dept.get("dept_id") == dept_id:
            return dept
    return None


# ── 決策 E：key 來源政策（見 docs/admin-web-plan.md）───────────────────────────
# 「上游是誰」（litellm_params.model）跟「key 從哪來」解耦。這個判斷本來在
# config/custom_logger.py 用 model 字串的 openrouter/ 前綴硬判斷；現在移來這裡，
# 因為這裡本來就在讀 SQLite（db_version + 30 秒 TTL 快取）、本來就知道請求的
# model 是什麼，解析完直接把結果放進 metadata，custom_logger 退化成「metadata
# 有就套用」，前綴檢查整段刪掉。決策 E 落地後 openrouter/ 前綴只是命名慣例，
# 不再是功能開關。


def _infer_default_key_policy(model_name: str) -> str:
    """沒有明確政策的模型：openrouter/ 開頭視為 dept:openrouter，其餘視為 model——
    跟決策 E 之前的唯一行為完全一致，既有模型不需要任何資料回填。
    """
    return "dept:openrouter" if model_name.startswith("openrouter/") else "model"


def _resolve_injected_key(model_name: str, dept: dict) -> str:
    """回傳這次請求該注入的 api_key；"" 表示不注入（沿用模型自己定義的 key）。"""
    policies = _load_config().get("model_key_policies", {})
    policy = policies.get(model_name) or _infer_default_key_policy(model_name)

    if not policy.startswith("dept:"):
        return ""

    provider = policy.split(":", 1)[1]
    key = (dept.get("provider_keys") or {}).get(provider, "")
    if key.startswith("sk-or-CHANGE"):
        return ""  # 未換過的 placeholder，視同未設定，不注入（見「容易做錯的五件事」#5）
    return key


# ── 部門層 Rate Limit（in-memory，單 replica 適用）────────────────────────────

_DEPT_COUNTERS: dict[str, dict] = {}
_DEPT_LOCK = Lock()


def _check_dept_rate_limit(dept: dict, token_estimate: int = 0) -> None:
    dept_id = dept["dept_id"]
    now = time.monotonic()
    window = 60.0

    with _DEPT_LOCK:
        if dept_id not in _DEPT_COUNTERS:
            _DEPT_COUNTERS[dept_id] = {
                "rpm": {"count": 0, "window_start": now},
                "tpm": {"count": 0, "window_start": now},
            }

        c = _DEPT_COUNTERS[dept_id]

        if now - c["rpm"]["window_start"] >= window:
            c["rpm"] = {"count": 0, "window_start": now}
        if now - c["tpm"]["window_start"] >= window:
            c["tpm"] = {"count": 0, "window_start": now}

        rpm_limit = dept.get("dept_rpm_limit")
        tpm_limit = dept.get("dept_tpm_limit")

        if rpm_limit and c["rpm"]["count"] >= rpm_limit:
            raise ProxyException(
                message=f"Department '{dept_id}' has exceeded RPM limit ({rpm_limit})",
                type="rate_limit_error",
                param="rpm",
                code=429,
            )
        if tpm_limit and token_estimate and c["tpm"]["count"] >= tpm_limit:
            raise ProxyException(
                message=f"Department '{dept_id}' has exceeded TPM limit ({tpm_limit})",
                type="rate_limit_error",
                param="tpm",
                code=429,
            )

        c["rpm"]["count"] += 1
        c["tpm"]["count"] += token_estimate


# ── Model 權限檢查 ────────────────────────────────────────────────────────────

def _check_model_allowed(user: dict, dept: dict, model: str | None) -> None:
    if not model:
        return

    eff = _effective_models(user, dept)
    if eff is None or model in eff:  # None = 完全不限制
        return

    write_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "auth_denied",
        "reason": "model_not_allowed",
        "user_id": user["user_id"],
        "key_name": user.get("key_name"),
        "dept_id": dept["dept_id"],
        "model": model,
    })
    raise ProxyException(
        message=f"Model '{model}' is not allowed for this user",
        type="auth_error",
        param="model",
        code=403,
    )


# ── Request body 解析 ─────────────────────────────────────────────────────────

async def _get_requested_model(request: Request) -> str | None:
    try:
        body_bytes = await request.body()
        if not body_bytes:
            return None
        return json.loads(body_bytes).get("model")
    except Exception:
        return None


# ── 主要認證入口 ──────────────────────────────────────────────────────────────

_HEALTH_PATHS = {"/health", "/health/readiness", "/health/liveness", "/"}


# LiteLLM 原生特殊值：表示這把 key 不能存取任何預設模型。
# （LiteLLM 把 models=[] 當成「不限制=全部」，所以零權限必須用此哨兵而非空陣列）
NO_ACCESS_SENTINEL = "no-default-models"


def _effective_models(user: dict, dept: dict) -> list[str] | None:
    """使用者實際可用的模型。回傳 None = 完全不限制（全部）；[] = 無任何權限。

    加法模型（OpenWebUI 主導架構）：
      effective = dept.allowed_models ∪ user.models
      - dept.allowed_models：部門（OpenWebUI group）被授權的模型
      - user.models：個別授權給此使用者的額外模型
      - 聯集為空 → 拒絕（未明確授權 = 沒人能用）
      - 任一邊含 "*" → 完全不限制
    """
    user_models = user.get("models", [])
    dept_models = dept.get("allowed_models", [])

    if "*" in dept_models or "*" in user_models:
        return None

    merged = list(dict.fromkeys([*dept_models, *user_models]))  # 聯集去重保序
    return merged


def _build_auth_response(
    user: dict, dept: dict, api_key: str, source: str = "api_key", requested_model: str | None = None
) -> UserAPIKeyAuth:
    metadata: dict = dict(user.get("metadata", {}))
    metadata["dept_id"] = dept["dept_id"]
    metadata["dept_name"] = dept.get("dept_name", "")
    # 決策 E：這裡已經解析完「這次請求該用哪把 key」，custom_logger 不再需要自己
    # 判斷 model 字串前綴——metadata 裡有值就套用，沒有就維持模型自己定義的 key。
    metadata["injected_api_key"] = _resolve_injected_key(requested_model, dept) if requested_model else ""
    metadata["portal"] = source  # 請求來源入口（portal-a / portal-b / api_key），供用量分流

    eff = _effective_models(user, dept)
    if eff is None:
        allowed = []                       # LiteLLM: 空 = 全部
    elif len(eff) == 0:
        allowed = [NO_ACCESS_SENTINEL]     # 零權限 → /models 回傳空、呼叫全 403
    else:
        allowed = eff

    return UserAPIKeyAuth(
        api_key=api_key,
        key_name=user.get("key_name"),
        key_alias=user.get("key_name"),
        user_id=user["user_id"],
        user_email=user.get("user_email"),
        models=allowed,
        aliases=user.get("aliases", {}),
        rpm_limit=user.get("rpm_limit"),
        tpm_limit=user.get("tpm_limit"),
        max_budget=user.get("max_budget"),
        budget_duration=user.get("budget_duration"),
        allowed_routes=user.get("allowed_routes"),
        metadata=metadata,
    )


async def _resolve_user(request: Request, api_key: str) -> tuple[dict, str]:
    """回傳 (user, source)。source 用於標記請求來源入口（寫入 metadata / log）。

    路線一：Bearer token 是個人 api_key → 直接查 SQLite。source="api_key"。
    路線二：Bearer token 對應某 OpenWebUI 入口的 service key → 讀 X-OpenWebUI-User-Id header，
            查該入口的 API 取得 Keycloak UUID，再查 SQLite。source=該入口名稱（portal-a/portal-b）。
    """
    normalized = _normalize_api_key(api_key)

    # ── 路線二：OpenWebUI 模式（依 service key 對應到來源入口）──────────────────
    instance = OPENWEBUI_INSTANCES.get(normalized)
    if instance:
        portal = instance["name"]
        openwebui_id = request.headers.get("x-openwebui-user-id", "").strip()
        if not openwebui_id:
            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "auth_denied",
                "reason": "missing_openwebui_user_id",
                "portal": portal,
            })
            raise ProxyException(
                message="X-OpenWebUI-User-Id header is required",
                type="authentication_error",
                param="x-openwebui-user-id",
                code=401,
            )

        keycloak_sub = await _resolve_keycloak_sub(
            openwebui_id, instance["url"], instance["admin_key"]
        )
        if not keycloak_sub:
            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "auth_denied",
                "reason": "openwebui_resolve_failed",
                "openwebui_id": openwebui_id,
                "portal": portal,
            })
            raise ProxyException(
                message="Cannot resolve OpenWebUI user to Keycloak identity",
                type="authentication_error",
                param="x-openwebui-user-id",
                code=401,
            )

        user = _find_user_by_user_id(keycloak_sub)
        if user is None:
            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "auth_denied",
                "reason": "user_not_found",
                "openwebui_id": openwebui_id,
                "keycloak_sub": keycloak_sub,
                "portal": portal,
            })
            raise ProxyException(
                message="User not found or blocked",
                type="authentication_error",
                param="x-openwebui-user-id",
                code=401,
            )

        return user, portal

    # ── 路線一：api_key 模式 ────────────────────────────────────────────────────
    user = _find_user(api_key)
    if user is None:
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "auth_denied",
            "reason": "invalid_key",
        })
        raise ProxyException(
            message="Invalid API key",
            type="authentication_error",
            param="api_key",
            code=401,
        )

    return user, "api_key"


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    if request.url.path in _HEALTH_PATHS:
        return UserAPIKeyAuth(api_key="health-check")

    normalized = _normalize_api_key(api_key)

    # ── Master key：管理用途（admin-api 查模型清單等），全權限 ─────────────────
    # user_role 必須明確設成 PROXY_ADMIN：LiteLLM 內建的「master key → PROXY_ADMIN」
    # 判斷只存在於它自己的 user_api_key_auth（見 litellm/proxy/auth/user_api_key_auth.py），
    # 這個 custom_auth 完全取代了那條路徑（custom_auth_run_common_checks: false），
    # 沒有明確設定的話 UserAPIKeyAuth.user_role 預設是 None。GET /model/info 這類
    # 端點不檢查角色所以感覺沒事，但 POST /model/new、/model/delete 會在
    # ModelManagementAuthChecks.can_user_make_model_call 檢查角色，None 一律 403
    # 「Your role=None」——外部模型自助上架（見 docs/external-models-ops.md「路線 C」）
    # 的 admin-api 代理端點就是靠這裡吃到 PROXY_ADMIN 才打得通。
    master_key = os.getenv("LITELLM_MASTER_KEY", "")
    if master_key and normalized == master_key:
        return UserAPIKeyAuth(
            api_key=normalized, models=[], user_role=LitellmUserRoles.PROXY_ADMIN
        )  # models=[] → 全部

    # ── OpenWebUI 全域 model 探索 ──────────────────────────────────────────────
    # OpenWebUI 由管理員在「連線」層級抓 model list，用 service key 但不帶 user header。
    # 這種請求無法 per-user 過濾，放行並回傳全部模型；per-user 權限改由 chat 時 enforce。
    _MODEL_LIST_PATHS = ("/models", "/v1/models", "/model/info")
    if (
        normalized in OPENWEBUI_INSTANCES
        and not request.headers.get("x-openwebui-user-id", "").strip()
        and request.url.path in _MODEL_LIST_PATHS
    ):
        return UserAPIKeyAuth(api_key=normalized, models=[])  # models=[] → 全部

    user, source = await _resolve_user(request, api_key)

    dept = _find_dept(user.get("dept_id", ""))
    if dept is None:
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "auth_denied",
            "reason": "dept_not_found",
            "user_id": user["user_id"],
            "dept_id": user.get("dept_id"),
        })
        raise ProxyException(
            message="User's department is not configured",
            type="auth_error",
            param="dept_id",
            code=403,
        )

    requested_model = await _get_requested_model(request)
    _check_model_allowed(user, dept, requested_model)
    _check_dept_rate_limit(dept)

    return _build_auth_response(user, dept, _normalize_api_key(api_key), source, requested_model)

if __name__ == "__main__":
    import asyncio
    test_openwebui_id = "6564ea72-0063-4474-8d0d-7a9072bc68cb"
    keycloak_sub = asyncio.run(
        _resolve_keycloak_sub(test_openwebui_id, OPENWEBUI_URL, OPENWEBUI_ADMIN_KEY)
    )
    print(f"OpenWebUI ID {test_openwebui_id} resolves to Keycloak sub: {keycloak_sub}")
