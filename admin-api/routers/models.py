import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import verify_admin_key
from models import ExternalModelIn

router = APIRouter(prefix="/api/v1/models", dependencies=[Depends(verify_admin_key)])

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


@router.get("")
async def list_models():
    """列出 LiteLLM 目前設定的可用模型，供 UI 選取時使用。"""
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


# ── 外部模型自助上架（見 docs/external-models-ops.md「路線 C」）────────────────
# 這一組端點是 LiteLLM `/model/new` `/model/info` `/model/delete` 的薄代理：
# admin-api 拿 LITELLM_MASTER_KEY 去呼叫 LiteLLM，讓持有 ADMIN_API_KEY 的呼叫者
# （沿用既有角色/認證機制，跟 /api/v1/departments 的 PATCH 一樣）不需要拿到
# LiteLLM master key、也不需要任何 kubectl 存取，就能新增/查詢/刪除動態註冊到
# Postgres 的模型（store_model_in_db，不影響 config/litellm_config.yaml 那份
# YAML model_list，也不會重啟 litellm pod）。


@router.get("/external")
async def list_external_models():
    """列出目前 DB-managed（store_model_in_db）的模型，不含 YAML model_list 定義的那些。"""
    async with _litellm_client() as client:
        try:
            resp = await client.get("/model/info")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/info failed: {resp.text}")

    out = []
    for item in resp.json().get("data", []):
        info = item.get("model_info") or {}
        if not info.get("db_model"):
            continue
        litellm_params = item.get("litellm_params") or {}
        out.append(
            {
                "id": info.get("id"),
                "model_name": item.get("model_name"),
                "model": litellm_params.get("model"),
                "api_base": litellm_params.get("api_base"),
            }
        )
    return {"models": out}


@router.post("/external", status_code=201)
async def create_external_model(body: ExternalModelIn):
    """自助上架一個外部模型（OpenRouter 或原生 Provider），不需改 YAML、不需重啟 litellm pod。

    上架後這個模型還沒有任何人能打——跟 YAML model_list 定義的模型一樣，權限
    (`allowed_models`) 是分開的一件事，一定要另外到 OpenWebUI 開通（見
    docs/external-models.md）。
    """
    async with _litellm_client() as client:
        try:
            existing = await client.get("/model/info")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
        if existing.status_code == 200:
            names = {item.get("model_name") for item in existing.json().get("data", [])}
            if body.model_name in names:
                raise HTTPException(
                    status_code=409,
                    detail=f"model_name '{body.model_name}' 已存在（YAML 或 DB-managed 皆算），"
                    "換一個名字，或先用 DELETE /api/v1/models/external/{id} 刪除舊的",
                )

        litellm_params: dict = {"model": body.model}
        is_openrouter = body.model_name.startswith("openrouter/")
        if body.api_key:
            litellm_params["api_key"] = body.api_key
        elif is_openrouter:
            # 跟 YAML model_list 現有 openrouter 那幾筆一樣用共用 placeholder；
            # 實際 key 由 custom_logger.py 依呼叫者的部門動態注入，見上方模組註解。
            litellm_params["api_key"] = "os.environ/OPENROUTER_API_KEY_PLACEHOLDER"
        else:
            raise HTTPException(
                status_code=422,
                detail="api_key 必填（原生 Provider 路線一定要提供；openrouter/ 路線可留空，"
                "由部門 openrouter_api_key 動態注入）",
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

    return {"model_name": body.model_name, "status": "created"}


@router.delete("/external/{model_id}", status_code=204)
async def delete_external_model(model_id: str):
    """刪除一個 DB-managed 模型。model_id 是 GET /api/v1/models/external 回傳的 id，

    不是 model_name（同一個 model_name 理論上可以有多筆 deployment 做負載平衡）。
    """
    async with _litellm_client() as client:
        try:
            resp = await client.post("/model/delete", json={"id": model_id})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/delete failed: {resp.text}")
