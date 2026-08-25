from fastapi import APIRouter, Depends

from auth import verify_admin_key
from models import ExternalModelIn
from services import models_service

router = APIRouter(prefix="/api/v1/models", dependencies=[Depends(verify_admin_key)])


@router.get("")
async def list_models():
    """列出 LiteLLM 目前設定的可用模型，供 UI 選取時使用。"""
    return await models_service.list_models()


@router.get("/external")
async def list_external_models():
    """列出目前 DB-managed（store_model_in_db）的模型，不含 YAML model_list 定義的那些。"""
    return await models_service.list_external_models()


@router.post("/external", status_code=201)
async def create_external_model(body: ExternalModelIn):
    """自助上架一個外部模型（OpenRouter 或原生 Provider），不需改 YAML、不需重啟 litellm pod。"""
    return await models_service.create_external_model(body)


@router.delete("/external/{model_id}", status_code=204)
async def delete_external_model(model_id: str):
    """刪除一個 DB-managed 模型。"""
    await models_service.delete_external_model(model_id)
