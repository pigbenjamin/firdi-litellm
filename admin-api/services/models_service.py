"""LiteLLM 模型管理的業務邏輯，不帶認證。

刻意不吃任何 auth 相關參數/依賴——`routers/models.py`（ADMIN_API_KEY）與未來
`admin-web` 的模型管理頁（Keycloak session）各自認證後呼叫這裡的函式，避免網頁層
繞去用 ADMIN_API_KEY 打自己的 API（見 docs/admin-web-plan.md 決策 C）。

# ── 外部模型自助上架（見 docs/external-models-ops.md「路線 C」）────────────────
# 這一組函式是 LiteLLM `/model/new` `/model/info` `/model/delete` 的薄代理：
# admin-api 拿 LITELLM_MASTER_KEY 去呼叫 LiteLLM，讓呼叫者不需要拿到 LiteLLM
# master key、也不需要任何 kubectl 存取，就能新增/查詢/刪除動態註冊到 Postgres
# 的模型（store_model_in_db，不影響 config/litellm_config.yaml 那份 YAML
# model_list，也不會重啟 litellm pod）。
"""
import json
import os
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from database import DB_PATH, get_conn
from models import ExternalModelIn
from services import model_metadata_service

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")


def _litellm_client(timeout: float = 15) -> httpx.AsyncClient:
    """所有打 LiteLLM 的請求都從這裡拿 client——集中一處，離線測試才有辦法用
    httpx.MockTransport 換掉整條路徑（見 scripts/test_model_lifecycle.py）。
    """
    if not LITELLM_MASTER_KEY:
        raise HTTPException(status_code=500, detail="LITELLM_MASTER_KEY not configured")
    return httpx.AsyncClient(
        base_url=LITELLM_URL,
        headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
        timeout=timeout,
    )


# ── 決策 E：key 來源政策（見 docs/admin-web-plan.md）───────────────────────────
# 上游（litellm_params.model）跟 key 從哪來解耦：政策存 admin-api 自己的 SQLite
# （model_key_policies 表），不存 LiteLLM 的 model_info——後者會讓
# config/custom_auth.py 在每個請求的熱路徑上多一個 Postgres 相依，不值得。


def _infer_default_key_policy(model_name: str) -> str:
    """沒有明確政策時的預設值，跟決策 E 之前的唯一行為（只有 openrouter/ 前綴
    會觸發部門 key 注入）完全一致，既有模型不需要任何資料回填。
    """
    return "dept:openrouter" if model_name.startswith("openrouter/") else "model"


def _validate_key_policy(policy: str) -> None:
    if policy == "model":
        return
    if policy.startswith("dept:") and len(policy) > len("dept:"):
        return
    raise HTTPException(
        status_code=422,
        detail=f"key_policy 格式錯誤：'{policy}'，需為 'model' 或 'dept:<provider>'（如 'dept:openai'）",
    )


def _set_key_policy(model_name: str, policy: str) -> None:
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO model_key_policies (model_name, key_policy) VALUES (?, ?) "
            "ON CONFLICT(model_name) DO UPDATE SET key_policy=excluded.key_policy",
            (model_name, policy),
        )


async def _cleanup_orphaned_key_policy(model_name: str) -> None:
    """model_name 底下已無其他 deployment 時才刪政策紀錄，避免同名模型日後重建被
    舊政策悄悄套用；查不到（LiteLLM 連不上）就放著，不影響刪除本身——下次用同名
    重新上架時 _set_key_policy 的 upsert 一樣會覆蓋掉舊值。
    """
    async with _litellm_client() as client:
        try:
            resp = await client.get("/model/info")
        except httpx.RequestError:
            return
    if resp.status_code == 200:
        if any(item.get("model_name") == model_name for item in resp.json().get("data", [])):
            return  # 還有其他 deployment 用同一個 model_name，政策仍在使用中
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM model_key_policies WHERE model_name=?", (model_name,))


async def list_models() -> dict:
    async with _litellm_client() as client:
        try:
            resp = await client.get("/models")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="LiteLLM returned non-200 response")

    data = resp.json()
    model_ids = sorted(item["id"] for item in data.get("data", []))
    return {"models": model_ids}


