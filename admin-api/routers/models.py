import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import verify_admin_key

router = APIRouter(prefix="/api/v1/models", dependencies=[Depends(verify_admin_key)])

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")


@router.get("")
async def list_models():
    """列出 LiteLLM 目前設定的可用模型，供 UI 選取時使用。"""
    if not LITELLM_MASTER_KEY:
        raise HTTPException(status_code=500, detail="LITELLM_MASTER_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LITELLM_URL}/models",
                headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="LiteLLM returned non-200 response")

    data = resp.json()
    model_ids = sorted(item["id"] for item in data.get("data", []))
    return {"models": model_ids}
