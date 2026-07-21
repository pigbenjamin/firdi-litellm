#!/usr/bin/env python3
"""
把 Function 匯出檔（如 thinking_mode.json）套用到 OpenWebUI：已存在就 update 內容，
不存在就 create 並補開 is_active/is_global（create 預設兩者皆 False，OpenWebUI 不會自動開）。
不使用 /api/v1/functions/sync：那個端點是全量鏡射，沒在清單裡的既有 Function 會被直接刪除。
"""
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WEBUI_URL = os.environ["WEBUI_URL"].rstrip("/")
ADMIN_KEY = os.environ["OPENWEBUI_ADMIN_KEY"]
# 預設掃這個腳本所在目錄下的所有 *.json：本機執行時就是 openwebui/functions/，
# K8s Job 裡 ConfigMap 通常會把腳本跟所有匯出檔掛在同一個目錄，兩邊都不用另外設路徑。
# 新增 function 只要把匯出的 json 丟進同一個目錄即可，不需要改這支腳本或任何設定。
FUNCTIONS_DIR = os.environ.get("FUNCTIONS_DIR", str(Path(__file__).resolve().parent))
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "180"))

HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": "application/json"}


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{WEBUI_URL}{path}", data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_for_ready():
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{WEBUI_URL}/health", timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(3)
    sys.exit(f"OpenWebUI not ready after {READY_TIMEOUT}s at {WEBUI_URL}")


def apply_function(fn: dict):
    fid = fn["id"]
    payload = {"id": fid, "name": fn["name"], "content": fn["content"], "meta": fn["meta"]}

    status, _ = request("GET", f"/api/v1/functions/id/{fid}")
    exists = status == 200

    if exists:
        status, body = request("POST", f"/api/v1/functions/id/{fid}/update", payload)
        if status != 200:
            sys.exit(f"[{fid}] update failed: {status} {body}")
        print(f"[{fid}] updated content")
        return

    status, body = request("POST", "/api/v1/functions/create", payload)
    if status != 200:
        sys.exit(f"[{fid}] create failed: {status} {body}")
    print(f"[{fid}] created")

    if fn.get("is_active"):
        status, body = request("POST", f"/api/v1/functions/id/{fid}/toggle")
        if status != 200:
            sys.exit(f"[{fid}] toggle is_active failed: {status} {body}")
        print(f"[{fid}] is_active -> True")

    if fn.get("is_global"):
        status, body = request("POST", f"/api/v1/functions/id/{fid}/toggle/global")
        if status != 200:
            sys.exit(f"[{fid}] toggle is_global failed: {status} {body}")
        print(f"[{fid}] is_global -> True")


def load_functions() -> list[dict]:
    # 可傳明確路徑做單一檔案測試，例如：apply_function.py openwebui/functions/thinking_mode.json
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(FUNCTIONS_DIR, "*.json")))
    if not paths:
        sys.exit(f"no *.json function export found in {FUNCTIONS_DIR}")
    functions = []
    for path in paths:
        with open(path) as f:
            functions.extend(json.load(f))
    return functions


def main():
    wait_for_ready()
    for fn in load_functions():
        apply_function(fn)


if __name__ == "__main__":
    main()