async def _fetch_model_info(client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get("/model/info")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/info failed: {resp.text}")
    return resp.json().get("data", [])


async def _find_deployment(client: httpx.AsyncClient, model_name: str) -> dict | None:
    """model_name → LiteLLM 的 deployment 記錄（含 model_info.id）。找不到回 None。

    停用中的模型在 LiteLLM 裡不存在（停用＝真的 /model/delete），所以這裡回 None
    是正常狀態，不是錯誤——狀態機的權威來源是 model_metadata.status。
    """
    for item in await _fetch_model_info(client):
        if item.get("model_name") == model_name:
            return item
    return None


async def list_external_models(include_yaml: bool = False) -> dict:
    """DB-managed 模型 ∪ 停用中的模型（∪ YAML model_list 定義的，看 include_yaml）。

    停用中的模型在 LiteLLM 裡已經不存在了，只剩 model_metadata 那筆紀錄——要是
    只列 LiteLLM 回的清單，停用的模型就會從管理介面上整個消失、再也點不到
    「重新啟用」。所以兩邊聯集，並用 status 欄位區分。

    include_yaml：
      False（預設）＝ curl 端點 `GET /api/v1/models/external` 的既有契約，文件
        明講「不含 YAML model_list 定義的那些」，不能改。
      True ＝ admin-web 的模型清單／詳情頁。管理者需要在同一個地方看到平台上
        「所有」的模型——授權矩陣本來就含地端模型（它走 LiteLLM /models），
        模型清單卻不含，同一個介面對「有哪些模型」給出兩種答案，很容易誤判。
        這些模型標成 yaml_managed，路由設定一律唯讀（要改得動 YAML 並重啟 pod），
        但顯示名稱／類型／成本歸屬／備註仍可設定。
    """
    async with _litellm_client() as client:
        info = await _fetch_model_info(client)

    def _is_db(item: dict) -> bool:
        return bool((item.get("model_info") or {}).get("db_model"))

    db_items = [item for item in info if _is_db(item)]
    if include_yaml:
        db_items += [item for item in info if not _is_db(item)]
    # LiteLLM 裡所有的名字，含這次沒列出來的。下面「只存在於 metadata」那一段要
    # 靠它排除掉「其實在 LiteLLM 裡、只是被 include_yaml 過濾掉」的模型，否則
    # 一個被納管過的 YAML 模型會在 curl 端點被誤判成「停用中」。
    in_litellm = {item.get("model_name") for item in info}

    with get_conn(DB_PATH) as conn:
        stored_policies = {
            row["model_name"]: row["key_policy"]
            for row in conn.execute("SELECT model_name, key_policy FROM model_key_policies").fetchall()
        }
    metadata = model_metadata_service.list_metadata()
    spend_by_model = model_metadata_service.list_spend()
    period = model_metadata_service.current_period()

    def _spend(name: str) -> dict:
        return spend_by_model.get(name) or {"period": period, "monthly": 0.0, "calls": 0, "total": 0.0}

    out = []
    seen = set()
    for item in db_items:
        model_info = item.get("model_info") or {}
        litellm_params = item.get("litellm_params") or {}
        model_name = item.get("model_name") or ""
        seen.add(model_name)
        meta = metadata.get(model_name) or model_metadata_service.synthesized(model_name)
        # YAML 定義的模型不受狀態機管理（沒有草稿／發布／停用可言，改它要動
        # config/litellm_config.yaml 並重啟 litellm pod）。這個旗標讓 UI 知道
        # 要標成「既有」並收起那些操作——就算它已經被納管（有 model_metadata
        # 紀錄）也一樣，不能因為有紀錄就顯示成「已發布」。
        meta["yaml_managed"] = not bool(model_info.get("db_model"))
        out.append(
            {
                "id": model_info.get("id"),
                "model_name": model_name,
                "model": litellm_params.get("model"),
                "api_base": litellm_params.get("api_base"),
                "key_policy": stored_policies.get(model_name) or _infer_default_key_policy(model_name),
                "registered": True,
                "meta": meta,
                "spend": _spend(model_name),
            }
        )

    # 只存在於 model_metadata 的（停用中，或 LiteLLM 那邊被人繞過 UI 刪掉了）
    for model_name, meta in metadata.items():
        if model_name in seen or model_name in in_litellm:
            continue
        out.append(
            {
                "id": None,
                "model_name": model_name,
                "model": meta["litellm_model"],
                "api_base": meta["api_base"],
                "key_policy": stored_policies.get(model_name) or _infer_default_key_policy(model_name),
                "registered": False,
                "meta": meta,
                "spend": _spend(model_name),
            }
        )

    out.sort(key=lambda m: m["model_name"])
    return {"models": out}


# ── 註冊到 LiteLLM（上架與「重新啟用」共用）──────────────────────────────────

def _build_litellm_params(
    model: str, api_key: str | None, api_base: str | None, key_policy: str, model_name: str
) -> dict:
    litellm_params: dict = {"model": model}
    if api_key:
        litellm_params["api_key"] = api_key
    elif key_policy.startswith("dept:"):
        # 跟 YAML model_list 現有 openrouter 那幾筆一樣用共用 placeholder；這個值
        # 只在 custom_auth 找不到對應部門 key 時才會真的被拿去打上游（會 401，
        # 這是刻意的失敗模式，見 docs/admin-web-plan.md「容易做錯的五件事」#5）。
        # 沿用既有 OPENROUTER_API_KEY_PLACEHOLDER 這個環境變數名稱，即使現在
        # 也給非 openrouter 的 dept:* 政策用——重新命名要動 k8s deployment
        # manifest，留給部署階段一併處理，不在這裡做。
        litellm_params["api_key"] = "os.environ/OPENROUTER_API_KEY_PLACEHOLDER"
    else:
        raise HTTPException(
            status_code=422,
            detail="api_key 必填（key_policy='model' 時一定要提供；"
            "key_policy='dept:<provider>' 可留空，由該部門的 provider_keys 動態注入）",
        )
    if api_base:
        litellm_params["api_base"] = api_base
    elif model_name.startswith("openrouter/"):
        litellm_params["api_base"] = "https://openrouter.ai/api/v1"
    return litellm_params


async def _assert_name_available(client: httpx.AsyncClient, model_name: str) -> None:
    existing = await _find_deployment(client, model_name)
    if existing is None:
        return
    is_db_managed = bool((existing.get("model_info") or {}).get("db_model"))
    origin = (
        "DB-managed（可從 UI 刪除後重建）" if is_db_managed
        else "YAML model_list 定義（不能從 UI 刪除，需改 config/litellm_config.yaml 並重啟 litellm pod）"
    )
    raise HTTPException(
        status_code=409,
        detail=f"model_name '{model_name}' 已存在，是 {origin}。換一個名字，"
        "或先用 DELETE /api/v1/models/external/{id} 刪除舊的（僅限 DB-managed）",
    )


async def _post_model_new(client: httpx.AsyncClient, model_name: str, litellm_params: dict) -> None:
    try:
        resp = await client.post(
            "/model/new", json={"model_name": model_name, "litellm_params": litellm_params}
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/new failed: {resp.text}")


async def create_external_model(body: ExternalModelIn) -> dict:
    """上架後這個模型還沒有任何人能打——跟 YAML model_list 定義的模型一樣，權限
    (`allowed_models`) 是分開的一件事，一定要另外到 OpenWebUI 開通（見
    docs/external-models.md）。

    body.status='draft'（admin-web 表單走的路）時還多一層：草稿狀態的模型
    config/custom_auth.py 會直接擋掉一般使用者，要先通過測試呼叫才發布得出去。
    curl 路徑的預設值仍是 published，既有行為完全不變。
    """
    # R-20：model / model_name 不可為空白字串——目前 pydantic 只驗證是字串，空字串
    # 會通過驗證再到 LiteLLM 失敗，變成難懂的 502，這個檢查補在這裡而不是表單，
    # 讓 curl 呼叫者也受益。
    if not body.model_name.strip():
        raise HTTPException(status_code=422, detail="model_name 不可為空白字串")
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model 不可為空白字串")

    key_policy = body.key_policy or _infer_default_key_policy(body.model_name)
    _validate_key_policy(key_policy)
    model_metadata_service.validate_model_type(body.model_type)
    model_metadata_service.validate_budget(body.budget_limit_usd, body.budget_period, body.budget_enforce)

    async with _litellm_client() as client:
        await _assert_name_available(client, body.model_name)
        litellm_params = _build_litellm_params(
            body.model, body.api_key, body.api_base, key_policy, body.model_name
        )
        await _post_model_new(client, body.model_name, litellm_params)

    # 成功建立才落政策與 metadata，避免上架失敗卻留下孤兒紀錄。
    _set_key_policy(body.model_name, key_policy)
    model_metadata_service.upsert_metadata(
        body.model_name,
        display_name=body.display_name,
        model_type=body.model_type,
        cost_center=body.cost_center,
        budget_limit_usd=body.budget_limit_usd,
        budget_enforce=int(body.budget_enforce),
        budget_period=body.budget_period,
        notes=body.notes,
        status=body.status,
        upstream=body.upstream,
        litellm_model=body.model,
        api_base=body.api_base,
        api_key=body.api_key or "",
    )

    return {"model_name": body.model_name, "status": "created", "key_policy": key_policy,
            "lifecycle_status": body.status}


async def delete_external_model(model_id: str) -> None:
    """model_id 是 GET /api/v1/models/external 回傳的 id，不是 model_name（同一個
    model_name 理論上可以有多筆 deployment 做負載平衡）。
    """
    async with _litellm_client() as client:
        model_name = None
        for item in await _fetch_model_info(client):
            if (item.get("model_info") or {}).get("id") == model_id:
                model_name = item.get("model_name")
                break

        try:
            resp = await client.post("/model/delete", json={"id": model_id})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")

    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"LiteLLM /model/delete failed: {resp.text}")

    if model_name:
        await _cleanup_orphaned_key_policy(model_name)
        model_metadata_service.delete_metadata(model_name)


# ── WP2：生命週期（草稿 → 發布 → 停用 → 重新啟用 → 硬刪除）────────────────────
#
# LiteLLM 沒有 /model/update 這支端點，只有 /model/new 跟 /model/delete。所以
# 「編輯」在實作上一律是刪除重建，也因此只開放給 draft 狀態——還沒發布的模型
# 不會有人正在用，重建期間打不通沒有影響；已發布的模型改路由就是換上游，
# 一律要求走「停用 → 重建」的明確流程。

async def _delete_by_name(client: httpx.AsyncClient, model_name: str) -> None:
    """把 model_name 底下所有 deployment 都刪掉（同名可能有多筆做負載平衡）。"""
    ids = [
        (item.get("model_info") or {}).get("id")
        for item in await _fetch_model_info(client)
        if item.get("model_name") == model_name
    ]
    for model_id in [i for i in ids if i]:
        try:
            resp = await client.post("/model/delete", json={"id": model_id})
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach LiteLLM: {exc}")
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"LiteLLM /model/delete failed: {resp.text}")


