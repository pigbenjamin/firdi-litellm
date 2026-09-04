#!/usr/bin/env python3
"""dev_admin_web.py — 在本機把 admin-web 跑起來，用瀏覽器點完整流程

給「改完想先看看畫面長怎樣」用的。**不需要叢集、不需要 Keycloak、不需要
LiteLLM、不需要 OpenWebUI**——這支腳本在同一個 process 裡：

  1. 用暫存檔當 users.db，塞一些假的部門與使用者
  2. 起一個假的 LiteLLM + 假的 OpenWebUI（同一個 http.server，用路徑前綴區分），
     寫法照 scripts/mock_openwebui.py 的樣式
  3. 把 require_admin 這個 dependency 換掉，直接當成管理員登入
  4. uvicorn 跑 admin-api 的 FastAPI app

於是「上架 → 測試呼叫 → 發布 → 授權（含 push 回 OpenWebUI）→ 停用 → 重新啟用
→ 刪除」整條路徑都真的會走一遍，只是上游是假的。

用法：
  python3 scripts/dev_admin_web.py
  python3 scripts/dev_admin_web.py --port 8099
  python3 scripts/dev_admin_web.py --fresh      # 清空資料重來（預設是保留的）

資料放在 --data-dir（預設 /tmp/firdi-devweb）這個**固定**路徑，重開服務不會消失
——驗收是一項一項慢慢做的，中間重開一次就把前面建好的模型全部丟掉會很難用。

要看「測試呼叫失敗」長怎樣，不用重開服務，打一行就切換：

  curl -s "http://127.0.0.1:8098/control/fail?code=401"   # 或 429 / 404
  curl -s "http://127.0.0.1:8098/control/fail?code=0"     # 改回一律成功

**這支腳本刻意繞過身分驗證**——任何連得到這個 port 的人都是管理員。所以預設只綁
127.0.0.1。從 SSH 進來想用瀏覽器看的話，優先用 SSH 埠轉送（VSCode Remote-SSH 的
「連接埠」面板，或 `ssh -L 8099:127.0.0.1:8099 <user>@<host>`）——那樣連線只走
SSH 通道，不會把管理介面丟到網路上。

真的需要讓同網段的其他機器連進來，才用 `--host 0.0.0.0`，且要記得：這時候內網任何
人打開網址就是管理員，而且它連的是假的上游、假的 DB，**絕對不要在正式環境跑**。
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "admin-api"))

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8099, help="admin-api 的 port（預設 8099）")
parser.add_argument("--host", default="127.0.0.1",
                    help="綁定位址（預設 127.0.0.1）。設成 0.0.0.0 會讓內網任何人都是管理員，"
                         "優先考慮 SSH 埠轉送")
parser.add_argument("--mock-port", type=int, default=8098, help="假上游的 port（預設 8098）")
parser.add_argument("--fail", type=int, default=0,
                    help="假上游的推論端點一律回這個狀態碼（開機預設值）。"
                         "服務跑起來之後可以隨時用 /control/fail 改，不用重開")
parser.add_argument("--data-dir", default=os.path.join(tempfile.gettempdir(), "firdi-devweb"),
                    help="DB 與紀錄檔放哪（預設 /tmp/firdi-devweb，固定路徑）")
parser.add_argument("--fresh", action="store_true",
                    help="把 --data-dir 整個清掉重來（預設保留，重開服務資料還在）")
args = parser.parse_args()

# 資料夾用固定路徑而不是 tempfile.mkdtemp()：驗收是一項一項慢慢做的，中間難免要
# 重開服務，每次換一個隨機目錄就等於把前面建好的模型、範本、稽核紀錄全部丟掉。
_TMP = args.data_dir
if args.fresh and os.path.isdir(_TMP):
    import shutil
    shutil.rmtree(_TMP)
os.makedirs(_TMP, exist_ok=True)
os.environ["USER_AUTH_DB_PATH"] = os.path.join(_TMP, "users.db")
os.environ["ADMIN_AUDIT_LOG_PATH"] = os.path.join(_TMP, "admin-web-audit.jsonl")
os.environ["LOG_PATH"] = os.path.join(_TMP, "usage.jsonl")
os.environ["LITELLM_MASTER_KEY"] = "sk-dev-master"
os.environ["LITELLM_URL"] = f"http://127.0.0.1:{args.mock_port}/litellm"
os.environ["OPENWEBUI_URL"] = f"http://127.0.0.1:{args.mock_port}/openwebui"
os.environ["OPENWEBUI_ADMIN_KEY"] = "dev-owui-key"
os.environ["ADMIN_WEB_USERNAMES"] = "firdiadm"
# curl 路徑（/api/v1/models/external 等）的共享金鑰。網頁流程完全用不到它，
# 這裡設一個固定值是為了讓「curl 上架的模型仍然直接是 published」這條回溯相容
# 的驗證，在本機也跑得起來。
os.environ["ADMIN_API_KEY"] = "dev-admin-key"


# ── 假的 LiteLLM + 假的 OpenWebUI ─────────────────────────────────────────────

class Upstream:
    """把兩個上游的狀態放在一起，讓 handler 好寫。deployments 用遞增 id 模擬
    LiteLLM 的行為——同一個 model_name 刪掉重建之後 id 會換一個。

    狀態會存成 JSON 檔跟著 --data-dir 一起持久化。只有 users.db 持久、假上游卻
    只活在記憶體的話，重開服務之後兩邊會對不起來：model_metadata 裡有這個模型、
    LiteLLM 裡卻沒有，畫面上就會看到一個「草稿但未註冊」的鬼狀態。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.deployments = []
        self.next_id = 1
        # 假裝 YAML model_list 已經定義了這幾個地端模型（＝「既有」模型，沒有管理紀錄）
        self.yaml_models = ["gemma-4-31B-it", "gemma-4-26B-A4B-it", "embeddinggemma-300m"]
        # OpenWebUI 側
        self.groups = [{"id": "grp-rd", "name": "RD"}, {"id": "grp-sales", "name": "SALES"}]
        self.owui_users = [
            {"id": "owui-1", "email": "alice@example.com", "oauth": {"oidc": {"sub": "uid-alice"}}},
            {"id": "owui-2", "email": "bob@example.com", "oauth": {"oidc": {"sub": "uid-bob"}}},
            {"id": "owui-3", "email": "carol@example.com", "oauth": {"oidc": {"sub": "uid-carol"}}},
            # 刻意留一個沒 SSO 登入過的，讓診斷頁看得到「對映不到」的情境
            {"id": "owui-4", "email": "dave@example.com", "oauth": {}},
        ]
        self.owui_models = {}   # model_id → 記錄（含 access_grants）
        # 假上游的推論端點要回什麼狀態碼。做成可以執行中改（見 /control/fail），
        # 因為「看 401/429/404 各自被翻成哪一句人話」是連著要驗的三件事，
        # 為了換一個數字就重開服務，等於每次都把前面建好的資料丟掉。
        self.fail_code = 0
        # 最近收到的請求，給 /control/requests 用。驗「embedding 模型會不會打到
        # /v1/embeddings」這種事，翻日誌不如直接問假上游收到什麼——尤其服務常常
        # 是在背景跑的，根本沒有終端機可以看。
        self.recent = []
        self.path = os.path.join(_TMP, "mock-upstream.json")
        self._load()

    # 只有 users.db 持久、假上游卻活在記憶體的話，重開服務之後兩邊會對不起來：
    # model_metadata 裡有這個模型、LiteLLM 裡卻沒有，畫面上就是一個「草稿但未註冊」
    # 的鬼狀態。所以這幾個欄位跟著 --data-dir 一起存檔。
    _PERSIST = ("deployments", "next_id", "owui_models")

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for key in self._PERSIST:
            if key in saved:
                setattr(self, key, saved[key])

    def save(self):
        """呼叫端已經持有 self.lock，這裡只負責寫檔。"""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({k: getattr(self, k) for k in self._PERSIST}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)   # 原子替換：中途被 Ctrl+C 也不會留下半個檔
        except OSError:
            pass

    def all_model_ids(self):
        return sorted(set(self.yaml_models + [d["model_name"] for d in self.deployments]))


