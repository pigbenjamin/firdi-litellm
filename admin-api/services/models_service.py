"""LiteLLM 模型管理的業務邏輯，不帶認證。

刻意不吃任何 auth 相關參數/依賴——`routers/models.py`（ADMIN_API_KEY）與未來
`admin-web` 的模型管理頁（Keycloak session）各自認證後呼叫這裡的函式，避免網頁層
繞去用 ADMIN_API_KEY 打自己的 API（見 docs/admin-web-plan.md 決策 C）。

# ── 外部模型自助上架（見 docs/external-models-ops.md「路線 C」）────────────────
# 這一組函式是 LiteLLM `/model/new` `/model/info` `/model/delete` 的薄代理：
# admin-api 拿 LITELLM_MASTER_KEY 去呼叫 LiteLLM，讓呼叫者不需要拿到 LiteLLM
# master key、也不需要任何 kubectl 存取，就能新增/查詢/刪除動態註冊到 Postgres
# 的模型（store_model_in_db，不影響 config/litellm_config.yaml 那份 YAML
# model_list，也不會重啟 litellm pod）。
"""
import os

import httpx
from fastapi import HTTPException

from database import DB_PATH, get_conn
from models import ExternalModelIn

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")


def _litellm_client() -> httpx.AsyncClient:
    if not LITELLM_MASTER_KEY:
        raise HTTPException(status_code=500, detail="LITELLM_MASTER_KEY not configured")
    return httpx.AsyncClient(
        base_url=LITELLM_URL,
        headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
        timeout=15,
    )


# ── 決策 E：key 來源政策（見 docs/admin-web-plan.md）───────────────────────────
# 上游（litellm_params.model）跟 key 從哪來解耦：政策存 admin-api 自己的 SQLite
# （model_key_policies 表），不存 LiteLLM 的 model_info——後者會讓
# config/custom_auth.py 在每個請求的熱路徑上多一個 Postgres 相依，不值得。


def _infer_default_key_policy(model_name: str) -> str:
    """沒有明確政策時的預設值，跟決策 E 之前的唯一行為（只有 openrouter/ 前綴
    會觸發部門 key 注入）完全一致，既有模型不需要任何資料回填。
    """
    return "dept:openrouter" if model_name.startswith("openrouter/") else "model"


def _validate_key_policy(policy: str) -> None:
    if policy == "model":
        return
    if policy.startswith("dept:") and len(policy) > len("dept:"):
        return
    raise HTTPException(
        status_code=422,
        detail=f"key_policy 格式錯誤：'{policy}'，需為 'model' 或 'dept:<provider>'（如 'dept:openai'）",
    )


def _set_key_policy(model_name: str, policy: str) -> None:
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO model_key_policies (model_name, key_policy) VALUES (?, ?) "
            "ON CONFLICT(model_name) DO UPDATE SET key_policy=excluded.key_policy",
            (model_name, policy),
        )


async def _cleanup_orphaned_key_policy(model_name: str) -> None:
    """model_name 底下已無其他 deployment 時才刪政策紀錄，避免同名模型日後重建被
    舊政策悄悄套用；查不到（LiteLLM 連不上）就放著，不影響刪除本身——下次用同名
    重新上架時 _set_key_policy 的 upsert 一樣會覆蓋掉舊值。
    """
    async with _litellm_client() as client:
        try:
            resp = await client.get("/model/info")
        except httpx.RequestError:
            return
    if resp.status_code == 200:
        if any(item.get("model_name") == model_name for item in resp.json().get("data", [])):
            return  # 還有其他 deployment 用同一個 model_name，政策仍在使用中
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM model_key_policies WHERE model_name=?", (model_name,))


async def list_models() -> dict:
    async with _litellm_client() as client:
        try:
            resp = await client.get("/models")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="LiteLLM returned non-200 response")

    data = resp.json()
    model_ids = sorted(item["id"] for item in data.get("data", []))
    return {"models": model_ids}


async def list_external_models() -> dict:
    async with _litellm_client() as client:
        try:
            resp = await client.get("/model/info")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/info failed: {resp.text}")

    db_items = [
        item for item in resp.json().get("data", [])
        if (item.get("model_info") or {}).get("db_model")
    ]

    with get_conn(DB_PATH) as conn:
        stored_policies = {
            row["model_name"]: row["key_policy"]
            for row in conn.execute("SELECT model_name, key_policy FROM model_key_policies").fetchall()
        }

    out = []
    for item in db_items:
        info = item.get("model_info") or {}
        litellm_params = item.get("litellm_params") or {}
        model_name = item.get("model_name") or ""
        out.append(
            {
                "id": info.get("id"),
                "model_name": model_name,
                "model": litellm_params.get("model"),
                "api_base": litellm_params.get("api_base"),
                "key_policy": stored_policies.get(model_name) or _infer_default_key_policy(model_name),
            }
        )
    return {"models": out}