def _require_meta(model_name: str) -> dict:
    meta = model_metadata_service.get_metadata(model_name)
    if not meta["has_record"]:
        raise HTTPException(
            status_code=404,
            detail=f"'{model_name}' 沒有管理紀錄——它是這個功能上線前就存在的模型"
            "（YAML model_list 定義的地端模型，或舊版 curl 上架的），不受狀態機管理。",
        )
    return meta


async def update_draft_model(
    model_name: str, *, model: str, api_base: str | None, api_key: str | None,
    key_policy: str, display_name: str, model_type: str, cost_center: str,
    budget_limit_usd: float | None, budget_enforce: bool, budget_period: str,
    notes: str, upstream: str,
) -> dict:
    """改草稿模型的完整設定。LiteLLM 沒有 update，實作是刪掉再用新值建一次。

    路由欄位一改，之前那次測試就不算數了——last_test_ok 一併清掉，逼使用者重測
    才發布得出去（WP3 的閘門）。
    """
    meta = _require_meta(model_name)
    if meta["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"只有草稿狀態才能編輯，'{model_name}' 目前是 {meta['status']}。"
            "已發布的模型要改設定請先停用。",
        )
    _validate_key_policy(key_policy)
    model_metadata_service.validate_model_type(model_type)
    model_metadata_service.validate_budget(budget_limit_usd, budget_period, budget_enforce)
    if not model.strip():
        raise HTTPException(status_code=422, detail="model 不可為空白字串")

    litellm_params = _build_litellm_params(model, api_key, api_base, key_policy, model_name)
    async with _litellm_client() as client:
        await _delete_by_name(client, model_name)
        await _post_model_new(client, model_name, litellm_params)

    _set_key_policy(model_name, key_policy)
    return model_metadata_service.upsert_metadata(
        model_name,
        display_name=display_name, model_type=model_type, cost_center=cost_center,
        budget_limit_usd=budget_limit_usd, budget_enforce=int(budget_enforce),
        budget_period=budget_period, notes=notes, upstream=upstream,
        litellm_model=model, api_base=api_base, api_key=api_key or "",
        last_test_ok=None, last_test_at=None, last_test_result="",
    )


