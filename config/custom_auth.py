import json
import os
from datetime import datetime, timezone
from threading import Lock

from fastapi import Request
from litellm.proxy._types import ProxyException, UserAPIKeyAuth

_WRITE_LOCK = Lock()


def write_log(record: dict) -> None:
    path = os.getenv("LOG_PATH", "/app/logs/usage.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


DEFAULT_CONFIG_PATH = "/app/config/users.json"
_CONFIG_LOCK = Lock()
_CONFIG_CACHE: dict | None = None
_CONFIG_CACHE_KEY: tuple[str, float] | None = None


def _normalize_api_key(api_key: str) -> str:
    if api_key.startswith("Bearer "):
        return api_key.removeprefix("Bearer ").strip()
    return api_key.strip()


def _load_user_config() -> dict:
    global _CONFIG_CACHE, _CONFIG_CACHE_KEY

    config_path = os.getenv("USER_AUTH_CONFIG_PATH", DEFAULT_CONFIG_PATH)

    with _CONFIG_LOCK:
        config_mtime = os.path.getmtime(config_path)
        cache_key = (config_path, config_mtime)
        if _CONFIG_CACHE is not None and _CONFIG_CACHE_KEY == cache_key:
            return _CONFIG_CACHE

        with open(config_path, "r", encoding="utf-8") as config_file:
            _CONFIG_CACHE = json.load(config_file)
            _CONFIG_CACHE_KEY = cache_key
            return _CONFIG_CACHE


def _find_user(api_key: str) -> dict | None:
    normalized_key = _normalize_api_key(api_key)
    for user in _load_user_config().get("users", []):
        if user.get("api_key") == normalized_key and not user.get("blocked", False):
            return user
    return None


async def _get_requested_model(request: Request) -> str | None:
    try:
        body_bytes = await request.body()
        if not body_bytes:
            return None
        return json.loads(body_bytes).get("model")
    except Exception:
        return None


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    user = _find_user(api_key)
    if user is None:
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "auth_denied",
            "reason": "invalid_key",
        })
        raise ProxyException(
            message="Invalid API key",
            type="authentication_error",
            param="api_key",
            code=401,
        )

    allowed_models = user.get("models", [])
    if allowed_models:
        requested_model = await _get_requested_model(request)
        if requested_model and requested_model not in allowed_models:
            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "auth_denied",
                "reason": "model_not_allowed",
                "user_id": user["user_id"],
                "key_name": user.get("key_name"),
                "model": requested_model,
            })
            raise ProxyException(
                message=f"Model '{requested_model}' is not allowed for this key",
                type="auth_error",
                param="model",
                code=403,
            )

    metadata = dict(user.get("metadata", {}))
    if user.get("team_id"):
        metadata["team_id"] = user["team_id"]
    if user.get("team_alias"):
        metadata["team_alias"] = user["team_alias"]

    return UserAPIKeyAuth(
        api_key=_normalize_api_key(api_key),
        key_name=user.get("key_name"),
        user_id=user["user_id"],
        user_email=user.get("user_email"),
        models=user.get("models", []),
        aliases=user.get("aliases", {}),
        rpm_limit=user.get("rpm_limit"),
        tpm_limit=user.get("tpm_limit"),
        max_budget=user.get("max_budget"),
        budget_duration=user.get("budget_duration"),
        allowed_routes=user.get("allowed_routes"),
        metadata=metadata,
    )