UP = Upstream()


class MockHandler(BaseHTTPRequestHandler):
    # 刻意用 HTTP/1.0：每個請求結束就關連線，httpx 的連線池不會重用。用 1.1 的
    # keep-alive 時，ThreadingHTTPServer 這種簡易 server 會偶發地在對方重用連線的
    # 瞬間關掉它，httpx 拋 RemoteProtocolError，畫面上就變成一個看起來像程式壞掉的
    # 「發生錯誤」。假上游不在乎那點連線成本，穩定壓倒一切。
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        # flush 是必要的：導向檔案時 stdout 是 block-buffered，不 flush 的話
        # 日誌會卡在緩衝區裡，看起來像什麼都沒收到。
        print(f"  [mock] {fmt % a}", flush=True)

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    # ── LiteLLM ──
    def _litellm_get(self, path):
        if path == "/models":
            return self._send(200, {"data": [{"id": i} for i in UP.all_model_ids()]})
        if path == "/model/info":
            data = [
                {"model_name": n, "litellm_params": {"model": f"hosted_vllm/{n}"},
                 "model_info": {"id": f"yaml-{n}", "db_model": False}}
                for n in UP.yaml_models
            ] + [
                {"model_name": d["model_name"], "litellm_params": d["litellm_params"],
                 "model_info": {"id": d["id"], "db_model": True}}
                for d in UP.deployments
            ]
            return self._send(200, {"data": data})
        return self._send(404, {"error": f"unmocked GET {path}"})

    def _litellm_post(self, path, body):
        if path == "/model/new":
            with UP.lock:
                UP.deployments.append({
                    "id": f"dep-{UP.next_id}", "model_name": body["model_name"],
                    "litellm_params": body["litellm_params"],
                })
                UP.next_id += 1
                UP.save()
            return self._send(200, {"status": "ok"})
        if path == "/model/delete":
            with UP.lock:
                before = len(UP.deployments)
                UP.deployments = [d for d in UP.deployments if d["id"] != body.get("id")]
                found = len(UP.deployments) < before
                if found:
                    UP.save()
            return self._send(200 if found else 404, {"status": "ok" if found else "not found"})
        if path in ("/v1/chat/completions", "/v1/embeddings", "/v1/rerank"):
            with UP.lock:
                UP.recent.append({"endpoint": path, "model": body.get("model", ""),
                                  "replied": UP.fail_code or 200})
                del UP.recent[:-50]
            if UP.fail_code:
                return self._send(UP.fail_code, {"error": {
                    "message": f"（假的上游依目前設定回這個錯誤）", "code": UP.fail_code,
                }})
            return self._send(200, {"id": "dev", "choices": [], "data": []})
        return self._send(404, {"error": f"unmocked POST {path}"})

    # ── OpenWebUI ──
    def _owui_get(self, path):
        if path == "/api/v1/groups/":
            return self._send(200, UP.groups)
        if path == "/api/v1/users/all":
            return self._send(200, {"users": UP.owui_users})
        if path == "/api/v1/models/base":
            return self._send(200, list(UP.owui_models.values()))
        return self._send(404, {"error": f"unmocked GET {path}"})

    def _owui_post(self, path, body, query):
        if path == "/api/v1/models/model/update":
            model_id = query.get("id", [""])[0] if isinstance(query, dict) else ""
            with UP.lock:
                UP.owui_models[model_id] = body
                UP.save()
            return self._send(200, body)
        if path == "/api/v1/models/create":
            with UP.lock:
                UP.owui_models[body["id"]] = body
                UP.save()
            return self._send(200, body)
        return self._send(404, {"error": f"unmocked POST {path}"})

    def _requests(self):
        """假上游最近收到的推論請求（新的在前）。"""
        with UP.lock:
            return self._send(200, {"requests": list(reversed(UP.recent))})

    def _control(self, query):
        """切換假上游的回應狀態碼，不用重開服務。0（或 ok）代表恢復成成功。"""
        raw = (query.get("code", ["0"])[0] or "0").strip()
        code = 0 if raw in ("0", "ok", "") else int(raw)
        UP.fail_code = code
        state = "一律成功（200）" if not code else f"一律回 HTTP {code}"
        print(f"  [mock] 假上游改成：{state}", flush=True)
        return self._send(200, {"fail_code": code, "state": state})

    def do_GET(self):
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/control/requests":
            return self._requests()
        if parsed.path == "/control/fail":
            return self._control(parse_qs(parsed.query))
        if parsed.path.startswith("/litellm"):
            return self._litellm_get(parsed.path[len("/litellm"):] or "/")
        if parsed.path.startswith("/openwebui"):
            return self._owui_get(parsed.path[len("/openwebui"):] or "/")
        return self._send(404, {"error": "unknown upstream"})

    def do_POST(self):
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        # 一律先把 body 讀完再分流：留著沒讀的 body 會讓下一個請求讀到殘留資料。
        body = self._read_json()
        if parsed.path == "/control/requests":
            return self._requests()
        if parsed.path == "/control/fail":
            return self._control(parse_qs(parsed.query))
        if parsed.path.startswith("/litellm"):
            return self._litellm_post(parsed.path[len("/litellm"):] or "/", body)
        if parsed.path.startswith("/openwebui"):
            return self._owui_post(parsed.path[len("/openwebui"):] or "/", body, parse_qs(parsed.query))
        return self._send(404, {"error": "unknown upstream"})