async def update_descriptive_fields(
    model_name: str, *, display_name: str, cost_center: str, budget_limit_usd: float | None,
    budget_enforce: bool, budget_period: str, notes: str, model_type: str | None = None,
) -> dict:
    """已發布的模型仍可改的欄位：顯示名稱、成本歸屬、額度、備註。

    刻意不含 upstream/litellm_model/api_base/api_key/model_type——那些一改，
    使用者當下打到的就是另一個東西了（見 model_metadata_service.ROUTING_FIELDS）。

    這也是「既有模型納管」的入口：這個功能上線前就存在的模型（YAML model_list 定義
    的地端模型、舊版 curl 上架的外部模型）沒有 model_metadata 紀錄，在這裡存一次就
    建起來，狀態預設 published（見 model_metadata_service.DEFAULTS），行為不變但
    從此可以設額度、寫備註。因為會建新紀錄，所以要先確認 model_name 真的存在於
    LiteLLM——否則打錯字就默默留下一筆對不到任何模型的孤兒設定。
    """
    if not model_metadata_service.get_metadata(model_name)["has_record"]:
        known = (await list_models())["models"]
        if model_name not in known:
            raise HTTPException(
                status_code=404,
                detail=f"LiteLLM 裡找不到模型 '{model_name}'，無法建立管理紀錄（請檢查名稱是否正確）",
            )
    model_metadata_service.validate_budget(budget_limit_usd, budget_period, budget_enforce)
    extra = {}
    if model_type is not None:
        # 只有 YAML 定義的模型會走這條路：它們沒有「草稿」狀態可以編輯上游設定，
        # 但類型必須設得對（embeddinggemma 用 chat 的形狀去測只會拿到 400）。
        # DB-managed 模型的類型仍然歸在「上游設定」那組、只有草稿能改。
        model_metadata_service.validate_model_type(model_type)
        extra["model_type"] = model_type
    return model_metadata_service.upsert_metadata(
        model_name,
        display_name=display_name, cost_center=cost_center, budget_limit_usd=budget_limit_usd,
        budget_enforce=int(budget_enforce), budget_period=budget_period, notes=notes, **extra,
    )


