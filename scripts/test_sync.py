#!/usr/bin/env python3
"""
test_sync.py — Keycloak → admin-api 使用者同步整合測試

T1  Keycloak Client Credentials 連線
T2  Webhook Secret 驗證（拒絕未授權請求）
T3  缺少 user_id 的錯誤處理
T4  部門不存在時略過（skipped）
T5  新使用者 Provision（自動產生 api_key、繼承部門 model）
T6  更新現有使用者（api_key 不變、資料更新）
T7  DELETE 事件封鎖使用者（blocked=true，資料保留）

用法：
  python3 scripts/test_sync.py
  python3 scripts/test_sync.py --admin-host localhost:8080
  python3 scripts/test_sync.py --suite unit
  python3 scripts/test_sync.py --suite provision --user-id <UUID>
  python3 scripts/test_sync.py --no-cleanup
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── 顏色 ──────────────────────────────────────────────────────────────────────
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"


@dataclass
class HttpResult:
    status: int
    body: str


class TestRunner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def section(self, title: str) -> None:
        print(f"\n{CYAN}{BOLD}── {title} ──────────────────────────────────{NC}")

    def pass_(self, label: str) -> None:
        print(f"{GREEN}  ✓ PASS{NC} {label}")
        self.passed += 1

    def fail(self, label: str, detail: str = "") -> None:
        print(f"{RED}  ✗ FAIL{NC} {label}")
        if detail:
            print(f"{YELLOW}    → {detail}{NC}")
        self.failed += 1

    def skip(self, label: str) -> None:
        print(f"{YELLOW}  ⊘ SKIP{NC} {label}")
        self.skipped += 1

    def info(self, msg: str) -> None:
        print(f"{YELLOW}  →{NC} {msg}")

    def summary(self) -> int:
        print(f"\n{BOLD}────────────────────────────────────────{NC}")
        print(f"  {GREEN}PASS: {self.passed}{NC}   {RED}FAIL: {self.failed}{NC}   {YELLOW}SKIP: {self.skipped}{NC}")
        print(f"{BOLD}────────────────────────────────────────{NC}\n")
        return 0 if self.failed == 0 else 1


# ── .env 讀取 ─────────────────────────────────────────────────────────────────
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


# ── HTTP 工具 ──────────────────────────────────────────────────────────────────
def _ssl_ctx(verify: bool | str) -> ssl.SSLContext | None:
    if verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(verify, str) and verify not in ("true", "1", "yes"):
        ctx = ssl.create_default_context(cafile=verify)
        return ctx
    return None


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | bytes | None = None,
    form: dict[str, str] | None = None,
    ssl_ctx: ssl.SSLContext | None = None,
    timeout: int = 10,
) -> HttpResult:
    data: bytes | None = None
    hdrs: dict[str, str] = headers or {}

    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(body, dict):
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(body, bytes):
        data = body

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
            return HttpResult(status=r.status, body=r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return HttpResult(status=e.code, body=e.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return HttpResult(status=0, body=str(e))


def json_body(r: HttpResult) -> dict:
    try:
        return json.loads(r.body)
    except Exception:
        return {}


# ── Keycloak 工具 ─────────────────────────────────────────────────────────────
class KeycloakClient:
    def __init__(self, url: str, realm: str, client_id: str, client_secret: str, ssl_ctx: ssl.SSLContext | None) -> None:
        self.url = url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.ssl_ctx = ssl_ctx
        self._token: str = ""

    def get_token(self) -> str | None:
        r = http(
            "POST",
            f"{self.url}/realms/{self.realm}/protocol/openid-connect/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            ssl_ctx=self.ssl_ctx,
        )
        if r.status == 200:
            self._token = json_body(r).get("access_token", "")
            return self._token
        return None

    def list_users(self, max_count: int = 20) -> list[dict]:
        if not self._token:
            return []
        r = http(
            "GET",
            f"{self.url}/admin/realms/{self.realm}/users?max={max_count}",
            headers={"Authorization": f"Bearer {self._token}"},
            ssl_ctx=self.ssl_ctx,
        )
        if r.status == 200:
            return json_body(r) if isinstance(json_body(r), list) else []
        return []

    def get_user(self, user_id: str) -> dict | None:
        if not self._token:
            return None
        r = http(
            "GET",
            f"{self.url}/admin/realms/{self.realm}/users/{user_id}",
            headers={"Authorization": f"Bearer {self._token}"},
            ssl_ctx=self.ssl_ctx,
        )
        return json_body(r) if r.status == 200 else None

    def get_user_groups(self, user_id: str) -> list[str]:
        """取得使用者的頂層 group 名稱列表（對應 dept_id）。"""
        if not self._token:
            return []
        r = http(
            "GET",
            f"{self.url}/admin/realms/{self.realm}/users/{user_id}/groups",
            headers={"Authorization": f"Bearer {self._token}"},
            ssl_ctx=self.ssl_ctx,
        )
        if r.status != 200:
            return []
        groups = json_body(r)
        if not isinstance(groups, list):
            return []
        result = []
        for g in groups:
            parts = [p for p in g.get("path", "").split("/") if p]
            if parts:
                result.append(parts[0])
        return result


# ── admin-api 工具 ────────────────────────────────────────────────────────────
class AdminClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def get(self, path: str) -> HttpResult:
        return http("GET", f"{self.base}{path}", headers=self._auth())

    def post(self, path: str, body: dict) -> HttpResult:
        return http("POST", f"{self.base}{path}", headers=self._auth(), body=body)

    def delete(self, path: str) -> HttpResult:
        return http("DELETE", f"{self.base}{path}", headers=self._auth())

    def sync(self, webhook_secret: str, payload: dict) -> HttpResult:
        return http(
            "POST",
            f"{self.base}/api/v1/sync/keycloak",
            headers={"X-Webhook-Secret": webhook_secret, "Content-Type": "application/json"},
            body=payload,
        )


# ── 測試主體 ──────────────────────────────────────────────────────────────────
def run_tests(
    t: TestRunner,
    admin: AdminClient,
    kc: KeycloakClient,
    webhook_secret: str,
    suite: str,
    user_id: str,
    do_cleanup: bool,
) -> None:
    # ── T1：Keycloak Client Credentials 連線 ──────────────────────────────────
    if suite in ("all", "unit", "keycloak"):
        t.section("T1  Keycloak Client Credentials 連線")
        if not kc.client_secret:
            t.skip("T1 — 未設定 KEYCLOAK_CLIENT_SECRET")
        else:
            token = kc.get_token()
            if token:
                t.pass_(f"T1-1 取得 access_token（開頭：{token[:20]}...）")
            else:
                t.fail("T1-1 無法取得 access_token", "請確認 client_id / client_secret 及 Keycloak 連線")
                return

    # ── T2：Webhook Secret 驗證 ────────────────────────────────────────────────
    if suite in ("all", "unit"):
        t.section("T2  Webhook Secret 驗證")

        r = http("POST", f"{admin.base}/api/v1/sync/keycloak",
                 body={"user_id": "test", "event_type": "UPDATE"})
        if r.status == 401:
            t.pass_("T2-1 無 secret → 401")
        else:
            t.fail("T2-1 無 secret 應回 401", f"實際 {r.status}: {r.body[:100]}")

        r = admin.sync("wrong-secret", {"user_id": "test", "event_type": "UPDATE"})
        if r.status == 401:
            t.pass_("T2-2 錯誤 secret → 401")
        else:
            t.fail("T2-2 錯誤 secret 應回 401", f"實際 {r.status}")

        r = admin.sync(webhook_secret, {"user_id": "00000000-0000-0000-0000-000000000000", "event_type": "UPDATE"})
        if r.status in (200, 502):
            t.pass_(f"T2-3 正確 secret 通過驗證（HTTP {r.status}）")
        else:
            t.fail("T2-3 正確 secret 應回 200 或 502", f"實際 {r.status}")

    # ── T3：缺少 user_id 的錯誤處理 ───────────────────────────────────────────
    if suite in ("all", "unit"):
        t.section("T3  缺少 user_id 的錯誤處理")

        r = admin.sync(webhook_secret, {"event_type": "UPDATE"})
        if r.status == 422:
            t.pass_("T3-1 缺少 user_id → 422")
        else:
            t.fail("T3-1 缺少 user_id 應回 422", f"實際 {r.status}")

        r = admin.sync(webhook_secret, {"user_id": "", "event_type": "UPDATE"})
        if r.status == 422:
            t.pass_("T3-2 空字串 user_id → 422")
        else:
            t.fail("T3-2 空字串 user_id 應回 422", f"實際 {r.status}")

    # ── T4-T7：需要真實 Keycloak user UUID ───────────────────────────────────
    if suite not in ("all", "provision"):
        return

    # 自動取得 UUID
    resolved_uid = user_id
    if not resolved_uid:
        t.section("自動取得 Keycloak 使用者 UUID")
        if not kc._token and not kc.get_token():
            t.skip("T4-T7 — 無法取得 Keycloak token，略過")
            return
        users = kc.list_users(max_count=20)
        if not users:
            t.skip("T4-T7 — Keycloak 中無使用者，略過")
            return
        # 印出可用使用者清單
        print(f"\n  {'UUID':<38} {'username':<20} email")
        print(f"  {'-'*38} {'-'*20} {'-'*30}")
        for u in users:
            print(f"  {u.get('id',''):<38} {u.get('username',''):<20} {u.get('email','')}")
        resolved_uid = users[0]["id"]
        t.info(f"自動選用第一個使用者：{resolved_uid}")

    # ── T4：部門不存在時略過 ──────────────────────────────────────────────────
    t.section("T4  部門不存在時略過")
    r = admin.sync(webhook_secret, {"user_id": resolved_uid, "event_type": "UPDATE", "source": "admin_event"})
    d = json_body(r)
    if r.status == 200:
        status = d.get("status", "")
        if status == "skipped":
            t.pass_(f"T4-1 部門不存在 → skipped（{d.get('reason','')}）")
        elif status in ("created", "updated"):
            t.info(f"T4-1 使用者已同步（status={status}），部門已存在，略過 T4 驗證")
        else:
            t.fail("T4-1 非預期 status", f"{d}")
    else:
        t.fail("T4-1 應回 HTTP 200", f"實際 {r.status}: {r.body[:150]}")

    # 取得使用者在 Keycloak 的實際 group → 用作測試部門 ID
    user_groups = kc.get_user_groups(resolved_uid)
    if user_groups:
        test_dept_id = user_groups[0]
        t.info(f"使用者 KC group：{user_groups}，測試部門 ID：'{test_dept_id}'")
    else:
        test_dept_id = "test-sync-dept-tmp"
        t.info(f"無法取得 KC group，使用預設部門 ID：'{test_dept_id}'")

    # 建立測試部門（若已存在則不在 cleanup 時刪除）
    dept_created = False
    existing_dept = admin.get(f"/api/v1/departments/{test_dept_id}")
    if existing_dept.status == 200:
        t.info(f"部門已存在：{test_dept_id}（測試後不清除）")
    else:
        r = admin.post("/api/v1/departments", {
            "dept_id": test_dept_id,
            "dept_name": f"Sync Test ({test_dept_id})",
            "allowed_models": ["fast-qwen"],
            "dept_rpm_limit": 60,
            "dept_tpm_limit": 100000,
        })
        if r.status == 201:
            dept_created = True
            t.info(f"建立測試部門：{test_dept_id}")
        else:
            t.info(f"警告：無法建立測試部門（{r.status}），T5/T6/T7 可能受影響")

    # ── T5：新使用者 Provision ────────────────────────────────────────────────
    t.section("T5  新使用者 Provision")

    # 清除既有使用者以確保 created 路徑
    existing = admin.get(f"/api/v1/users/{resolved_uid}")
    original_api_key = ""
    if existing.status == 200:
        original_api_key = json_body(existing).get("api_key", "")
        admin.delete(f"/api/v1/users/{resolved_uid}")
        t.info("清除既有使用者，確保走 create 路徑")

    r = admin.sync(webhook_secret, {"user_id": resolved_uid, "event_type": "CREATE", "source": "admin_event"})
    d = json_body(r)
    if r.status != 200:
        t.fail("T5 — HTTP 200 預期", f"實際 {r.status}: {r.body[:150]}")
    elif d.get("status") == "created":
        t.pass_("T5-1 新使用者建立成功")
        api_key = d.get("api_key", "")
        if api_key.startswith("sk-"):
            t.pass_(f"T5-2 api_key 自動產生（{api_key[:12]}...）")
        else:
            t.fail("T5-2 api_key 格式不符", f"實際：{api_key}")

        # 驗證 SQLite 內容
        check = admin.get(f"/api/v1/users/{resolved_uid}")
        if check.status == 200:
            u = json_body(check)
            t.pass_("T5-3 使用者已存入 SQLite")
            t.info(f"models: {u.get('models')}")
            t.info(f"dept_id: {u.get('dept_id')}")
            t.info(f"email: {u.get('user_email')}")
        else:
            t.fail("T5-3 查詢使用者失敗", f"HTTP {check.status}")
    elif d.get("status") == "skipped":
        t.skip(f"T5 — {d.get('reason')}")
    else:
        t.fail("T5 — 非預期 status", f"{d}")

    # ── T6：更新現有使用者 ────────────────────────────────────────────────────
    t.section("T6  更新現有使用者（api_key 不變）")

    before = admin.get(f"/api/v1/users/{resolved_uid}")
    key_before = json_body(before).get("api_key", "") if before.status == 200 else ""

    r = admin.sync(webhook_secret, {"user_id": resolved_uid, "event_type": "UPDATE", "source": "admin_event"})
    d = json_body(r)
    if r.status == 200 and d.get("status") == "updated":
        t.pass_("T6-1 再次 sync → updated")
    else:
        t.fail("T6-1 應回 status=updated", f"HTTP {r.status}: {d}")

    after = admin.get(f"/api/v1/users/{resolved_uid}")
    key_after = json_body(after).get("api_key", "") if after.status == 200 else ""
    if key_before and key_before == key_after:
        t.pass_("T6-2 api_key 未改變")
    elif not key_before:
        t.info("T6-2 無法驗證 api_key（T5 未建立使用者）")
    else:
        t.fail("T6-2 api_key 不應改變", f"前：{key_before}  後：{key_after}")

    # ── T7：DELETE 事件封鎖使用者 ─────────────────────────────────────────────
    t.section("T7  DELETE 事件封鎖使用者")

    r = admin.sync(webhook_secret, {"user_id": resolved_uid, "event_type": "DELETE", "source": "admin_event"})
    d = json_body(r)
    if r.status == 200 and d.get("status") == "blocked":
        t.pass_("T7-1 DELETE 事件 → blocked")
    else:
        t.fail("T7-1 應回 status=blocked", f"HTTP {r.status}: {d}")

    check = admin.get(f"/api/v1/users/{resolved_uid}")
    if check.status == 200:
        t.pass_("T7-2 使用者仍存在（軟刪除）")
        if json_body(check).get("blocked") is True:
            t.pass_("T7-3 blocked=true")
        else:
            t.fail("T7-3 blocked 應為 true", f"實際：{json_body(check).get('blocked')}")
    else:
        t.fail("T7-2 使用者不應被移除", f"HTTP {check.status}")

    # ── 清理 ──────────────────────────────────────────────────────────────────
    if do_cleanup:
        admin.delete(f"/api/v1/users/{resolved_uid}")
        t.info(f"清理測試使用者：{resolved_uid}")
        if dept_created:
            admin.delete(f"/api/v1/departments/{test_dept_id}")
            t.info(f"清理測試部門：{test_dept_id}")


# ── 主程式 ────────────────────────────────────────────────────────────────────
def sync_all_users(admin: "AdminClient", kc: "KeycloakClient", webhook_secret: str) -> None:
    """將 Keycloak 所有使用者同步到 admin-api。"""
    print(f"\n{BOLD}同步所有 Keycloak 使用者 → admin-api{NC}")

    if not kc.get_token():
        print(f"{RED}無法取得 Keycloak token，中止{NC}")
        return

    users = kc.list_users(max_count=500)
    if not users:
        print(f"{YELLOW}Keycloak 中無使用者{NC}")
        return

    print(f"  找到 {len(users)} 個使用者\n")
    results: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for u in users:
        uid = u.get("id", "")
        username = u.get("username", "")
        r = admin.sync(webhook_secret, {"user_id": uid, "event_type": "UPDATE", "source": "admin_event"})
        d = json_body(r)
        status = d.get("status", "error") if r.status == 200 else "error"
        results[status] = results.get(status, 0) + 1
        color = GREEN if status in ("created", "updated") else YELLOW if status == "skipped" else RED
        print(f"  {color}{status:<8}{NC} {username} ({uid[:8]}...)")
        if status == "error":
            print(f"    {YELLOW}→ HTTP {r.status}: {r.body[:100]}{NC}")

    print(f"\n{BOLD}結果：{NC} created={results.get('created',0)}  updated={results.get('updated',0)}"
          f"  skipped={results.get('skipped',0)}  error={results.get('error',0)}")


def reconcile_deleted_users(admin: "AdminClient", kc: "KeycloakClient", webhook_secret: str) -> None:
    """完整對帳：確認 Keycloak 與 admin-api DB 一致。
    1. KC 有的使用者 → upsert 到 DB（新增或更新）
    2. KC 已刪除但 DB 仍存在的使用者 → blocked
    """
    print(f"\n{BOLD}對帳：同步 Keycloak ↔ admin-api DB{NC}")

    if not kc.get_token():
        print(f"{RED}無法取得 Keycloak token，中止{NC}")
        return

    kc_users = kc.list_users(max_count=500)
    kc_ids = {u["id"] for u in kc_users}

    r = admin.get("/api/v1/users")
    if r.status != 200:
        print(f"{RED}無法取得 admin-api 使用者清單（HTTP {r.status}）{NC}")
        return
    api_users = json_body(r)
    if not isinstance(api_users, list):
        api_users = []

    print(f"  Keycloak：{len(kc_ids)} 個使用者  /  admin-api DB：{len(api_users)} 個使用者\n")

    # Step 1: KC → DB upsert
    print(f"{CYAN}{BOLD}── Step 1  KC → DB（新增 / 更新）{NC}")
    results: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
    for u in kc_users:
        uid = u.get("id", "")
        username = u.get("username", "")
        r2 = admin.sync(webhook_secret, {"user_id": uid, "event_type": "UPDATE", "source": "admin_event"})
        d = json_body(r2)
        status = d.get("status", "error") if r2.status == 200 else "error"
        results[status] = results.get(status, 0) + 1
        color = GREEN if status in ("created", "updated") else YELLOW if status == "skipped" else RED
        print(f"  {color}{status:<8}{NC} {username} ({uid[:8]}...)")

    print(f"\n  created={results.get('created',0)}  updated={results.get('updated',0)}"
          f"  skipped={results.get('skipped',0)}  error={results.get('error',0)}")

    # Step 2: DB 有但 KC 沒有 → blocked
    print(f"\n{CYAN}{BOLD}── Step 2  DB → blocked（KC 已刪除）{NC}")
    orphans = [u for u in api_users if u.get("user_id") and u["user_id"] not in kc_ids]
    if not orphans:
        print(f"  {GREEN}沒有孤立使用者{NC}")
        return
    for u in orphans:
        uid = u["user_id"]
        username = u.get("key_name", "")
        if u.get("blocked"):
            print(f"  {YELLOW}已封鎖{NC}  {username} ({uid[:8]}...)  （跳過）")
            continue
        r2 = admin.sync(webhook_secret, {"user_id": uid, "event_type": "DELETE", "source": "admin_event"})
        d = json_body(r2)
        if r2.status == 200 and d.get("status") == "blocked":
            print(f"  {GREEN}blocked{NC}  {username} ({uid[:8]}...)")
        else:
            print(f"  {RED}FAIL{NC}    {username} ({uid[:8]}...)  HTTP {r2.status}: {r2.body[:80]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Keycloak → admin-api sync 整合測試")
    parser.add_argument("--admin-host", default="", help="admin-api host:port（預設自動偵測）")
    parser.add_argument("--suite", default="all", choices=["all", "unit", "keycloak", "provision"],
                        help="all=全部, unit=T1-T3, keycloak=T1, provision=T4-T7")
    parser.add_argument("--user-id", default="", help="Keycloak user UUID（省略時自動取第一個使用者）")
    parser.add_argument("--no-cleanup", action="store_true", help="測試後保留建立的資料")
    parser.add_argument("--sync-all", action="store_true", help="將所有 Keycloak 使用者同步到 admin-api")
    parser.add_argument("--reconcile", action="store_true",
                        help="偵測 Keycloak 已刪除但 admin-api 仍存在的使用者，標記 blocked")
    args = parser.parse_args()

    # 讀取 .env（優先用 script 所在目錄往上找，fallback 用 cwd）
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        env_path = Path.cwd() / ".env"
    load_env(env_path)

    webhook_secret = os.getenv("WEBHOOK_SECRET", "")
    keycloak_url = os.getenv("KEYCLOAK_URL", "").rstrip("/")
    keycloak_realm = os.getenv("KEYCLOAK_REALM", "")
    keycloak_client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
    keycloak_client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    admin_api_key = os.getenv("ADMIN_API_KEY", "")

    ssl_verify_raw = os.getenv("KEYCLOAK_SSL_VERIFY", "true").lower()
    ssl_verify: bool | str = (
        False if ssl_verify_raw == "false"
        else ssl_verify_raw if ssl_verify_raw not in ("true", "1", "yes")
        else True
    )
    ssl_ctx = _ssl_ctx(ssl_verify)

    # 自動偵測 admin-api host
    admin_host = args.admin_host
    if not admin_host:
        try:
            import subprocess
            node_ip = subprocess.check_output(
                ["kubectl", "get", "nodes", "-o",
                 "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            admin_host = f"{node_ip}:30408" if node_ip else "localhost:8080"
        except Exception:
            admin_host = "localhost:8080"

    admin_url = f"http://{admin_host}"
    print(f"\n{BOLD}Keycloak → admin-api 使用者同步測試{NC}")
    print(f"  Admin API      : {admin_url}")
    print(f"  Suite          : {args.suite}")
    print(f"  SSL verify     : {ssl_verify}")
    print(f"  .env path      : {env_path}")
    print(f"  WEBHOOK_SECRET : {'(set)' if webhook_secret else '(empty!) — 請檢查 .env'}")
    print(f"  KC client_id   : {keycloak_client_id or '(empty!)'}")
    if args.user_id:
        print(f"  User ID        : {args.user_id}")

    t = TestRunner()
    admin = AdminClient(admin_url, admin_api_key)
    kc = KeycloakClient(keycloak_url, keycloak_realm, keycloak_client_id, keycloak_client_secret, ssl_ctx)

    if args.sync_all:
        sync_all_users(admin, kc, webhook_secret)
        return

    if args.reconcile:
        reconcile_deleted_users(admin, kc, webhook_secret)
        return

    run_tests(t, admin, kc, webhook_secret, args.suite, args.user_id, not args.no_cleanup)

    sys.exit(t.summary())


if __name__ == "__main__":
    main()
