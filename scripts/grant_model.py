#!/usr/bin/env python3
"""
grant_model.py — 快速把一個 model 開放（或收回）給全部部門或指定部門

只動 departments.allowed_models（部門層級授權）；個別使用者的 models 欄位
（/api/v1/users）不受影響。effective 權限 = dept.allowed_models ∪ user.models
（見 config/custom_auth.py _effective_models），所以幫部門開通後，該部門所有
使用者立即可用（custom_auth 30 秒 TTL 快取過期後自動生效，不需重啟任何服務）。

用法：
  ADMIN_API_KEY=xxx ./scripts/grant_model.py gemma-4-31B-it --all
  ADMIN_API_KEY=xxx ./scripts/grant_model.py gemma-4-31B-it --dept PM RD
  ADMIN_API_KEY=xxx ./scripts/grant_model.py gemma-4-31B-it --all --revoke
  ADMIN_API_KEY=xxx ./scripts/grant_model.py gemma-4-31B-it --all --dry-run
  ADMIN_API_KEY=xxx ./scripts/grant_model.py gemma-4-31B-it --dept RD --admin-host localhost:8080
"""

import argparse
import json
import os
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


def main() -> int:
    parser = argparse.ArgumentParser(description="快速開放/收回一個 model 給部門")
    parser.add_argument("model_id", help="LiteLLM model_name，例如 gemma-4-31B-it")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="套用到全部部門")
    target.add_argument("--dept", nargs="+", metavar="DEPT_ID", help="只套用到指定部門（可多個）")
    parser.add_argument("--revoke", action="store_true", help="從 allowed_models 移除，預設是新增")
    parser.add_argument("--admin-host", default="", help="admin-api host:port（預設自動偵測 K8s NodeIP）")
    parser.add_argument("--admin-key", default="", help="預設讀 ADMIN_API_KEY 環境變數")
    parser.add_argument("--dry-run", action="store_true", help="只列出會變更的部門，不實際送出")
    parser.add_argument("-y", "--yes", action="store_true", help="套用 --all 時跳過確認提示")
    args = parser.parse_args()

    load_env(Path(__file__).resolve().parent.parent / ".env")
    admin_key = args.admin_key or os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        sys.exit(f"{RED}錯誤：請設定 ADMIN_API_KEY 環境變數或 --admin-key{NC}")

    admin_host = args.admin_host or autodetect_admin_host()
    admin_url = f"http://{admin_host}"
    print(f"{CYAN}Admin API: {admin_url}{NC}")

    # 順手核對 model_id 是否真的存在於 LiteLLM，打錯字時提醒但不擋（admin-api/litellm
    # 可能暫時連不上，仍允許使用者強行送出）
    status, body = request("GET", f"{admin_url}/api/v1/models", admin_key)
    if status == 200 and args.model_id not in body.get("models", []):
        known = ", ".join(body.get("models", [])) or "(空)"
        print(f"{YELLOW}警告：'{args.model_id}' 不在 LiteLLM 目前的 model 清單中，"
              f"確定沒打錯字嗎？（目前清單：{known}）{NC}")

    status, depts = request("GET", f"{admin_url}/api/v1/departments", admin_key)
    if status != 200:
        sys.exit(f"{RED}讀取部門清單失敗：HTTP {status} {depts}{NC}")

    if args.dept:
        wanted = set(args.dept)
        missing = wanted - {d["dept_id"] for d in depts}
        if missing:
            sys.exit(f"{RED}找不到部門：{', '.join(sorted(missing))}{NC}")
        targets = [d for d in depts if d["dept_id"] in wanted]
    else:
        targets = depts

    verb = "收回" if args.revoke else "開放"
    changes = []
    for d in targets:
        current = d["allowed_models"]
        has_wildcard = "*" in current
        already = args.model_id in current
        if args.revoke:
            if not already:
                continue
            new_list = [m for m in current if m != args.model_id]
        else:
            if already or has_wildcard:
                continue
            new_list = [*current, args.model_id]
        changes.append((d["dept_id"], current, new_list, has_wildcard))

    if not changes:
        print(f"{GREEN}沒有部門需要變更 —— 全部目標部門的 '{args.model_id}' 狀態已符合預期。{NC}")
        return 0

    print(f"\n即將對以下部門{verb} '{args.model_id}'：")
    for dept_id, current, new_list, has_wildcard in changes:
        note = ""
        if args.revoke and has_wildcard:
            note = f"  {YELLOW}(注意：此部門有 '*' 全開放，收回單一 model 不會限制其存取){NC}"
        print(f"  {dept_id}: {current} → {new_list}{note}")

    if args.dry_run:
        print(f"\n{YELLOW}--dry-run：以上變更尚未送出{NC}")
        return 0

    if args.all and not args.yes:
        ans = input(f"\n{YELLOW}確定要套用到以上 {len(changes)} 個部門？[y/N] {NC}").strip().lower()
        if ans != "y":
            print("已取消")
            return 1

    fail_count = 0
    for dept_id, _current, new_list, _hw in changes:
        status, body = request(
            "PATCH", f"{admin_url}/api/v1/departments/{dept_id}", admin_key,
            {"allowed_models": new_list},
        )
        if status != 200:
            print(f"{RED}[{dept_id}] 失敗：HTTP {status} {body}{NC}")
            fail_count += 1
            continue
        print(f"{GREEN}[{dept_id}] 已更新{NC}")

    print(f"\n{CYAN}custom_auth 的 SQLite 快取 TTL 是 30 秒，之後所有請求會自動套用新權限"
          f"（不需重啟 litellm 或 admin-api）。{NC}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