async def create_external_model(body: ExternalModelIn) -> dict:
    """上架後這個模型還沒有任何人能打——跟 YAML model_list 定義的模型一樣，權限
    (`allowed_models`) 是分開的一件事，一定要另外到 OpenWebUI 開通（見
    docs/external-models.md）。
    """
    # R-20：model / model_name 不可為空白字串——目前 pydantic 只驗證是字串，空字串
    # 會通過驗證再到 LiteLLM 失敗，變成難懂的 502，這個檢查補在這裡而不是表單，
    # 讓 curl 呼叫者也受益。
    if not body.model_name.strip():
        raise HTTPException(status_code=422, detail="model_name 不可為空白字串")
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model 不可為空白字串")

    key_policy = body.key_policy or _infer_default_key_policy(body.model_name)
    _validate_key_policy(key_policy)
    uses_dept_key = key_policy.startswith("dept:")
    is_openrouter = body.model_name.startswith("openrouter/")  # 只影響 api_base 的自動預設值

    async with _litellm_client() as client:
        try:
            existing = await client.get("/model/info")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
        if existing.status_code == 200:
            matches = [item for item in existing.json().get("data", []) if item.get("model_name") == body.model_name]
            if matches:
                is_db_managed = bool((matches[0].get("model_info") or {}).get("db_model"))
                origin = "DB-managed（可從 UI 刪除後重建）" if is_db_managed else "YAML model_list 定義（不能從 UI 刪除，需改 config/litellm_config.yaml 並重啟 litellm pod）"
                raise HTTPException(
                    status_code=409,
                    detail=f"model_name '{body.model_name}' 已存在，是 {origin}。換一個名字，"
                    "或先用 DELETE /api/v1/models/external/{id} 刪除舊的（僅限 DB-managed）",
                )

        litellm_params: dict = {"model": body.model}
        if body.api_key:
            litellm_params["api_key"] = body.api_key
        elif uses_dept_key:
            # 跟 YAML model_list 現有 openrouter 那幾筆一樣用共用 placeholder；這個值
            # 只在 custom_auth 找不到對應部門 key 時才會真的被拿去打上游（會 401，
            # 這是刻意的失敗模式，見 docs/admin-web-plan.md「容易做錯的五件事」#5）。
            # 沿用既有 OPENROUTER_API_KEY_PLACEHOLDER 這個環境變數名稱，即使現在
            # 也給非 openrouter 的 dept:* 政策用——重新命名要動 k8s deployment
            # manifest，留給部署階段一併處理，不在這裡做。
            litellm_params["api_key"] = "os.environ/OPENROUTER_API_KEY_PLACEHOLDER"
        else:
            raise HTTPException(
                status_code=422,
                detail="api_key 必填（key_policy='model' 時一定要提供；"
                "key_policy='dept:<provider>' 可留空，由該部門的 provider_keys 動態注入）",
            )
        if body.api_base:
            litellm_params["api_base"] = body.api_base
        elif is_openrouter:
            litellm_params["api_base"] = "https://openrouter.ai/api/v1"

        try:
            resp = await client.post(
                "/model/new",
                json={"model_name": body.model_name, "litellm_params": litellm_params},
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/new failed: {resp.text}")

    # 成功建立才落政策，避免上架失敗卻留下孤兒政策紀錄。
    _set_key_policy(body.model_name, key_policy)

    return {"model_name": body.model_name, "status": "created", "key_policy": key_policy}


async def delete_external_model(model_id: str) -> None:
    """model_id 是 GET /api/v1/models/external 回傳的 id，不是 model_name（同一個
    model_name 理論上可以有多筆 deployment 做負載平衡）。
    """
    async with _litellm_client() as client:
        model_name = None
        try:
            existing = await client.get("/model/info")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
        if existing.status_code == 200:
            for item in existing.json().get("data", []):
                if (item.get("model_info") or {}).get("id") == model_id:
                    model_name = item.get("model_name")
                    break

        try:
            resp = await client.post("/model/delete", json={"id": model_id})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/delete failed: {resp.text}")

    if model_name:
        await _cleanup_orphaned_key_policy(model_name)