# ── 種一些假資料 ──────────────────────────────────────────────────────────────

from database import DB_PATH, get_conn, init_db  # noqa: E402

init_db(DB_PATH)

# HR 刻意設成 ["*"]（不限制）：授權矩陣要驗「* 部門不列進矩陣、只在上方以警告
# 列出」，本機得先有這樣一個部門才驗得到。
_DEPTS = [
    ("RD", "研發部", ["gemma-4-31B-it"]),
    ("SALES", "業務部", ["gemma-4-31B-it"]),
    ("HR", "人資部", ["*"]),
]
_USERS = [
    ("uid-alice", "alice@example.com", "RD"),
    ("uid-bob", "bob@example.com", "RD"),
    ("uid-carol", "carol@example.com", "SALES"),
    ("uid-dave", "dave@example.com", "SALES"),
    ("uid-erin", "erin@example.com", "HR"),
]


def seed():
    with get_conn(DB_PATH) as conn:
        for dept_id, name, allowed in _DEPTS:
            conn.execute(
                "INSERT OR IGNORE INTO departments (dept_id, dept_name, allowed_models, provider_keys) "
                "VALUES (?, ?, ?, ?)",
                (dept_id, name, json.dumps(allowed),
                 json.dumps({"openrouter": "sk-or-dev-1234"}) if dept_id == "RD" else "{}"),
            )
        for user_id, email, dept_id in _USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users (api_key, key_name, user_id, user_email, dept_id, models) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"sk-dev-{user_id}", email.split('@')[0], user_id, email, dept_id, "[]"),
            )
        # 給既有的地端模型塞一點用量，額度那一欄才看得到東西
        conn.execute(
            "INSERT OR IGNORE INTO model_spend (model_name, period, spend_usd, calls) "
            "VALUES ('gemma-4-31B-it', strftime('%Y-%m','now'), 12.3456, 421)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO model_spend (model_name, period, spend_usd, calls) "
            "VALUES ('gemma-4-31B-it', 'total', 58.9012, 1893)"
        )


