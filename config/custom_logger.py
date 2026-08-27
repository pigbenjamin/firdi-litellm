import json
import os
import sqlite3
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_WRITE_LOCK = Lock()
_SPEND_LOCK = Lock()

DEFAULT_DB_PATH = "/app/data/users.db"


def write_log(record: dict) -> None:
    path = os.getenv("LOG_PATH", "/app/logs/usage.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _extract_cost(kwargs: dict) -> float | None:
    """這次呼叫的花費（USD）。拿不到就回 None——回 0 會讓「算不出成本」看起來像
    「這次免費」，額度就會永遠用不完卻沒有人發現。回 None 時上層會把它記進
    usage.jsonl 的 response_cost 欄位（null），至少查得出來。
    """
    cost = kwargs.get("response_cost")
    if cost is None:
        cost = (kwargs.get("standard_logging_object") or {}).get("response_cost")
    try:
        return float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return None


def record_spend(model_name: str, cost: float) -> None:
    """把花費累加進 model_spend（同時記當月與累計兩筆）。

    這張表是本專案自己的用量累計，不是 LiteLLM 內建的 spend tracking——後者在
    config/litellm_config.yaml 被刻意關掉了（disable_spend_logs／
    disable_spend_updates），所以 LiteLLM 原生的 budget 機制在這裡沒有資料可用。
    admin-api 的 model_metadata 額度上限要「真的擋得下來」，就得靠這裡累加、靠
    config/custom_auth.py 讀出來比對。

    刻意不 bump db_version：每筆請求都 bump 會讓 custom_auth 的設定快取一直失效，
    整個快取就白做了。代價是額度用完後最多 30 秒（_CACHE_TTL）才開始擋。
    """
    if not model_name:
        return
    db_path = os.getenv("USER_AUTH_DB_PATH", DEFAULT_DB_PATH)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    sql = (
        "INSERT INTO model_spend (model_name, period, spend_usd, calls) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(model_name, period) DO UPDATE SET "
        "spend_usd = spend_usd + excluded.spend_usd, calls = calls + 1, updated_at = datetime('now')"
    )
    with _SPEND_LOCK:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(sql, (model_name, period, cost))
            conn.execute(sql, (model_name, "total", cost))
            conn.commit()
        except sqlite3.OperationalError:
            # 表還不存在（admin-api 還沒跑過 init_db）或短暫鎖住——用量累計不該
            # 影響這次呼叫本身，靜默跳過，jsonl 那份記錄仍然完整。
            conn.rollback()
        finally:
            conn.close()


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

        # WP1 額度累計：把「呼叫者請求的那個 model_name」帶進這次請求的 metadata，
        # 讓 async_log_success_event 把花費記到正確的模型上。kwargs["model"] 在
        # logger 端可能已經是上游的 litellm_params.model（如 openai/gpt-4o-mini），
        # 跟 model_metadata / allowed_models 認的公開名稱不是同一個字串。
        billing_model = user_meta.get("requested_model", "")
        if billing_model:
            data.setdefault("metadata", {})["billing_model"] = billing_model

        # 決策 E（見 docs/admin-web-plan.md）：要不要注入、注入哪個部門的哪把 key，
        # 判斷已經在 config/custom_auth.py 做完並放進 metadata——這裡不再看 model
        # 字串前綴，metadata 有值就套用，沒有就維持模型自己定義的 key。
        injected_key = user_meta.get("injected_api_key", "")
        if injected_key:
            data["api_key"] = injected_key
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

            # 額度累計要記在「呼叫者請求的公開 model_name」上，跟 model_metadata
            # 的主鍵一致；退回 model_group、再退回 kwargs["model"]（上游名稱）。
            billing_model = meta.get("billing_model") or meta.get("model_group") or kwargs.get("model")
            cost = _extract_cost(kwargs)

            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "llm_call",
                "status": "success",
                "user_id": user_id,
                "key_name": key_name,
                "dept_id": dept_id,
                "model": kwargs.get("model"),
                "billing_model": billing_model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "response_cost": cost,   # null = LiteLLM 算不出這個模型的成本（地端模型沒有定價）
                "latency_ms": latency_ms,
            })

            if cost is not None:
                record_spend(billing_model, cost)
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