async def publish_model(model_name: str) -> dict:
    """草稿 → 已發布。WP3 的閘門：沒通過測試呼叫的模型不准發布。"""
    meta = _require_meta(model_name)
    if meta["status"] == "published":
        raise HTTPException(status_code=409, detail=f"'{model_name}' 已經是發布狀態")
    if meta["status"] != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"只有草稿能發布，'{model_name}' 目前是 {meta['status']}（停用中的模型請用「重新啟用」）",
        )
    if meta["last_test_ok"] != 1:
        raise HTTPException(
            status_code=409,
            detail="還沒通過測試呼叫，不能發布——沒測過的模型不該放給使用者。請先在模型頁按「測試呼叫」。",
        )
    async with _litellm_client() as client:
        if await _find_deployment(client, model_name) is None:
            raise HTTPException(
                status_code=409,
                detail=f"'{model_name}' 在 LiteLLM 裡找不到（可能被繞過 UI 刪掉了），無法發布",
            )
    return model_metadata_service.upsert_metadata(model_name, status="published")


async def disable_model(model_name: str) -> dict:
    """停用：從 LiteLLM 刪掉（使用者立刻打不到、OpenWebUI 的模型清單也看不到），
    但 model_metadata 那筆設定與 model_key_policies 的政策完整保留，可一鍵重建。

    刻意不動 departments.allowed_models / users.models——授權是獨立的一件事，
    重新啟用時模型回到 LiteLLM 清單，原本的授權自然又生效，不必重放一次。

    要留意的副作用：停用期間 CronJob 的 pull 會把 dept.allowed_models 裡這個
    model_name 拿掉（pull 以 LiteLLM /models 清單過濾，停用的模型不在裡面）。
    OpenWebUI 的 access_grants 不受影響（push 同樣會跳過不在 LiteLLM 清單的模型），
    所以重新啟用之後下一次 pull 就會從 OpenWebUI 把授權補回 DB。
    """
    meta = _require_meta(model_name)
    if meta["status"] == "disabled":
        raise HTTPException(status_code=409, detail=f"'{model_name}' 已經是停用狀態")
    async with _litellm_client() as client:
        await _delete_by_name(client, model_name)
    return model_metadata_service.upsert_metadata(model_name, status="disabled")


