import json
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import psycopg2
import psycopg2.pool
from litellm.integrations.custom_logger import CustomLogger

_WRITE_LOCK = Lock()
_SPEND_LOCK = Lock()

# 2026-09 從 users-db-pvc(SQLite) 遷到 Postgres。刻意不 `import custom_auth` 共用
# 它的連線池——litellm 用 importlib.util.spec_from_file_location 直接從檔案路徑
# 載入這兩個 ConfigMap 掛載的檔案（見 litellm/proxy/types_utils/utils.py 的
# get_instance_fn），不會把 /app/config 加進 sys.path，跨檔案 import 在真正的
# litellm 執行環境裡會是 ModuleNotFoundError（本機手動測試時自己塞了 sys.path 才
# 沒踩到，部署到叢集才炸出來）。這裡自己獨立管一份連線池。
DEFAULT_DATABASE_URL = "postgresql://litellm:litellm@localhost:5432/firdi_users"
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.getenv("USER_AUTH_DATABASE_URL", DEFAULT_DATABASE_URL)
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=dsn)
    return _pool


def write_log(record: dict) -> None:
    path = os.getenv("LOG_PATH", "/app/logs/usage.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usage_field(response_obj, name: str):
    """從回應的 usage 取一個欄位。LiteLLM 的 usage 有時是物件、有時是 dict。"""
    usage = getattr(response_obj, "usage", None)
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    return value


def _extract_cost(kwargs: dict, response_obj) -> float | None:
    """這次呼叫的花費（USD）。拿不到就回 None——回 0 會讓「算不出成本」看起來像
    「這次免費」，額度就會永遠用不完卻沒有人發現。回 None 時上層會把它記進
    usage.jsonl 的 response_cost 欄位（null），至少查得出來。

    來源的優先序很重要：

    1. **上游在 usage.cost 回報的實際金額**（OpenRouter 會給，那是真正的帳單
       金額）。這一項必須排在 LiteLLM 自己算的前面——LiteLLM 的 response_cost
       是用它內建的定價表算的，查不到的模型一律回 **0.0（不是 None）**，而透過
       OpenRouter 上架的模型 litellm_params.model 是 openai/<slug>，定價表裡
       通常沒有。於是每一筆都記成 0、額度永遠不會觸發，而且完全沒有錯誤訊息。
       2026-08-28 在 ai-x-dev 驗收時實際踩到：同一次呼叫 usage.cost 是
       0.000108375，response_cost 卻是 0.0。
    2. kwargs["response_cost"] / standard_logging_object：LiteLLM 認得的模型
       （原生 provider 路線）才會有值。
    """
    upstream = _as_float(_usage_field(response_obj, "cost"))
    if upstream:   # 0 或 None 都往下找——上游沒給價，才輪到 LiteLLM 自己算的
        return upstream

    cost = kwargs.get("response_cost")
    if cost is None:
        cost = (kwargs.get("standard_logging_object") or {}).get("response_cost")
    return _as_float(cost)


def record_spend(model_name: str, cost: float, dept_id: str | None = None) -> None:
    """把花費累加進 model_spend（同時記當月與累計兩筆）。

    這張表是本專案自己的用量累計，不是 LiteLLM 內建的 spend tracking——後者在
    config/litellm_config.yaml 被刻意關掉了（disable_spend_logs／
    disable_spend_updates），所以 LiteLLM 原生的 budget 機制在這裡沒有資料可用。
    admin-api 的 model_metadata 額度上限要「真的擋得下來」，就得靠這裡累加、靠
    config/custom_auth.py 讀出來比對。

    刻意不 bump db_version：每筆請求都 bump 會讓 custom_auth 的設定快取一直失效，
    整個快取就白做了。代價是額度用完後最多 30 秒（_CACHE_TTL）才開始擋。

    dept_id 是 2026-09 加的維度（取代手填的 model_metadata.cost_center 標籤，
    改成用真實用量算出「這個模型的花費裡，各部門各花了多少」）。沒有部門脈絡的
    呼叫（例如地端模型的健康檢查、curl 測試）記到 dept_id=''（未分類），不記
    NULL——PK 需要一個穩定可比較的值，NULL 在 SQL 的 UNIQUE/ON CONFLICT 比對裡
    不會視為相等，用 '' 才能正確累加到同一列。額度判斷（_check_model_budget）
    仍然是整個模型的總量，讀取端會跨 dept_id 加總，這裡不用改判斷邏輯。
    """
    if not model_name:
        return
    dept_id = dept_id or ""
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    pool = _get_pool()
    # 注意：SET 右側的 spend_usd/calls 一定要加上表名前綴——不加的話 Postgres 會報
    # AmbiguousColumn（SQLite 不加也能跑，這是原本記憶裡誤判成「已經相容」的地方,
    # 實測才發現要修正）。
    sql = (
        "INSERT INTO model_spend (model_name, dept_id, period, spend_usd, calls) "
        "VALUES (%s, %s, %s, %s, 1) "
        "ON CONFLICT(model_name, dept_id, period) DO UPDATE SET "
        "spend_usd = model_spend.spend_usd + excluded.spend_usd, "
        "calls = model_spend.calls + 1, updated_at = now()::text"
    )
    with _SPEND_LOCK:
        conn = pool.getconn()
        conn.autocommit = True
        broken = False
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, (model_name, dept_id, period, cost))
                cur.execute(sql, (model_name, dept_id, "total", cost))
            except Exception:
                # 表還不存在（admin-api 還沒跑過 init_db）或短暫的連線問題——用量
                # 累計不該影響這次呼叫本身，靜默跳過，jsonl 那份記錄仍然完整。
                broken = True
        finally:
            pool.putconn(conn, close=broken)


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
            cost = _extract_cost(kwargs, response_obj)

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
                record_spend(billing_model, cost, dept_id)
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
