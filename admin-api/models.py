from typing import Any, Literal
from pydantic import BaseModel, Field


class DepartmentIn(BaseModel):
    dept_id: str
    dept_name: str
    openrouter_api_key: str = ""
    allowed_models: list[str] = Field(default_factory=list)
    dept_rpm_limit: int | None = None
    dept_tpm_limit: int | None = None


class DepartmentPatch(BaseModel):
    dept_name: str | None = None
    openrouter_api_key: str | None = None
    allowed_models: list[str] | None = None
    dept_rpm_limit: int | None = None
    dept_tpm_limit: int | None = None


class DepartmentOut(BaseModel):
    dept_id: str
    dept_name: str
    openrouter_api_key: str
    allowed_models: list[str]
    dept_rpm_limit: int | None
    dept_tpm_limit: int | None
    created_at: str
    updated_at: str


class UserIn(BaseModel):
    api_key: str
    key_name: str
    user_id: str
    user_email: str | None = None
    dept_id: str
    account_type: Literal["human", "service"] = "human"
    models: list[str] = Field(default_factory=list)
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    aliases: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    blocked: bool = False


class UserPatch(BaseModel):
    key_name: str | None = None
    user_email: str | None = None
    dept_id: str | None = None
    account_type: Literal["human", "service"] | None = None
    models: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    aliases: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    blocked: bool | None = None


class MeOut(BaseModel):
    """自助端點 /api/v1/me 的回應：只給使用者自己需要知道的欄位。

    刻意不含 rpm/tpm/metadata/部門 openrouter key 等管理面資訊。
    """
    user_id: str
    key_name: str
    user_email: str | None
    dept_id: str
    api_key: str
    allowed_models: list[str]  # 部門 ∪ 個人 的聯集；["*"] = 不限制
    blocked: bool


class ExternalModelIn(BaseModel):
    """自助上架外部模型（見 docs/external-models-ops.md「路線 C」）。

    對應 LiteLLM `/model/new` 的簡化版輸入，讓「使用管理者」不需要知道 LiteLLM
    的完整 Deployment schema。model_name 走 `openrouter/` 前綴代表要用 OpenRouter
    路線——這個慣例跟這個模型是 YAML model_list 定義還是 DB-managed 無關，
    config/custom_logger.py 的 async_pre_call_hook 只看請求當下的 model 字串
    是否以 openrouter/ 開頭，來決定要不要注入呼叫者的部門 openrouter_api_key。
    """
    model_name: str
    model: str  # litellm_params.model，如 "openai/gpt-4o-mini" 或 openrouter 路線的 "openai/anthropic/claude-sonnet-4-5"
    api_key: str | None = None  # 原生 Provider 路線必填；openrouter 路線留空則用共用 placeholder，實際 key 由部門設定動態注入
    api_base: str | None = None  # 原生 Provider 若非官方預設端點才需要；openrouter 路線留空則自動帶 https://openrouter.ai/api/v1


class ExternalModelOut(BaseModel):
    id: str
    model_name: str
    model: str
    api_base: str | None


class UserOut(BaseModel):
    api_key: str
    key_name: str
    user_id: str
    user_email: str | None
    dept_id: str
    account_type: Literal["human", "service"]
    models: list[str]
    rpm_limit: int | None
    tpm_limit: int | None
    aliases: dict[str, Any]
    metadata: dict[str, Any]
    blocked: bool
    created_at: str
    updated_at: str
