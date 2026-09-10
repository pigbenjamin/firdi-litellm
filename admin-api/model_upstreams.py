"""上架模型表單的「上游」固定枚舉（R-16）。

決策 D 已定案的 provider 枚舉七項：OpenRouter、OpenAI、Anthropic、Gemini、
地端 vLLM、地端 Ollama、其他 OpenAI 相容。上游用固定枚舉、不讓自由輸入——自由
輸入等於把 provider 前綴的正確性丟回給使用者，這正是這個表單存在的理由。
需要新 provider 時改這裡加一項，而不是開放自由輸入。

**一律模型自帶 key**（2026-09 起，舊制部門 key 機制已拔除）：這裡只剩
key_required 一個布林——要不要填 key。要給特定部門專屬 key 的做法是「同一個
上游再上架一個模型」：名稱加後綴（例如 gpt-4o-deptA）、填該部門的 key，開給誰
仍然在模型授權頁決定，不限一個部門。
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
    name_prefix: str  # model_name 的建議前綴


UPSTREAMS: dict[str, Upstream] = {
    "openrouter": Upstream(
        key="openrouter", label="OpenRouter",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default="https://openrouter.ai/api/v1", api_base_hint="",
        key_required=True, name_prefix="",
    ),
    "openai": Upstream(
        key="openai", label="OpenAI 官方",
        model_template="openai/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, name_prefix="",
    ),
    "anthropic": Upstream(
        key="anthropic", label="Anthropic 官方",
        model_template="anthropic/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, name_prefix="",
    ),
    "gemini": Upstream(
        key="gemini", label="Gemini 官方",
        model_template="gemini/{slug}",
        api_base_mode="auto", api_base_default=None, api_base_hint="",
        key_required=True, name_prefix="",
    ),
    "vllm": Upstream(
        key="vllm", label="地端 vLLM",
        model_template="hosted_vllm/{slug}",
        api_base_mode="required", api_base_default=None,
        api_base_hint="Service DNS，要帶 /v1，例如 http://my-vllm-service:8000/v1",
        key_required=False, name_prefix="",
    ),
    "ollama": Upstream(
        key="ollama", label="地端 Ollama",
        model_template="ollama/{slug}",
        api_base_mode="required", api_base_default="http://ollama-service:11434",
        api_base_hint="Service DNS，不帶 /v1",
        key_required=False, name_prefix="ollama/",
    ),
    "other": Upstream(
        key="other", label="其他 OpenAI 相容",
        model_template="openai/{slug}",
        api_base_mode="required", api_base_default=None, api_base_hint="",
        key_required=True, name_prefix="",
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
    """R-17：系統建議、允許覆寫。"""
    return f"{upstream.name_prefix}{slug}"


def looks_like_ip(value: str) -> bool:
    """R-24 的非阻斷提醒：api_base 填了節點 IP 而不是 Service DNS。"""
    host = value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)