seed()

# ── 換掉身分驗證，起 server ───────────────────────────────────────────────────

from admin_auth import require_admin  # noqa: E402
from main import app  # noqa: E402

DEV_ADMIN = {"preferred_username": "firdiadm", "email": "firdiadm@dev.local", "sub": "dev-sub"}
app.dependency_overrides[require_admin] = lambda: DEV_ADMIN

if __name__ == "__main__":
    import uvicorn

    UP.fail_code = args.fail
    mock = ThreadingHTTPServer(("127.0.0.1", args.mock_port), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    _EXPOSED = (
        "\n  ⚠ 警告                    綁在 " + args.host + "，這個網段上任何人打開網址就是管理員"
        if args.host not in ("127.0.0.1", "localhost") else "（只綁 127.0.0.1）"
    )
    _URL_HOST = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host

    print(f"""
  假的 LiteLLM / OpenWebUI  http://127.0.0.1:{args.mock_port}
  users.db                  {DB_PATH}
  稽核紀錄                  {os.environ['ADMIN_AUDIT_LOG_PATH']}
  身分驗證                  已繞過，一律當成 firdiadm 登入{_EXPOSED}
  假上游的推論端點          {'一律回 HTTP ' + str(args.fail) if args.fail else '一律成功（200）'}
  curl 路徑的 ADMIN_API_KEY  dev-admin-key
  資料保留                  重開服務資料還在；要清空重來加 --fresh

  打開 →  http://{_URL_HOST}:{args.port}/api/v1/admin/web

  建議的點法：
    1. 上架模型 → OpenRouter → slug 填 anthropic/claude-sonnet-4-5、key 隨便填一個
    2. 成功頁點進詳情 → 按「測試呼叫」→ 再按「發布」
       想看失敗訊息長怎樣，不用重開服務，換一行指令就好：
         curl -s "http://127.0.0.1:{args.mock_port}/control/fail?code=401"   # 或 429 / 404
         curl -s "http://127.0.0.1:{args.mock_port}/control/fail?code=0"     # 改回成功
    3. 想確認「測試呼叫到底打了哪個端點」（例如 embedding 是不是走 /v1/embeddings）：
         curl -s "http://127.0.0.1:{args.mock_port}/control/requests" | python3 -m json.tool
    4. 模型授權 → 勾幾個 → 預覽變更 → 確認（會真的 push 到假的 OpenWebUI）
    5. 回詳情頁 → 停用 → 重新啟用 → 永久刪除（會先算影響範圍）
    6. 稽核紀錄 → 看 before/after → 下載 CSV
""", flush=True)  # 導向檔案時 stdout 是 block-buffered，不 flush 這段會卡住看不到
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
