"""上架模型表單的「上游」固定枚舉（R-16）。

決策 D 已定案的 provider 枚舉七項：OpenRouter、OpenAI、Anthropic、Gemini、
地端 vLLM、地端 Ollama、其他 OpenAI 相容。上游用固定枚舉、不讓自由輸入——自由
輸入等於把 provider 前綴的正確性丟回給使用者，這正是這個表單存在的理由。
需要新 provider 時改這裡加一項，而不是開放自由輸入。

跟 config/custom_auth.py 的 key_policy 解耦（決策 E）：這裡只決定表單怎麼問、
litellm_params.model/api_base 怎麼推導、model_name 怎麼建議；「要不要用部門 key」
最終仍是 models_service.create_external_model 存進 model_key_policies 的
key_policy 欄位，跟這裡的 upstream key 本身無關。
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
    key_mode: str  # "choice"（各部門自己／共用一把）｜"fixed_shared"（固定用 EMPTY，沒有選擇）
    provider: str | None  # provider_keys 的 key 名稱；fixed_shared 沒有部門 key 概念，設 None
    name_prefix_dept: str  # key_mode=dept 時 model_name 建議前綴
    name_prefix_shared: str  # key_mode=shared（或 fixed_shared）時 model_name 建議前綴


UPSTREAMS: dict[str, Upstream] = {
    "openrouter": Upstream(
        key="openrouter", label="OpenRouter",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default="https://openrouter.ai/api/v1", api_base_hint="",
        key_mode="choice", provider="openrouter",
        name_prefix_dept="openrouter/", name_prefix_shared="",
    ),
    "openai": Upstream(
        key="openai", label="OpenAI 官方",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_mode="choice", provider="openai",
        name_prefix_dept="openai-dept/", name_prefix_shared="",
    ),
    "anthropic": Upstream(
        key="anthropic", label="Anthropic 官方",
        model_template="anthropic/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_mode="choice", provider="anthropic",
        name_prefix_dept="anthropic-dept/", name_prefix_shared="",
    ),
    "gemini": Upstream(
        key="gemini", label="Gemini 官方",
        model_template="gemini/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_mode="choice", provider="gemini",
        name_prefix_dept="gemini-dept/", name_prefix_shared="",
    ),
    "vllm": Upstream(
        key="vllm", label="地端 vLLM",
        model_template="hosted_vllm/{slug}",
        api_base_mode="required", api_base_default=None,
        api_base_hint="Service DNS，要帶 /v1，例如 http://my-vllm-service:8000/v1",
        key_mode="fixed_shared", provider=None,
        name_prefix_dept="", name_prefix_shared="",
    ),
    "ollama": Upstream(
        key="ollama", label="地端 Ollama",
        model_template="ollama/{slug}",
        api_base_mode="required", api_base_default="http://ollama-service:11434",
        api_base_hint="Service DNS，不帶 /v1",
        key_mode="fixed_shared", provider=None,
        name_prefix_dept="ollama/", name_prefix_shared="ollama/",
    ),
    "other": Upstream(
        key="other", label="其他 OpenAI 相容",
        model_template="openai/{slug}",
        api_base_mode="required", api_base_default=None, api_base_hint="",
        key_mode="choice", provider="other",
        name_prefix_dept="other-dept/", name_prefix_shared="",
    ),
}

FIXED_SHARED_KEY = "EMPTY"  # vLLM/Ollama 固定共用值；LiteLLM 不驗證這個值


def derive_model(upstream: Upstream, slug: str) -> str:
    return upstream.model_template.format(slug=slug)


def derive_api_base(upstream: Upstream, user_input: str) -> str | None:
    if upstream.api_base_mode == "auto":
        return upstream.api_base_default  # 可能是 None（雲端官方端點留空即可）
    return user_input.strip() or upstream.api_base_default


def suggest_model_name(upstream: Upstream, slug: str, key_source: str) -> str:
    """R-17：系統建議、允許覆寫。key_source 是 'dept' 或 'shared'（fixed_shared 一律當 shared）。"""
    prefix = upstream.name_prefix_dept if key_source == "dept" else upstream.name_prefix_shared
    return f"{prefix}{slug}"


def looks_like_ip(value: str) -> bool:
    """R-24 的非阻斷提醒：api_base 填了節點 IP 而不是 Service DNS。"""
    host = value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)
