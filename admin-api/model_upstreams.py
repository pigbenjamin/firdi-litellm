"""上架模型表單的「上游」固定枚舉（R-16）。

決策 D 已定案的 provider 枚舉七項：OpenRouter、OpenAI、Anthropic、Gemini、
地端 vLLM、地端 Ollama、其他 OpenAI 相容。上游用固定枚舉、不讓自由輸入——自由
輸入等於把 provider 前綴的正確性丟回給使用者，這正是這個表單存在的理由。
需要新 provider 時改這裡加一項，而不是開放自由輸入。

跟 config/custom_auth.py 的 key_policy 解耦（決策 E）：這裡只決定表單怎麼問、
litellm_params.model/api_base 怎麼推導、model_name 怎麼建議；「要不要用部門 key」
最終仍是 models_service.create_external_model 存進 model_key_policies 的
key_policy 欄位，跟這裡的 upstream key 本身無關。

**上架動線已不再詢問「key 從哪來」**（第三期）：新模型一律是「模型自帶 key」
（key_policy='model'），所以這裡只剩 key_required 一個布林——要不要填 key。要給
特定部門專屬 key 的做法改成「同一個上游再上架一個模型」：名稱加後綴
（例如 gpt-4o-deptA）、填該部門的 key，開給誰仍然在模型授權頁決定，不限一個部門。

provider 欄位保留：決策 E 時期建立的 dept:<provider> 模型還在跑，「Provider Key」
頁面要靠它列出可設定的 provider（見 routers/admin_web_write.py 的舊制區塊）。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Upstream:
    key: str
    label: str
    model_template: str  # {slug} 會被使用者填的值取代
    api_base_mode: str  # "auto"（系統推導，表單不顯示）｜"required"（必填）
    api_base_default: str | None  # required 模式下的建議預設值
    api_base_hint: str
    key_required: bool  # True＝表單要填 API key｜False＝固定用 FIXED_SHARED_KEY，不問
    provider: str | None  # provider_keys 的 key 名稱（僅舊制 dept:<provider> 模型與 Provider Key 頁面用）
    name_prefix: str  # model_name 的建議前綴


UPSTREAMS: dict[str, Upstream] = {
    "openrouter": Upstream(
        key="openrouter", label="OpenRouter",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default="https://openrouter.ai/api/v1", api_base_hint="",
        key_required=True, provider="openrouter", name_prefix="",
    ),
    "openai": Upstream(
        key="openai", label="OpenAI 官方",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, provider="openai", name_prefix="",
    ),
    "anthropic": Upstream(
        key="anthropic", label="Anthropic 官方",
        model_template="anthropic/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, provider="anthropic", name_prefix="",
    ),
    "gemini": Upstream(
        key="gemini", label="Gemini 官方",
        model_template="gemini/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, provider="gemini", name_prefix="",
    ),
    "vllm": Upstream(
        key="vllm", label="地端 vLLM",
        model_template="hosted_vllm/{slug}",
        api_base_mode="required", api_base_default=None,
        api_base_hint="Service DNS，要帶 /v1，例如 http://my-vllm-service:8000/v1",
        key_required=False, provider=None, name_prefix="",
    ),
    "ollama": Upstream(
        key="ollama", label="地端 Ollama",
        model_template="ollama/{slug}",
        api_base_mode="required", api_base_default="http://ollama-service:11434",
        api_base_hint="Service DNS，不帶 /v1",
        key_required=False, provider=None, name_prefix="ollama/",
    ),
    "other": Upstream(
        key="other", label="其他 OpenAI 相容",
        model_template="openai/{slug}",
        api_base_mode="required", api_base_default=None, api_base_hint="",
        key_required=True, provider="other", name_prefix="",
    ),
}

FIXED_SHARED_KEY = "EMPTY"  # vLLM/Ollama 固定共用值；LiteLLM 不驗證這個值


def derive_model(upstream: Upstream, slug: str) -> str:
    return upstream.model_template.format(slug=slug)


def derive_api_base(upstream: Upstream, user_input: str) -> str | None:
    if upstream.api_base_mode == "auto":
        return upstream.api_base_default  # 可能是 None（雲端官方端點留空即可）
    return user_input.strip() or upstream.api_base_default


def suggest_model_name(upstream: Upstream, slug: str) -> str:
    """R-17：系統建議、允許覆寫。

    刻意不再建議 openrouter/ 前綴：config/custom_auth.py 的
    _infer_default_key_policy 會把該前綴推導成 dept:openrouter，雖然上架時一定會
    寫一筆明確的 key_policy 紀錄（推導不會生效），但名稱本身容易讓人誤會這個模型
    走的是部門 key。
    """
    return f"{upstream.name_prefix}{slug}"


def looks_like_ip(value: str) -> bool:
    """R-24 的非阻斷提醒：api_base 填了節點 IP 而不是 Service DNS。"""
    host = value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)