async def enable_model(model_name: str) -> dict:
    """重新啟用：用保留下來的 upstream/litellm_model/api_base/api_key 重新註冊。

    回到哪個狀態依測試結果決定——通過測試過的回 published，沒通過的回 draft，
    才不會讓「停用一個草稿再啟用」變成繞過 WP3 發布閘門的後門。
    """
    meta = _require_meta(model_name)
    if meta["status"] != "disabled":
        raise HTTPException(
            status_code=409, detail=f"只有停用中的模型能重新啟用，'{model_name}' 目前是 {meta['status']}"
        )
    if not meta["litellm_model"]:
        raise HTTPException(
            status_code=409,
            detail=f"'{model_name}' 沒有保留上游設定（litellm_model 是空的），無法自動重建，請重新上架",
        )

    with get_conn(DB_PATH) as conn:
        row = conn.execute(
            "SELECT key_policy FROM model_key_policies WHERE model_name=?", (model_name,)
        ).fetchone()
    key_policy = row["key_policy"] if row else _infer_default_key_policy(model_name)

    litellm_params = _build_litellm_params(
        meta["litellm_model"], meta["api_key"] or None, meta["api_base"], key_policy, model_name
    )
    async with _litellm_client() as client:
        await _assert_name_available(client, model_name)
        await _post_model_new(client, model_name, litellm_params)

    return model_metadata_service.upsert_metadata(
        model_name, status="published" if meta["last_test_ok"] == 1 else "draft"
    )


async def hard_delete_model(model_name: str) -> None:
    """硬刪除：從 LiteLLM 刪掉，並清掉 model_metadata 與 key policy。不可復原。

    呼叫端有義務先讓操作者看過 model_impact() 的結果（客戶回饋明確要求「刪除前
    要看得到影響範圍」）。
    """
    async with _litellm_client() as client:
        await _delete_by_name(client, model_name)
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM model_key_policies WHERE model_name=?", (model_name,))
    model_metadata_service.delete_metadata(model_name)


def model_impact(model_name: str) -> dict:
    """刪除／停用前的影響範圍：哪些部門授權了這個模型、涵蓋多少人、哪些人是個別授權。

    含 "*"（不限制）的部門會被單獨列出來——它們沒有逐一列出 model_name，但一樣
    打得到這個模型，刪掉一樣有感。
    """
    with get_conn(DB_PATH) as conn:
        dept_rows = conn.execute("SELECT dept_id, dept_name, allowed_models FROM departments").fetchall()
        user_rows = conn.execute(
            "SELECT user_id, user_email, dept_id, models FROM users WHERE blocked=0"
        ).fetchall()
        headcount = {
            r["dept_id"]: r["n"]
            for r in conn.execute(
                "SELECT dept_id, COUNT(*) AS n FROM users WHERE blocked=0 GROUP BY dept_id"
            ).fetchall()
        }

    def _loads(raw):
        try:
            return json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    departments, wildcard_departments = [], []
    for r in dept_rows:
        allowed = _loads(r["allowed_models"])
        entry = {"dept_id": r["dept_id"], "dept_name": r["dept_name"], "headcount": headcount.get(r["dept_id"], 0)}
        if "*" in allowed:
            wildcard_departments.append(entry)
        elif model_name in allowed:
            departments.append(entry)

    users = [
        {"user_id": r["user_id"], "user_email": r["user_email"], "dept_id": r["dept_id"]}
        for r in user_rows if model_name in _loads(r["models"])
    ]

    affected_dept_ids = {d["dept_id"] for d in departments} | {d["dept_id"] for d in wildcard_departments}
    dept_headcount = sum(headcount.get(d, 0) for d in affected_dept_ids)
    extra_users = [u for u in users if u["dept_id"] not in affected_dept_ids]

    return {
        "model_name": model_name,
        "departments": departments,
        "wildcard_departments": wildcard_departments,
        "users": users,
        "dept_headcount": dept_headcount,
        "total_headcount": dept_headcount + len(extra_users),
    }


