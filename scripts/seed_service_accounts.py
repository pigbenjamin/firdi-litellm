#!/usr/bin/env python3
"""
seed_service_accounts.py — 收斂 config/service_accounts.json 定義的固定服務帳號

服務帳號（account_type=service）不像人類帳號有 Keycloak webhook 自動同步，之前都是
「當下手動對某台叢集的 admin-api 打 curl」建立，只活在那台機器的 SQLite 裡，git 完全
沒有紀錄——換一台新機器或重灌 users-db-pvc，這些帳號會全部消失且無感。這支腳本把「這個
平台必須有哪些服務帳號、各自能用哪些模型/多少額度」變成 git 追蹤的宣告式清單，新機器/
既有機器都跑同一支就能收斂到一致狀態。

冪等規則：
  - 帳號不存在 → 建立，隨機產生新 api_key（僅顯示一次，需自行存進 Secret 管理系統）
  - 帳號已存在 → 只 PATCH key_name/user_email/dept_id/models/rpm_limit/tpm_limit/metadata
    這些「設定」欄位對齊 JSON；**絕不碰 api_key**，避免重跑就讓所有消費端的 key 一起失效。
    也不碰 blocked——封鎖/解封是操作動作（見 docs/admin-api.md），不是宣告式設定的一部分。
  - dept_id 若不存在會自動建立一個空部門（allowed_models=[]，SERVICE 特別給預設 dept_name）

用法：
  ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py
  ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py --dry-run
  ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py --only svc-chat-summarizer
  ADMIN_API_KEY=xxx ./scripts/seed_service_accounts.py --admin-host localhost:8080
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

_MANAGED_FIELDS = ["key_name", "user_email", "dept_id", "models", "rpm_limit", "tpm_limit", "metadata"]
_DEFAULT_DEPT_NAMES = {"SERVICE": "Service Accounts"}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def request(method: str, url: str, key: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def autodetect_admin_host() -> str:
    try:
        out = subprocess.run(
            ["kubectl", "get", "nodes", "-o",
             "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"],
            capture_output=True, text=True, timeout=10,
        )
        node_ip = out.stdout.strip()
    except Exception:
        node_ip = ""
    return f"{node_ip}:30408" if node_ip else "localhost:8080"


def ensure_department(admin_url: str, admin_key: str, dept_id: str, dry_run: bool) -> bool:
    """部門不存在就建立一個空部門。回傳是否有變更（供統計）。"""
    status, _ = request("GET", f"{admin_url}/api/v1/departments/{dept_id}", admin_key)
    if status == 200:
        return False

    dept_name = _DEFAULT_DEPT_NAMES.get(dept_id, dept_id)
    print(f"{YELLOW}部門 '{dept_id}' 不存在{'（--dry-run，不會建立）' if dry_run else ''}，"
          f"將以 dept_name='{dept_name}'、allowed_models=[] 建立{NC}")
    if dry_run:
        return True

    status, body = request(
        "POST", f"{admin_url}/api/v1/departments", admin_key,
        {"dept_id": dept_id, "dept_name": dept_name},
    )
    if status != 201:
        print(f"{RED}建立部門 '{dept_id}' 失敗：HTTP {status} {body}{NC}")
        sys.exit(1)
    print(f"{GREEN}[dept:{dept_id}] 已建立{NC}")
    return True


def diff_fields(current: dict, desired: dict) -> dict:
    changed = {}
    for field in _MANAGED_FIELDS:
        if field not in desired:
            continue
        if current.get(field) != desired[field]:
            changed[field] = desired[field]
    return changed


def sync_account(admin_url: str, admin_key: str, entry: dict, dry_run: bool) -> bool:
    """回傳是否有變更（建立或 PATCH）。"""
    user_id = entry["user_id"]
    desired = {
        "key_name": entry.get("key_name", user_id),
        "user_email": entry.get("user_email"),
        "dept_id": entry["dept_id"],
        "models": entry.get("models", []),
        "rpm_limit": entry.get("rpm_limit"),
        "tpm_limit": entry.get("tpm_limit"),
        "metadata": entry.get("metadata", {}),
    }

    status, current = request("GET", f"{admin_url}/api/v1/users/{user_id}", admin_key)

    if status == 404:
        new_key = f"sk-{user_id}-{secrets.token_hex(16)}"
        print(f"\n{YELLOW}[{user_id}] 不存在{'（--dry-run，不會建立）' if dry_run else '，將建立新帳號'}{NC}")
        if dry_run:
            return True
        body = {
            "api_key": new_key,
            "user_id": user_id,
            "account_type": "service",
            "aliases": {},
            "blocked": False,
            **desired,
        }
        status, resp = request("POST", f"{admin_url}/api/v1/users", admin_key, body)
        if status != 201:
            print(f"{RED}[{user_id}] 建立失敗：HTTP {status} {resp}{NC}")
            return False
        print(f"{GREEN}[{user_id}] 已建立{NC}")
        print(f"{CYAN}  api_key（僅顯示這一次，請立刻存進該服務的 Secret 管理系統）：{NC}")
        print(f"  {new_key}")
        return True

    if status != 200:
        print(f"{RED}[{user_id}] 查詢失敗：HTTP {status} {current}{NC}")
        return False

    changed = diff_fields(current, desired)
    if not changed:
        print(f"{GREEN}[{user_id}] 已存在且設定一致，略過{NC}")
        return False

    print(f"\n{YELLOW}[{user_id}] 設定有差異{'（--dry-run，不會套用）' if dry_run else ''}：{NC}")
    for field, new_val in changed.items():
        print(f"  {field}: {current.get(field)!r} → {new_val!r}")
    if dry_run:
        return True

    status, resp = request("PATCH", f"{admin_url}/api/v1/users/{user_id}", admin_key, changed)
    if status != 200:
        print(f"{RED}[{user_id}] 更新失敗：HTTP {status} {resp}{NC}")
        return False
    print(f"{GREEN}[{user_id}] 已更新{NC}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="收斂 config/service_accounts.json 定義的固定服務帳號")
    parser.add_argument("--json", default="", help="服務帳號清單路徑（預設 config/service_accounts.json）")
    parser.add_argument("--only", nargs="+", metavar="USER_ID", help="只處理指定的 user_id（可多個）")
    parser.add_argument("--admin-host", default="", help="admin-api host:port（預設自動偵測 K8s NodeIP）")
    parser.add_argument("--admin-key", default="", help="預設讀 ADMIN_API_KEY 環境變數")
    parser.add_argument("--dry-run", action="store_true", help="只列出會變更的帳號，不實際送出")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    load_env(repo_root / ".env")
    admin_key = args.admin_key or os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        sys.exit(f"{RED}錯誤：請設定 ADMIN_API_KEY 環境變數或 --admin-key{NC}")

    admin_host = args.admin_host or autodetect_admin_host()
    admin_url = f"http://{admin_host}"
    print(f"{CYAN}Admin API: {admin_url}{NC}")

    json_path = Path(args.json) if args.json else repo_root / "config" / "service_accounts.json"
    if not json_path.exists():
        sys.exit(f"{RED}錯誤：找不到 {json_path}{NC}")
    entries = json.loads(json_path.read_text()).get("service_accounts", [])

    if args.only:
        wanted = set(args.only)
        missing = wanted - {e["user_id"] for e in entries}
        if missing:
            sys.exit(f"{RED}清單裡沒有這些 user_id：{', '.join(sorted(missing))}{NC}")
        entries = [e for e in entries if e["user_id"] in wanted]

    if args.dry_run:
        print(f"{YELLOW}--dry-run：以下僅預覽，不會實際送出{NC}")

    dept_ids = dict.fromkeys(e["dept_id"] for e in entries)  # 保序去重
    any_change = False
    for dept_id in dept_ids:
        any_change |= ensure_department(admin_url, admin_key, dept_id, args.dry_run)

    fail_count = 0
    for entry in entries:
        try:
            any_change |= sync_account(admin_url, admin_key, entry, args.dry_run)
        except SystemExit:
            raise
        except Exception as e:
            print(f"{RED}[{entry.get('user_id')}] 發生例外：{e}{NC}")
            fail_count += 1

    print()
    if not any_change:
        print(f"{GREEN}所有服務帳號皆已符合 {json_path.name} 定義的狀態，無需變更。{NC}")
    elif args.dry_run:
        print(f"{YELLOW}--dry-run 完成，上述變更尚未套用；移除 --dry-run 重跑即可實際套用。{NC}")
    else:
        print(f"{CYAN}custom_auth 的 SQLite 快取 TTL 是 30 秒，新設定會在 30 秒內對所有請求生效"
              f"（不需重啟 litellm 或 admin-api）。{NC}")

    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
