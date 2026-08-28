#!/usr/bin/env python3
"""test_model_lifecycle.py — 模型生命週期／授權／額度／稽核的離線整合測試

跟 scripts/test_admin.sh、test_sync.py 不同，這支**不需要任何叢集或執行中的服務**：
在同一個 process 裡把 admin-api 的 FastAPI app 跑起來，LiteLLM 用 httpx.MockTransport
假造，資料庫用暫存檔。目的是讓「上架 → 測試 → 發布 → 停用 → 啟用 → 刪除」這條
狀態機、授權矩陣的取代式寫入、以及額度累計與強制，在部署到任何環境之前就跑得過。

  L1  上架後是草稿；草稿被 custom_auth 擋掉
  L2  沒通過測試不准發布
  L3  測試呼叫依模型類型送不同形狀的請求，失敗訊息有分類
  L4  測試通過後可以發布，發布後使用者打得通
  L5  已發布的模型不能編輯上游設定；描述性欄位仍可改
  L6  停用＝從 LiteLLM 刪掉但保留設定；重新啟用可原樣重建
  L7  刪除前算得出影響範圍（部門、人數）
  A1  授權矩陣的差異預覽不寫入
  A2  確認後寫入 DB 並呼叫 push
  A3  授權不存在的模型會被擋下來
  B1  額度只記錄時不擋
  B2  額度強制時超額擋下來（429）
  B3  用量累計記在公開 model_name 上
  U1  稽核紀錄含 before/after，CSV 匯出是 UTF-8 BOM

用法：
  python3 scripts/test_model_lifecycle.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "admin-api"))
sys.path.insert(0, str(REPO / "config"))

_TMP = tempfile.mkdtemp(prefix="firdi-lifecycle-")
os.environ["USER_AUTH_DB_PATH"] = os.path.join(_TMP, "users.db")
os.environ["ADMIN_AUDIT_LOG_PATH"] = os.path.join(_TMP, "audit.jsonl")
# config/ 的兩個 hook 都會 write_log 到 LOG_PATH；不換掉會去寫 /app/logs 而 PermissionError
os.environ["LOG_PATH"] = os.path.join(_TMP, "usage.jsonl")
os.environ["LITELLM_MASTER_KEY"] = "sk-test-master"
os.environ["LITELLM_URL"] = "http://litellm-mock"
os.environ["ADMIN_WEB_USERNAMES"] = "firdiadm"

import httpx  # noqa: E402

GREEN, RED, YELLOW, CYAN, BOLD, NC = (
    "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0;36m", "\033[1m", "\033[0m"
)
PASS = FAIL = 0


def section(msg):
    print(f"\n{CYAN}{BOLD}── {msg} ──────────────────────────────{NC}")


def check(cond, msg, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"{GREEN}  ✓ PASS{NC} {msg}")
    else:
        FAIL += 1
        print(f"{RED}  ✗ FAIL{NC} {msg}" + (f"\n         {extra}" if extra else ""))


# ── 假的 LiteLLM ──────────────────────────────────────────────────────────────

class FakeLiteLLM:
    """只實作這個專案真的會用到的四支：/models /model/info /model/new /model/delete，
    外加測試呼叫會打的三種推論端點。deployments 用遞增 id 模擬 LiteLLM 的行為
    ——同一個 model_name 刪掉重建之後 id 會換一個，狀態機不能依賴 id。
    """

    def __init__(self):
        self.deployments: list[dict] = []
        self._next_id = 1
        self.yaml_models = ["gemma-4-31B-it", "embeddinggemma-300m"]
        self.infer_status = 200
        self.infer_body = {"ok": True}
        self.calls: list[tuple[str, dict]] = []

    def add_yaml(self, name):
        self.yaml_models.append(name)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))

        if path == "/models":
            ids = self.yaml_models + [d["model_name"] for d in self.deployments]
            return httpx.Response(200, json={"data": [{"id": i} for i in sorted(set(ids))]})

        if path == "/model/info":
            data = [
                {"model_name": n, "litellm_params": {"model": n}, "model_info": {"id": f"yaml-{n}", "db_model": False}}
                for n in self.yaml_models
            ] + [
                {"model_name": d["model_name"], "litellm_params": d["litellm_params"],
                 "model_info": {"id": d["id"], "db_model": True}}
                for d in self.deployments
            ]
            return httpx.Response(200, json={"data": data})

        if path == "/model/new":
            self.deployments.append({
                "id": f"dep-{self._next_id}", "model_name": body["model_name"],
                "litellm_params": body["litellm_params"],
            })
            self._next_id += 1
            return httpx.Response(200, json={"status": "ok"})

        if path == "/model/delete":
            before = len(self.deployments)
            self.deployments = [d for d in self.deployments if d["id"] != body.get("id")]
            return httpx.Response(200 if len(self.deployments) < before else 404, json={})

        if path in ("/v1/chat/completions", "/v1/embeddings", "/v1/rerank"):
            return httpx.Response(self.infer_status, json=self.infer_body)

        return httpx.Response(404, json={"error": f"unmocked {path}"})


FAKE = FakeLiteLLM()

from database import DB_PATH, get_conn, init_db  # noqa: E402

init_db(DB_PATH)

from services import model_access_service, model_metadata_service, models_service  # noqa: E402

# LiteLLM client 統一從 models_service._litellm_client 取得，換掉它就等於換掉整條路徑
_real_client = models_service._litellm_client
models_service._litellm_client = lambda timeout=15: httpx.AsyncClient(
    base_url="http://litellm-mock", transport=httpx.MockTransport(FAKE.handler), timeout=timeout
)

# push 走的是 openwebui_sync_service（要有真的 OpenWebUI），這裡只記錄有沒有被呼叫
PUSHES: list[dict] = []


async def _fake_push(dry_run: bool = False):
    PUSHES.append({"dry_run": dry_run})
    return {"status": "ok", "target": "a", "dry_run": dry_run, "results": [],
            "missing_groups": [], "missing_users": []}


model_access_service.push_now = _fake_push

from models import ExternalModelIn  # noqa: E402
import audit  # noqa: E402


def _stub_litellm() -> None:
    """config/ 底下的兩個 hook 是給 litellm pod 用的，本機通常沒裝 litellm 套件。

    只補上這兩支真的會用到的東西：ProxyException（拒絕請求時丟的例外，帶
    message/type/param/code）、UserAPIKeyAuth（放行時回傳的物件）、
    LitellmUserRoles.PROXY_ADMIN、以及 CustomLogger 基底類別。裝了真的 litellm
    時這個 stub 完全不會生效——測到的就是正牌型別。
    """
    import types

    class ProxyException(Exception):
        def __init__(self, message="", type="", param="", code=500):
            super().__init__(message)
            self.message, self.type, self.param, self.code = message, type, param, code

    class UserAPIKeyAuth:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class LitellmUserRoles:
        PROXY_ADMIN = "proxy_admin"

    class CustomLogger:
        pass

    litellm = types.ModuleType("litellm")
    proxy = types.ModuleType("litellm.proxy")
    ptypes = types.ModuleType("litellm.proxy._types")
    ptypes.ProxyException = ProxyException
    ptypes.UserAPIKeyAuth = UserAPIKeyAuth
    ptypes.LitellmUserRoles = LitellmUserRoles
    integrations = types.ModuleType("litellm.integrations")
    clog = types.ModuleType("litellm.integrations.custom_logger")
    clog.CustomLogger = CustomLogger
    for name, mod in [
        ("litellm", litellm), ("litellm.proxy", proxy), ("litellm.proxy._types", ptypes),
        ("litellm.integrations", integrations), ("litellm.integrations.custom_logger", clog),
    ]:
        sys.modules.setdefault(name, mod)


try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    _stub_litellm()
    print(f"{YELLOW}  ⓘ 本機沒有 litellm 套件，用最小 stub 代替（只影響 config/ 的兩個 hook）{NC}")

import custom_auth  # noqa: E402
import custom_logger  # noqa: E402

run = asyncio.run
ADMIN = {"preferred_username": "firdiadm", "sub": "test-sub", "email": "admin@example.com"}


def seed_departments():
    with get_conn(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO departments (dept_id, dept_name, allowed_models) VALUES (?, ?, ?)",
            ("RD", "研發部", json.dumps(["gemma-4-31B-it"])),
        )
        conn.execute(
            "INSERT INTO departments (dept_id, dept_name, allowed_models) VALUES (?, ?, ?)",
            ("SALES", "業務部", json.dumps([])),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO users (api_key, key_name, user_id, user_email, dept_id, models) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"sk-rd-{i}", f"rd{i}", f"uid-rd-{i}", f"rd{i}@example.com", "RD", "[]"),
            )
        conn.execute(
            "INSERT INTO users (api_key, key_name, user_id, user_email, dept_id, models) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sk-sales-0", "sales0", "uid-sales-0", "sales0@example.com", "SALES", "[]"),
        )


seed_departments()
NAME = "openrouter/anthropic/claude-sonnet-4-5"


def _bust_auth_cache():
    """custom_auth 的設定快取是 db_version + 30 秒 TTL；測試裡直接清掉重讀。"""
    custom_auth._CACHE_DATA = None
    custom_auth._CACHE_VERSION = -1
    custom_auth._CACHE_LOADED_AT = 0.0


async def _auth_model(api_key: str, model: str):
    """模擬一次 custom_auth 認證，回傳 (是否放行, 例外)。"""
    _bust_auth_cache()

    class _Req:
        url = type("U", (), {"path": "/v1/chat/completions"})()
        headers: dict = {}

        async def body(self):
            return json.dumps({"model": model}).encode()

    try:
        await custom_auth.user_api_key_auth(_Req(), api_key)
        return True, None
    except Exception as exc:  # ProxyException
        return False, exc


# ══ L1：上架後是草稿，且使用者打不通 ══════════════════════════════════════════
section("L1 上架後是草稿，custom_auth 擋掉一般使用者")

run(models_service.create_external_model(ExternalModelIn(
    model_name=NAME, model="openai/anthropic/claude-sonnet-4-5",
    api_base="https://openrouter.ai/api/v1", key_policy="dept:openrouter",
    display_name="Claude Sonnet 4.5", model_type="chat", cost_center="RD",
    notes="客戶指定", upstream="openrouter", status="draft",
)))
meta = model_metadata_service.get_metadata(NAME)
check(meta["status"] == "draft", "上架後 status=draft", f"got {meta['status']}")
check(meta["display_name"] == "Claude Sonnet 4.5", "顯示名稱有存下來")
check(meta["cost_center"] == "RD", "成本歸屬部門有存下來")
check(any(d["model_name"] == NAME for d in FAKE.deployments), "已註冊到 LiteLLM（草稿才測得起來）")

# 授權給 RD，但因為是草稿仍應被擋
model_access_service.apply_dept("RD", ["gemma-4-31B-it", NAME], {"gemma-4-31B-it", NAME})
ok, exc = run(_auth_model("sk-rd-0", NAME))
check(not ok, "草稿模型：已授權的使用者仍被擋", f"ok={ok}")
check(exc is not None and "draft" not in str(exc) and "草稿" in str(exc.message),
      "擋下來的訊息說得出「還在草稿狀態」", str(exc))
ok, _ = run(_auth_model("sk-rd-0", "gemma-4-31B-it"))
check(ok, "同一個使用者打已發布的既有模型不受影響")

# ══ L2：沒通過測試不准發布 ════════════════════════════════════════════════════
section("L2 沒通過測試不准發布")
try:
    run(models_service.publish_model(NAME))
    check(False, "未測試就發布應該被擋")
except Exception as exc:
    check(getattr(exc, "status_code", None) == 409, "未測試發布 → 409", str(exc))
    check("測試" in str(exc.detail), "錯誤訊息指出要先測試", str(exc.detail))

# ══ L3：測試呼叫的形狀與失敗分類 ══════════════════════════════════════════════
section("L3 測試呼叫依類型送不同請求，失敗訊息有分類")
FAKE.calls.clear()
FAKE.infer_status, FAKE.infer_body = 401, {"error": "invalid api key"}
result = run(models_service.test_model(NAME))
check(not result["ok"], "401 → 測試失敗")
check("金鑰" in result["result"], "401 分類成金鑰問題", result["result"])
check(FAKE.calls[-1][0] == "/v1/chat/completions", "chat 模型打 chat 端點", FAKE.calls[-1][0])
check(model_metadata_service.get_metadata(NAME)["last_test_ok"] == 0, "失敗結果有存回 DB")

FAKE.infer_status = 429
run(models_service.test_model(NAME))
check("額度" in model_metadata_service.get_metadata(NAME)["last_test_result"], "429 分類成額度耗盡")
FAKE.infer_status = 404
run(models_service.test_model(NAME))
check("找不到" in model_metadata_service.get_metadata(NAME)["last_test_result"], "404 分類成模型不存在")

# embedding 模型應該打 /v1/embeddings
EMB = "openrouter/text-embed"
run(models_service.create_external_model(ExternalModelIn(
    model_name=EMB, model="openai/text-embed", key_policy="dept:openrouter",
    model_type="embedding", upstream="openrouter", status="draft",
)))
FAKE.infer_status, FAKE.infer_body = 200, {"data": []}
FAKE.calls.clear()
run(models_service.test_model(EMB))
check(FAKE.calls[-1][0] == "/v1/embeddings", "embedding 模型打 embeddings 端點", FAKE.calls[-1][0])

# 認得出來的特定上游問題（真實樣本，2026-08-28 在 ai-x-dev 上遇到）：
# OpenRouter 帳號有 allowed-providers 白名單，模型存在、key 也有效，被帳號設定擋掉。
# 原本會被歸到「slug 拼錯或沒有權限」，方向對但沒點破要去改哪裡。
REAL_ALLOWED_PROVIDERS = (
    '{"error":{"message":"litellm.NotFoundError: NotFoundError: OpenAIException - '
    "No allowed providers are available for the selected model. Providers serving "
    "qwen/qwen3.8-flash-20260826: alibaba, but your account's allowed-providers setting "
    'permits only: xai, google-vertex, nvidia, openai, anthropic, perplexity, deepinfra, '
    'together, fireworks","type":"invalid_request_error"}}'
)
msg = models_service._classify_test_failure(404, REAL_ALLOWED_PROVIDERS)
check("allowed providers" in msg and "白名單" in msg,
      "allowed-providers 的 404 認得出來，不會被誤導成「slug 拼錯」", msg[:120])
check("Allowed Providers" in msg, "訊息講得出到 OpenRouter 哪裡去改")
check("fireworks" in msg,
      "上游原文沒有被截掉關鍵資訊（白名單清單本身就佔兩百多字元）", msg[-80:])

# ══ L4：測試通過 → 發布 → 使用者打得通 ════════════════════════════════════════
section("L4 測試通過後發布，使用者才打得通")
FAKE.infer_status = 200
result = run(models_service.test_model(NAME))
check(result["ok"], "200 → 測試通過")
run(models_service.publish_model(NAME))
check(model_metadata_service.get_metadata(NAME)["status"] == "published", "發布後 status=published")
ok, exc = run(_auth_model("sk-rd-0", NAME))
check(ok, "已授權的使用者現在打得通", str(exc))
ok, _ = run(_auth_model("sk-sales-0", NAME))
check(not ok, "沒授權的部門仍然打不通（狀態閘門不會取代授權檢查）")

# ══ L5：已發布不能改上游，描述性欄位仍可改 ════════════════════════════════════
section("L5 已發布鎖定上游設定，描述性欄位仍可改")
try:
    run(models_service.update_draft_model(
        NAME, model="openai/other", api_base=None, api_key=None, key_policy="model",
        display_name="x", model_type="chat", cost_center="", budget_limit_usd=None,
        budget_enforce=False, budget_period="monthly", notes="", upstream="openrouter",
    ))
    check(False, "已發布的模型改上游應該被擋")
except Exception as exc:
    check(getattr(exc, "status_code", None) == 409, "已發布改上游 → 409", str(exc))

run(models_service.update_descriptive_fields(
    NAME, display_name="Claude 4.5", cost_center="SALES", budget_limit_usd=1.0,
    budget_enforce=False, budget_period="monthly", notes="改過了",
))
meta = model_metadata_service.get_metadata(NAME)
check(meta["display_name"] == "Claude 4.5" and meta["cost_center"] == "SALES",
      "已發布的模型仍可改顯示名稱／成本歸屬")
check(meta["status"] == "published", "改描述性欄位不會動到狀態")

# ══ L6：停用與重新啟用 ════════════════════════════════════════════════════════
section("L6 停用保留設定，重新啟用原樣重建")
old_ids = {d["id"] for d in FAKE.deployments if d["model_name"] == NAME}
run(models_service.disable_model(NAME))
check(not any(d["model_name"] == NAME for d in FAKE.deployments), "停用後 LiteLLM 裡不存在")
meta = model_metadata_service.get_metadata(NAME)
check(meta["status"] == "disabled" and meta["litellm_model"] == "openai/anthropic/claude-sonnet-4-5",
      "停用後設定完整保留")
with get_conn(DB_PATH) as conn:
    allowed = json.loads(conn.execute(
        "SELECT allowed_models FROM departments WHERE dept_id='RD'").fetchone()[0])
check(NAME in allowed, "停用刻意不動 allowed_models（授權是獨立的一件事）")
ok, _ = run(_auth_model("sk-rd-0", NAME))
check(not ok, "停用後使用者打不通")

listing = run(models_service.list_external_models())
check(any(m["model_name"] == NAME and not m["registered"] for m in listing["models"]),
      "停用中的模型仍列在管理清單裡（否則按不到「重新啟用」）")

run(models_service.enable_model(NAME))
new = [d for d in FAKE.deployments if d["model_name"] == NAME]
check(len(new) == 1, "重新啟用後又註冊回 LiteLLM")
check(new[0]["id"] not in old_ids, "重建後 deployment id 換了一個（狀態機不能依賴 id）")
check(new[0]["litellm_params"]["model"] == "openai/anthropic/claude-sonnet-4-5", "上游設定原樣還原")
check(model_metadata_service.get_metadata(NAME)["status"] == "published",
      "測試通過過的模型啟用後回到 published")
ok, _ = run(_auth_model("sk-rd-0", NAME))
check(ok, "重新啟用後不用重放授權就打得通")

# 沒通過測試的草稿停用再啟用，不該偷偷變成已發布
UNTESTED = "openrouter/never-tested"
run(models_service.create_external_model(ExternalModelIn(
    model_name=UNTESTED, model="openai/never-tested", key_policy="dept:openrouter",
    upstream="openrouter", status="draft",
)))
run(models_service.disable_model(UNTESTED))
run(models_service.enable_model(UNTESTED))
check(model_metadata_service.get_metadata(UNTESTED)["status"] == "draft",
      "沒測過的模型啟用後仍是草稿（不能當成繞過發布閘門的後門）")

# ══ L7：刪除前的影響範圍 ══════════════════════════════════════════════════════
section("L7 刪除前算得出影響範圍")
model_access_service.apply_user("uid-sales-0", [NAME], {NAME, "gemma-4-31B-it"})
impact = models_service.model_impact(NAME)
check([d["dept_id"] for d in impact["departments"]] == ["RD"], "列得出授權的部門", str(impact))
check(impact["dept_headcount"] == 3, "算得出部門人數", str(impact["dept_headcount"]))
check(impact["total_headcount"] == 4, "個別授權的人也算進總影響人數", str(impact["total_headcount"]))
check([u["user_id"] for u in impact["users"]] == ["uid-sales-0"], "列得出個別授權的使用者")

# ══ A1/A2/A3：授權矩陣 ════════════════════════════════════════════════════════
section("A 授權矩陣：預覽不寫入、確認才寫入並 push")
known = {"gemma-4-31B-it", "embeddinggemma-300m", NAME, EMB}
before = model_access_service.get_dept("SALES")["allowed_models"]
diff = model_access_service.preview_dept("SALES", ["gemma-4-31B-it"])
check(diff["added"] == ["gemma-4-31B-it"] and diff["changed"], "預覽算得出新增的模型")
check(model_access_service.get_dept("SALES")["allowed_models"] == before, "預覽沒有寫入任何東西")

PUSHES.clear()
applied = model_access_service.apply_dept("SALES", ["gemma-4-31B-it"], known)
check(model_access_service.get_dept("SALES")["allowed_models"] == ["gemma-4-31B-it"], "確認後有寫入")
check(applied["before"] == before and applied["added"] == ["gemma-4-31B-it"],
      "回傳值含 before/after，稽核才記得下來")

diff = model_access_service.preview_dept("RD", [])
check(diff["removed"] and diff["changed"], "取消全部勾選＝收回授權，預覽看得出來", str(diff))

try:
    model_access_service.apply_dept("SALES", ["not-a-real-model"], known)
    check(False, "授權不存在的模型應該被擋")
except Exception as exc:
    check(getattr(exc, "status_code", None) == 422, "授權不存在的模型 → 422", str(exc))
    check("找不到" in str(exc.detail), "錯誤訊息說得出是哪個模型找不到", str(exc.detail))

# ══ B：額度累計與強制 ═════════════════════════════════════════════════════════
section("B 額度：只記錄 vs 真的擋下來")
custom_logger.record_spend(NAME, 0.6)
custom_logger.record_spend(NAME, 0.5)
# 成本來源的優先序（2026-08-28 在 ai-x-dev 驗收 W-51 時踩到的真實 bug）：
# LiteLLM 的 response_cost 是用內建定價表算的，查不到的模型一律回 0.0 而不是
# None——透過 OpenRouter 上架的模型 litellm_params.model 是 openai/<slug>，
# 定價表裡通常沒有。於是每筆都記 0、額度永遠不觸發，而且沒有任何錯誤訊息。
# 真正的金額 OpenRouter 有給，在 usage.cost 裡。
class _Resp:
    def __init__(self, usage): self.usage = usage

_REAL_USAGE = {"completion_tokens": 56, "prompt_tokens": 9, "total_tokens": 65,
               "cost": 0.000108375, "is_byok": False}
check(custom_logger._extract_cost({"response_cost": 0.0}, _Resp(_REAL_USAGE)) == 0.000108375,
      "上游 usage.cost 優先於 LiteLLM 算出來的 0.0")
check(custom_logger._extract_cost({"response_cost": 0.0042}, _Resp({"total_tokens": 100})) == 0.0042,
      "上游沒給價時才用 LiteLLM 算的")
check(custom_logger._extract_cost({}, _Resp({"total_tokens": 215})) is None,
      "兩邊都沒有 → None 而不是 0（0 會讓「算不出成本」看起來像「免費」）")
check(custom_logger._extract_cost({"response_cost": 0.0}, None) == 0.0,
      "沒有 response_obj 也不會爆，且 LiteLLM 明講的 0 就記 0（地端模型確實免費）")

spend = model_metadata_service.get_spend(NAME)
check(abs(spend["monthly"] - 1.1) < 1e-9, "用量累加正確", str(spend))
check(spend["calls"] == 2, "呼叫次數累加正確")
check(abs(spend["total"] - 1.1) < 1e-9, "累計欄位也同步累加")

# 額度 1.0、只記錄 → 不擋
run(models_service.update_descriptive_fields(
    NAME, display_name="Claude 4.5", cost_center="SALES", budget_limit_usd=1.0,
    budget_enforce=False, budget_period="monthly", notes="",
))
ok, exc = run(_auth_model("sk-rd-0", NAME))
check(ok, "超額但 budget_enforce=0 → 放行（只記錄）", str(exc))
state = model_metadata_service.budget_state(model_metadata_service.get_metadata(NAME), spend)
check(state["exceeded"] and not state["enforced"], "畫面上仍看得出已經超額", str(state))

# 改成強制 → 擋
run(models_service.update_descriptive_fields(
    NAME, display_name="Claude 4.5", cost_center="SALES", budget_limit_usd=1.0,
    budget_enforce=True, budget_period="monthly", notes="",
))
ok, exc = run(_auth_model("sk-rd-0", NAME))
check(not ok, "超額且 budget_enforce=1 → 擋下來")
check(exc is not None and getattr(exc, "code", None) in (429, "429"), "擋下來的狀態碼是 429", str(exc))

# 還沒超額的模型不受影響
run(models_service.update_descriptive_fields(
    NAME, display_name="Claude 4.5", cost_center="SALES", budget_limit_usd=100.0,
    budget_enforce=True, budget_period="monthly", notes="",
))
ok, exc = run(_auth_model("sk-rd-0", NAME))
check(ok, "額度還沒用完 → 正常放行", str(exc))

# 用量要記在公開 model_name 上，不是上游的 litellm_params.model
check(model_metadata_service.get_spend("openai/anthropic/claude-sonnet-4-5")["monthly"] == 0.0,
      "用量沒有被記到上游名稱底下")

# ══ U1：稽核 ══════════════════════════════════════════════════════════════════
section("U 稽核紀錄與匯出")
audit.write_audit(ADMIN, "set_dept_models", "SALES", "success",
                  {"before": [], "after": ["gemma-4-31B-it"], "added": ["gemma-4-31B-it"]})
audit.write_audit(ADMIN, "publish_model", NAME, "success",
                  {"before": {"status": "draft"}, "after": {"status": "published"}})
records, total = audit.read_audit()
check(total == 2, "讀得回寫進去的紀錄", str(total))
check(records[0]["action"] == "publish_model", "新的在前")
filtered, _ = audit.read_audit(action="set_dept_models")
check(len(filtered) == 1, "動作過濾有效")
filtered, _ = audit.read_audit(target="sales")
check(len(filtered) == 1, "目標過濾（不分大小寫）有效")
b, a, _ = audit.detail_parts(records[0]["detail"])
check("draft" in b and "published" in a, "before/after 拆得出來", f"{b} / {a}")
csv_bytes = audit.to_csv(records)
check(csv_bytes.startswith(b"\xef\xbb\xbf"), "CSV 帶 UTF-8 BOM（Excel 開中文不亂碼）")
check("動作".encode() in csv_bytes, "CSV 欄位標題是中文")
check(audit.action_label("set_provider_key:openai") == "設定部門 Provider Key（openai）",
      "帶參數的動作代碼也翻得出中文")

# ══ 路由註冊與頁面實際渲染 ════════════════════════════════════════════════════
section("R 路由都掛得上去")
from main import app  # noqa: E402

paths = {r.path for r in app.routes}
for p in ["/api/v1/admin/web/models/detail", "/api/v1/admin/web/models/test",
          "/api/v1/admin/web/models/publish", "/api/v1/admin/web/models/disable",
          "/api/v1/admin/web/models/enable", "/api/v1/admin/web/models/hard-delete",
          "/api/v1/admin/web/models/fields", "/api/v1/admin/web/models/edit",
          "/api/v1/admin/web/access", "/api/v1/admin/web/access/departments/preview",
          "/api/v1/admin/web/access/departments/apply", "/api/v1/admin/web/access/users",
          "/api/v1/admin/web/access/users/edit", "/api/v1/admin/web/audit",
          "/api/v1/admin/web/audit/export"]:
    check(p in paths, f"路由存在：{p}")


# ══ W：每一頁真的渲染得出來 ═══════════════════════════════════════════════════
#
# 這些頁面是很長的 f-string，少一個變數只有在真的送出請求時才會炸。認證用
# dependency_overrides 換掉（Keycloak 不在離線測試的範圍內），其餘一路走真的
# router → service → mock LiteLLM。
section("W 頁面渲染與表單往返")

from admin_auth import require_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app.dependency_overrides[require_admin] = lambda: ADMIN
client = TestClient(app)

enc_name = NAME.replace("/", "%2F")
for label, url in [
    ("總覽", "/api/v1/admin/web"),
    ("模型清單", "/api/v1/admin/web/models"),
    ("模型詳情（已發布）", f"/api/v1/admin/web/models/detail?model_name={enc_name}"),
    ("上架第一步", "/api/v1/admin/web/models/new"),
    ("上架第二步", "/api/v1/admin/web/models/new?upstream=openrouter"),
    ("上架表單", "/api/v1/admin/web/models/new?upstream=openrouter&key_source=dept"),
    ("上架表單（地端 vLLM）", "/api/v1/admin/web/models/new?upstream=vllm"),
    ("授權矩陣", "/api/v1/admin/web/access"),
    ("使用者搜尋", "/api/v1/admin/web/access/users?q=rd"),
    ("個人授權編輯", "/api/v1/admin/web/access/users/edit?user_id=uid-rd-0"),
    ("Provider Key 選單", "/api/v1/admin/web/keys"),
    ("Provider Key 表單", "/api/v1/admin/web/keys?provider=openrouter"),
    ("稽核紀錄", "/api/v1/admin/web/audit"),
]:
    resp = client.get(url)
    check(resp.status_code == 200, f"{label} 渲染成功", f"HTTP {resp.status_code}: {resp.text[:300]}")

resp = client.get(f"/api/v1/admin/web/models/detail?model_name={enc_name}")
check("Claude 4.5" in resp.text, "詳情頁顯示得出顯示名稱")
check("影響範圍" in resp.text and "人" in resp.text, "詳情頁顯示得出影響範圍")

# 草稿模型的詳情頁：要有「發布」按鈕與「編輯上游設定」連結
resp = client.get(f"/api/v1/admin/web/models/detail?model_name={UNTESTED.replace('/', '%2F')}")
check(resp.status_code == 200, "草稿詳情頁渲染成功", resp.text[:300])
check("編輯上游設定" in resp.text, "草稿詳情頁有編輯上游設定的入口")
check("disabled" in resp.text.split("發布</button>")[0][-200:],
      "沒通過測試時「發布」按鈕是 disabled 的")

resp = client.get(f"/api/v1/admin/web/models/edit?model_name={UNTESTED.replace('/', '%2F')}")
check(resp.status_code == 200, "草稿編輯表單渲染成功", resp.text[:300])
resp = client.get(f"/api/v1/admin/web/models/edit?model_name={enc_name}")
check(resp.status_code == 409, "已發布的模型開編輯表單 → 409", f"HTTP {resp.status_code}")

# 稽核的變更內容不能被截掉就算了：一個模型的完整欄位將近 300 字元，而 key 的
# 末四碼正好落在後段——截斷又不給展開的話，畫面上永遠看不到「這次用的是哪把 key」，
# 稽核就失去意義了。
audit.write_audit(ADMIN, "create_external_model", "長內容測試", "success", {
    "before": None,
    "after": {"litellm_model": "openai/x" * 20, "api_key": "...9876", "status": "draft"},
})
resp = client.get("/api/v1/admin/web/audit")
check("<details" in resp.text, "過長的變更內容收進可展開的 details")
check("…</summary>" in resp.text, "收合時有「…」提示後面還有內容")
check("...9876" in resp.text, "展開後看得到完整內容（含 key 末四碼）", "找不到 ...9876")
check('{"status": "draft"}' in resp.text or "&quot;status&quot;" in resp.text,
      "短的變更內容仍然直接顯示，不用點開")

# ── YAML model_list 定義的地端模型 ──────────────────────────────────────────
# 授權矩陣本來就含它們（走 LiteLLM /models），模型清單卻不含，同一個介面對
# 「有哪些模型」給兩種答案。納入之後要守住兩件事：curl 端點的契約不能變，
# 以及它們永遠不能顯示成「已發布」——它們沒有狀態機。
resp = client.get("/api/v1/admin/web/models")
check("gemma-4-31B-it" in resp.text, "模型清單看得到 YAML 定義的地端模型")
resp = client.get("/api/v1/admin/web/models/detail?model_name=gemma-4-31B-it")
check(resp.status_code == 200, "地端模型的詳情頁打得開", f"HTTP {resp.status_code}")
check("badge-legacy" in resp.text, "地端模型標「既有」")
check("永久刪除" not in resp.text and "停用</button>" not in resp.text,
      "地端模型不提供停用與刪除（那要改 YAML 並重啟 pod）")

curl_listing = run(models_service.list_external_models())            # curl 契約
web_listing = run(models_service.list_external_models(include_yaml=True))
curl_names = {m["model_name"] for m in curl_listing["models"]}
web_names = {m["model_name"] for m in web_listing["models"]}
check("gemma-4-31B-it" not in curl_names, "curl 端點仍然不含 YAML 模型（契約不變）")
check("gemma-4-31B-it" in web_names, "admin-web 那份含 YAML 模型")

# 納管：存一次描述性欄位就建起紀錄，但不能因此變成「已發布」
resp = client.post("/api/v1/admin/web/models/fields", data={
    "model_name": "gemma-4-31B-it", "model_type": "chat", "display_name": "Gemma 4 31B",
    "cost_center": "RD", "budget_limit_usd": "", "budget_period": "monthly", "notes": "地端主力",
}, follow_redirects=False)
check(resp.status_code == 303, "地端模型可以納管", str(resp.status_code))
adopted = model_metadata_service.get_metadata("gemma-4-31B-it")
check(adopted["has_record"] and adopted["display_name"] == "Gemma 4 31B", "納管後欄位存得下來")
resp = client.get("/api/v1/admin/web/models")
# 抓「那一列」而不是從模型名稱起算固定字元數——固定字元數會切進下一列，
# 讀到別的模型的徽章。
row = next((m.group(0) for m in re.finditer(r"<tr>.*?</tr>", resp.text, re.S)
            if "gemma-4-31B-it" in m.group(0)), "")
badges = re.findall(r'badge-([a-z]+)">', row)
check(badges == ["legacy"], "納管後仍然是「既有」，不會冒充成「已發布」", f"實際徽章：{badges}")

# 納管會在 model_metadata 留下紀錄——curl 端點不能因此把它當成「停用中的模型」列出來
curl_names_after = {m["model_name"] for m in run(models_service.list_external_models())["models"]}
check("gemma-4-31B-it" not in curl_names_after,
      "納管後 curl 端點仍然不含它（沒有被誤判成停用中）", str(sorted(curl_names_after)))

# 類型設對，測試呼叫才會用對的請求形狀
client.post("/api/v1/admin/web/models/fields", data={
    "model_name": "embeddinggemma-300m", "model_type": "embedding", "budget_period": "monthly",
}, follow_redirects=False)
FAKE.calls.clear()
run(models_service.test_model("embeddinggemma-300m"))
check(FAKE.calls[-1][0] == "/v1/embeddings",
      "地端 embedding 模型的類型設定會影響測試端點", FAKE.calls[-1][0])

resp = client.get("/api/v1/admin/web/audit/export")
check(resp.status_code == 200 and resp.content.startswith(b"\xef\xbb\xbf"), "CSV 匯出下載得到")
check("attachment" in resp.headers.get("content-disposition", ""), "CSV 匯出是附件下載")

# 授權矩陣的完整往返：預覽 → 確認 → 寫入 + push
PUSHES.clear()
resp = client.post("/api/v1/admin/web/access/departments/preview",
                   data={"grants": [f"SALES{'|'}gemma-4-31B-it", f"SALES{'|'}{NAME}"]})
check(resp.status_code == 200 and "差異預覽" in resp.text, "矩陣預覽頁渲染成功", resp.text[:300])
check(not PUSHES, "預覽階段沒有 push")
check(f'value="SALES{"|"}{NAME}"' in resp.text, "預覽頁把選擇帶進 hidden 欄位")

resp = client.post("/api/v1/admin/web/access/departments/apply",
                   data={"grants": [f"SALES{'|'}gemma-4-31B-it", f"SALES{'|'}{NAME}"],
                         "scope": ["SALES", "RD"]})
check(resp.status_code == 200 and "已生效" in resp.text, "矩陣確認頁渲染成功", resp.text[:300])
check(len(PUSHES) == 1 and PUSHES[0]["dry_run"] is False, "確認後 push 了一次（且不是 dry-run）")
check(model_access_service.get_dept("SALES")["allowed_models"] == sorted(["gemma-4-31B-it", NAME]),
      "確認後 DB 真的被改了")

# scope 裡有但沒被勾的部門＝清空，不是「沒送出」
check(model_access_service.get_dept("RD")["allowed_models"] == [],
      "沒被勾選的部門被清空（取代式語意，不是漏送）")

resp = client.post("/api/v1/admin/web/access/departments/preview",
                   data={"grants": [f"SALES{'|'}gemma-4-31B-it", f"SALES{'|'}{NAME}"]})
check("沒有任何變化" in resp.text, "送出跟現況一樣時明講「沒有變化」")

# 授權未知模型的 422 一定要打在 router 層測：service 層的 validate_models 早就有
# 測到（見上面 A 區），但 router 曾經先用「& 已知模型」把未知名稱濾掉才呼叫
# service，於是那個 422 變成永遠觸發不到的死碼，未知的授權被靜默吞掉。
for path, data, label in [
    ("access/departments/preview", {"grants": ["RD|does-not-exist"]}, "部門預覽"),
    ("access/departments/apply", {"grants": ["RD|does-not-exist"], "scope": ["RD"]}, "部門確認"),
    ("access/users/preview", {"user_id": "uid-rd-0", "models": ["does-not-exist"]}, "個人預覽"),
    ("access/users/apply", {"user_id": "uid-rd-0", "models": ["does-not-exist"]}, "個人確認"),
]:
    resp = client.post(f"/api/v1/admin/web/{path}", data=data)
    check(resp.status_code == 422 and "LiteLLM 找不到" in resp.text,
          f"{label}：授權 LiteLLM 不存在的模型 → 422", f"HTTP {resp.status_code}: {resp.text[:200]}")

# 反面：DB 裡本來就有的失效授權（模型被刪了、授權還在）不能因此卡住儲存——
# 它沒有對應的 checkbox，所以不會出現在送上來的清單裡，該被正常清理掉。
with get_conn(DB_PATH) as conn:
    conn.execute("UPDATE departments SET allowed_models=? WHERE dept_id='RD'",
                 (json.dumps(["gemma-4-31B-it", "deleted-long-ago"]),))
resp = client.get("/api/v1/admin/web/access")
check("失效：deleted-long-ago" in resp.text, "矩陣頁把失效的授權標出來", resp.text[:200])
resp = client.post("/api/v1/admin/web/access/departments/apply",
                   data={"grants": ["RD|gemma-4-31B-it"], "scope": ["RD"]})
check(resp.status_code == 200 and "已生效" in resp.text,
      "失效授權不會讓儲存變成 422", f"HTTP {resp.status_code}: {resp.text[:200]}")
check(model_access_service.get_dept("RD")["allowed_models"] == ["gemma-4-31B-it"],
      "失效授權被正常清理掉")

# 生命週期的 POST 端點：model_name 走表單欄位（名稱含斜線，放不進路徑）
resp = client.post("/api/v1/admin/web/models/test", data={"model_name": NAME}, follow_redirects=False)
check(resp.status_code == 303, "測試呼叫後 303 導回詳情頁（重新整理不會重跑）", str(resp.status_code))
check("model_name=openrouter%2F" in resp.headers.get("location", ""),
      "導回的網址把含斜線的 model_name 正確編碼", resp.headers.get("location", ""))

resp = client.post("/api/v1/admin/web/models/fields",
                   data={"model_name": NAME, "display_name": "改成這個", "cost_center": "RD",
                         "budget_limit_usd": "", "budget_period": "monthly", "notes": "n"},
                   follow_redirects=False)
check(resp.status_code == 303, "描述性欄位存檔 303", str(resp.status_code))
meta = model_metadata_service.get_metadata(NAME)
check(meta["display_name"] == "改成這個" and meta["budget_limit_usd"] is None,
      "額度留空＝取消額度，不是 422", str(meta["budget_limit_usd"]))

resp = client.post("/api/v1/admin/web/models/fields",
                   data={"model_name": NAME, "display_name": "x", "cost_center": "",
                         "budget_limit_usd": "abc", "budget_period": "monthly", "notes": ""})
check(resp.status_code == 422 and "要是數字" in resp.text, "額度填非數字 → 看得懂的 422", resp.text[:200])

resp = client.post("/api/v1/admin/web/models/fields",
                   data={"model_name": NAME, "display_name": "x", "cost_center": "",
                         "budget_limit_usd": "", "budget_period": "monthly",
                         "budget_enforce": "1", "notes": ""})
check(resp.status_code == 422 and "額度上限" in resp.text,
      "勾了「超額擋下來」卻沒填額度 → 看得懂的 422", resp.text[:200])

# 上架表單的完整往返（客戶回饋的第一個痛點就是這條流程）
resp = client.post("/api/v1/admin/web/models", data={
    "upstream": "openrouter", "key_source": "dept", "slug": "meta/llama-4",
    "model_name": "", "display_name": "Llama 4", "model_type": "chat",
    "cost_center": "RD", "budget_limit_usd": "50", "budget_period": "monthly",
    "notes": "表單上架", "preset_name": "OpenRouter 標準",
})
check(resp.status_code == 200 and "已建立草稿" in resp.text, "表單上架成功", resp.text[:400])
formed = model_metadata_service.get_metadata("openrouter/meta/llama-4")
check(formed["status"] == "draft", "表單上架的模型是草稿", formed["status"])
check(formed["display_name"] == "Llama 4" and formed["budget_limit_usd"] == 50.0,
      "表單填的管理面欄位都存進去了", str(formed))
check(formed["budget_enforce"] == 0, "沒勾「超額擋下來」＝只記錄")
presets = model_metadata_service.list_presets()
check(any(p["preset_name"] == "OpenRouter 標準" for p in presets), "常用範本存下來了")
check(all("api_key" not in p["payload"] for p in presets), "範本不含 key")

resp = client.get("/api/v1/admin/web/models/new")
check("OpenRouter 標準" in resp.text, "上架第一步列得出常用範本")
resp = client.get("/api/v1/admin/web/models/new?upstream=openrouter&key_source=dept&preset=OpenRouter+%E6%A8%99%E6%BA%96")
check(resp.status_code == 200 and 'value="meta/llama-4"' in resp.text, "套用範本會帶入上次填的 slug", resp.text[:300])

# curl 路徑的回溯相容：不帶 status 的舊呼叫仍然直接可用（published）
run(models_service.create_external_model(ExternalModelIn(
    model_name="openrouter/legacy-curl", model="openai/legacy", key_policy="dept:openrouter",
)))
check(model_metadata_service.get_metadata("openrouter/legacy-curl")["status"] == "published",
      "curl 路徑不帶 status → published（既有流程行為不變）")
model_access_service.apply_dept("RD", ["openrouter/legacy-curl", "gemma-4-31B-it"],
                                {"openrouter/legacy-curl", "gemma-4-31B-it"})
ok, exc = run(_auth_model("sk-rd-0", "openrouter/legacy-curl"))
check(ok, "curl 上架的模型授權後直接打得通，不用先發布", str(exc))

# 這個功能上線前就存在的模型（沒有 model_metadata 紀錄）一律放行
ok, exc = run(_auth_model("sk-rd-0", "gemma-4-31B-it"))
check(ok, "沒有管理紀錄的既有模型不受狀態閘門影響（零資料回填）", str(exc))

# 稽核裡的 key 欄位要能分辨「這個模型不帶 key」跟「key 被清空了」——記成空字串
# 兩者長得一模一樣，看的人分不出來。
resp = client.post("/api/v1/admin/web/models", data={
    "upstream": "openrouter", "key_source": "shared", "slug": "audit/sharedkey",
    "api_key": "sk-or-v1-secret4321", "model_type": "chat", "budget_period": "monthly",
})
check(resp.status_code == 200, "共用 key 上架成功", resp.text[:200])

by_target = {}
for line in open(os.environ["ADMIN_AUDIT_LOG_PATH"], encoding="utf-8"):
    rec = json.loads(line)
    if rec["action"] == "create_external_model":
        by_target[rec["target"]] = rec["detail"]["after"]

shared = by_target.get("audit/sharedkey", {})
check(shared.get("api_key") == "...4321", "共用 key 的稽核只留末四碼", str(shared.get("api_key")))
check("secret4321" not in json.dumps(shared, ensure_ascii=False), "完整的 key 沒有進稽核紀錄")

dept = by_target.get("openrouter/meta/llama-4", {})
check(dept.get("api_key") == "（無）", "不帶 key 的模型記「（無）」而不是空字串", str(dept.get("api_key")))
check(dept.get("key_policy", "").startswith("dept:"),
      "同一筆紀錄有 key_policy，看得出為什麼是「（無）」", str(dept.get("key_policy")))

# 既有模型（沒有管理紀錄）存一次描述性欄位＝納管；打錯名字要擋下來
resp = client.post("/api/v1/admin/web/models/fields",
                   data={"model_name": "gemma-4-31B-it", "display_name": "Gemma 4 31B",
                         "cost_center": "RD", "budget_limit_usd": "", "budget_period": "monthly",
                         "notes": "地端主力"}, follow_redirects=False)
check(resp.status_code == 303, "既有模型存描述性欄位 → 納管成功", str(resp.status_code))
adopted = model_metadata_service.get_metadata("gemma-4-31B-it")
check(adopted["has_record"] and adopted["status"] == "published",
      "納管後有紀錄且狀態是 published（行為不變）", str(adopted.get("status")))
ok, exc = run(_auth_model("sk-rd-0", "gemma-4-31B-it"))
check(ok, "納管不會讓既有模型突然打不通", str(exc))

resp = client.post("/api/v1/admin/web/models/fields",
                   data={"model_name": "typo-model", "display_name": "x", "cost_center": "",
                         "budget_limit_usd": "", "budget_period": "monthly", "notes": ""})
check(resp.status_code == 404, "打錯 model_name 不會留下孤兒設定 → 404", str(resp.status_code))
check(not model_metadata_service.get_metadata("typo-model")["has_record"], "確實沒有建立紀錄")

resp = client.post("/api/v1/admin/web/models/hard-delete", data={"model_name": NAME})
check(resp.status_code == 422, "永久刪除沒勾確認 → 422", str(resp.status_code))
resp = client.post("/api/v1/admin/web/models/hard-delete", data={"model_name": NAME, "confirm": "on"})
check(resp.status_code == 200 and "已永久刪除" in resp.text, "永久刪除成功", resp.text[:300])
check(not model_metadata_service.get_metadata(NAME)["has_record"], "刪除後管理紀錄也清掉了")
check(model_metadata_service.get_spend(NAME)["total"] > 0, "用量累計刻意保留（同名重上架不會歸零）")

app.dependency_overrides.clear()

print(f"\n{BOLD}結果：{GREEN}{PASS} passed{NC}, "
      f"{RED if FAIL else GREEN}{FAIL} failed{NC}   （暫存目錄 {_TMP}）")
sys.exit(1 if FAIL else 0)