# ── WP3：發布前的測試呼叫 ─────────────────────────────────────────────────────
#
# 用 LiteLLM master key 打一次最小請求。master key 在 config/custom_auth.py 走的是
# PROXY_ADMIN 那條早退路徑，不會經過狀態閘門，所以草稿模型測得起來。

_TEST_SHAPES = {
    "chat": ("/v1/chat/completions", lambda m: {
        "model": m, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1,
    }),
    "embedding": ("/v1/embeddings", lambda m: {"model": m, "input": "ping"}),
    "rerank": ("/v1/rerank", lambda m: {
        "model": m, "query": "ping", "documents": ["ping"], "top_n": 1,
    }),
}


def _classify_test_failure(status: int, text: str) -> str:
    """把上游錯誤翻成看得懂的一句話——客戶回饋的第二個痛點就是「失敗沒有回饋」。"""
    snippet = text.strip()[:300]
    if status in (401, 403):
        return (
            f"HTTP {status}：金鑰被拒。key 來源是「各部門自己」的話，檢查該部門的 provider key "
            f"有沒有設定、是不是還停在未替換的 placeholder；「共用一把」的話檢查上架時填的 key。"
            f"\n上游回應：{snippet}"
        )
    if status == 429:
        return f"HTTP 429：上游額度或流量已用盡（配額耗盡／速率限制）。\n上游回應：{snippet}"
    if status in (400, 404):
        return (
            f"HTTP {status}：上游找不到這個模型，通常是 slug 拼錯或該帳號沒有這個模型的權限。"
            f"\n上游回應：{snippet}"
        )
    if status >= 500:
        return f"HTTP {status}：上游或 LiteLLM 內部錯誤。\n上游回應：{snippet}"
    return f"HTTP {status}：{snippet}"


async def test_model(model_name: str) -> dict:
    """依模型類型送一次最小請求，結果寫回 model_metadata（last_test_ok/at/result）。

    三種類型的最小請求形狀不同，用 chat 的形狀去測 embedding 模型只會拿到一個
    看不懂的 400——這正是客戶抱怨的那種錯誤訊息。
    """
    meta = _require_meta(model_name)
    if meta["status"] == "disabled":
        raise HTTPException(
            status_code=409, detail=f"'{model_name}' 停用中，在 LiteLLM 裡不存在，請先重新啟用再測試"
        )

    model_type = meta["model_type"] or "chat"
    path, build = _TEST_SHAPES.get(model_type, _TEST_SHAPES["chat"])
    tested_at = datetime.now(timezone.utc).isoformat()

    async with _litellm_client(timeout=60) as client:
        try:
            resp = await client.post(path, json=build(model_name))
        except httpx.TimeoutException:
            result = f"逾時（60 秒內沒有回應）。地端模型請確認 api_base 指得到、服務有起來；雲端請確認對外網路。"
            model_metadata_service.upsert_metadata(
                model_name, last_test_ok=0, last_test_at=tested_at, last_test_result=result
            )
            return {"ok": False, "result": result, "tested_at": tested_at, "model_type": model_type}
        except httpx.RequestError as exc:
            result = f"連不上 LiteLLM：{exc}"
            model_metadata_service.upsert_metadata(
                model_name, last_test_ok=0, last_test_at=tested_at, last_test_result=result
            )
            return {"ok": False, "result": result, "tested_at": tested_at, "model_type": model_type}

    ok = resp.status_code == 200
    result = f"成功（{model_type} 最小請求回 200）" if ok else _classify_test_failure(resp.status_code, resp.text)
    model_metadata_service.upsert_metadata(
        model_name, last_test_ok=int(ok), last_test_at=tested_at, last_test_result=result
    )
    return {"ok": ok, "result": result, "tested_at": tested_at, "model_type": model_type}
