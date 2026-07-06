import json
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_WRITE_LOCK = Lock()


def write_log(record: dict) -> None:
    path = os.getenv("LOG_PATH", "/app/logs/usage.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class FirdiLogger(CustomLogger):

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict:
        user_meta = getattr(user_api_key_dict, "metadata", {}) or {}
        dept_id = user_meta.get("dept_id")
        if dept_id:
            data.setdefault("metadata", {})["dept_id"] = dept_id

        model: str = data.get("model", "")
        if model.startswith("openrouter/"):
            dept_key = user_meta.get("dept_openrouter_api_key", "")
            if dept_key and not dept_key.startswith("sk-or-CHANGE"):
                data["api_key"] = dept_key
        return data

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
            meta = kwargs.get("litellm_params", {}).get("metadata", {}) or {}
            user_id = meta.get("user_api_key_user_id")
            key_name = meta.get("user_api_key_alias")
            dept_id = meta.get("dept_id")

            usage = getattr(response_obj, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            total_tokens = getattr(usage, "total_tokens", 0) if usage else 0

            latency_ms = int((end_time - start_time).total_seconds() * 1000) if start_time and end_time else None

            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "llm_call",
                "status": "success",
                "user_id": user_id,
                "key_name": key_name,
                "dept_id": dept_id,
                "model": kwargs.get("model"),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "latency_ms": latency_ms,
            })
        except Exception:
            pass

    async def async_log_failure_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
            meta = kwargs.get("litellm_params", {}).get("metadata", {}) or {}
            user_id = meta.get("user_api_key_user_id")
            key_name = meta.get("user_api_key_alias")
            dept_id = meta.get("dept_id")

            latency_ms = int((end_time - start_time).total_seconds() * 1000) if start_time and end_time else None

            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "llm_call",
                "status": "failure",
                "user_id": user_id,
                "key_name": key_name,
                "dept_id": dept_id,
                "model": kwargs.get("model"),
                "error": str(response_obj) if response_obj else None,
                "latency_ms": latency_ms,
            })
        except Exception:
            pass


proxy_handler_config = FirdiLogger()
