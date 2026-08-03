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
