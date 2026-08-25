"""OpenWebUI ↔ DB 模型權限同步端點。業務邏輯見 services/openwebui_sync_service.py。"""
from fastapi import APIRouter, Depends

from auth import verify_admin_key
from services import openwebui_sync_service

router = APIRouter(
    prefix="/api/v1/sync/openwebui",
    dependencies=[Depends(verify_admin_key)],
)


@router.post("/pull-models")
async def pull_openwebui_model_access(dry_run: bool = False):
    """把 OpenWebUI 各模型的 access_grants 反算回 DB：
    dept.allowed_models ← group grants；users.models ← user grants。
    只在有差異時寫入並 bump 版本。
    """
    return await openwebui_sync_service.pull_openwebui_model_access(dry_run=dry_run)


@router.post("/models")
async def push_model_access_to_openwebui(target: str = "a", dry_run: bool = False):
    """把 DB 的權限完整鏡像到指定 OpenWebUI 入口（group + user 兩層都取代）。

    target=a（預設）：鏡像到權威入口 A。DB 側改權限的 SOP：pull → PATCH → push（target=a）。
    target=b：鏡像到第二入口 B（唯讀鏡像，由 CronJob 每 2 分鐘自動 push 使 B 對齊 A）。
              B 未設定時安全 no-op（回 status=skipped），方便 CronJob 先掛上、B 上線前不報錯。
    """
    return await openwebui_sync_service.push_model_access_to_openwebui(target=target, dry_run=dry_run)
