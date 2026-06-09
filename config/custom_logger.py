import json
import os
from datetime import datetime, timezone
from threading import Lock

from litellm.integrations.custom_logger import CustomLogger

_WRITE_LOCK = Lock()


def _log_path() -> str:
    return os.getenv("LOG_PATH", "/app/logs/usage.jsonl")


def write_log(record: dict) -> None:
    path = _log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class UsageLogger(CustomLogger):

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        metadata = kwargs.get("litellm_params", {}).get("metadata", {})
        usage = getattr(response_obj, "usage", None)
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "llm_call",
            "status": "success",
            "user_id": metadata.get("user_api_key_user_id"),
            "key_name": metadata.get("user_api_key_alias"),
            "model": kwargs.get("model"),
            "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
            "latency_ms": int((end_time - start_time).total_seconds() * 1000),
        })

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        metadata = kwargs.get("litellm_params", {}).get("metadata", {})
        write_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "llm_call",
            "status": "failure",
            "user_id": metadata.get("user_api_key_user_id"),
            "key_name": metadata.get("user_api_key_alias"),
            "model": kwargs.get("model"),
            "error": str(kwargs.get("exception", "")),
            "latency_ms": int((end_time - start_time).total_seconds() * 1000),
        })


proxy_handler_config = UsageLogger()
