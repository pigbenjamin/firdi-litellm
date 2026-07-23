#!/usr/bin/env python3
"""31b FP8 品質評測：對指定 vLLM base_url 送出 prompts.json 的固定題組，
記錄輸出內容 + TTFT/tokens 等指標成 JSON transcript，供 judge.py 比對。

用法：
    kubectl port-forward -n ai-platform svc/gemma-4-31b-vllm-service 8001:8000 &
    python3 run_eval.py --base-url http://127.0.0.1:8001 --label bf16

    kubectl port-forward -n ai-platform svc/gemma-4-31b-fp8-test-service 8002:8000 &
    python3 run_eval.py --base-url http://127.0.0.1:8002 --label fp8
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_MODEL = "google/gemma-4-31B-it"
SCRIPT_DIR = Path(__file__).resolve().parent


def check_health(base_url: str, timeout: float = 10) -> None:
    url = f"{base_url}/health"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        sys.exit(f"[FATAL] 無法連到 {url}：{exc}\n請確認 kubectl port-forward 是否還開著。")
    if resp.status_code != 200:
        sys.exit(f"[FATAL] {url} 回傳 {resp.status_code}，pod 可能還在冷啟動/編譯中，先別跑評測。")
    print(f"[OK] health check 通過（{url}）")


def build_payload(prompt: dict, model: str) -> dict:
    payload = {
        "model": model,
        "messages": prompt["messages"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if "tools" in prompt:
        payload["tools"] = prompt["tools"]
        payload["tool_choice"] = "auto"
    if "enable_thinking" in prompt:
        payload["chat_template_kwargs"] = {"enable_thinking": prompt["enable_thinking"]}
    return payload


def run_one(base_url: str, prompt: dict, model: str, timeout: float) -> dict:
    payload = build_payload(prompt, model)
    url = f"{base_url}/v1/chat/completions"

    result = {
        "id": prompt["id"],
        "category": prompt.get("category"),
        "enable_thinking": prompt.get("enable_thinking"),
        "has_tools": "tools" in prompt,
        "status": "ok",
        "http_status": None,
        "error": None,
        "ttft_ms": None,
        "total_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "content": "",
        "reasoning": "",
        "tool_calls": [],
    }

    start = time.monotonic()
    try:
        resp = requests.post(url, json=payload, stream=True, timeout=(10, timeout))
    except requests.RequestException as exc:
        result["status"] = "error"
        result["error"] = f"request failed: {exc}"
        return result

    result["http_status"] = resp.status_code
    if resp.status_code != 200:
        result["status"] = "error"
        result["error"] = f"HTTP {resp.status_code}: {resp.text[:500]}"
        return result

    first_token_at = None
    tool_calls_acc = {}  # index -> {id, name, arguments}
    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            data_str = raw_line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            usage = chunk.get("usage")
            if usage:
                result["prompt_tokens"] = usage.get("prompt_tokens")
                result["completion_tokens"] = usage.get("completion_tokens")

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            piece = delta.get("content")
            reasoning_piece = delta.get("reasoning") or delta.get("reasoning_content")
            tc_pieces = delta.get("tool_calls")

            if (piece or reasoning_piece or tc_pieces) and first_token_at is None:
                first_token_at = time.monotonic()

            if piece:
                result["content"] += piece
            if reasoning_piece:
                result["reasoning"] += reasoning_piece
            if tc_pieces:
                for tc in tc_pieces:
                    idx = tc.get("index", 0)
                    slot = tool_calls_acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
    except requests.RequestException as exc:
        result["status"] = "error"
        result["error"] = f"stream interrupted: {exc}"

    end = time.monotonic()
    result["total_ms"] = round((end - start) * 1000, 1)
    if first_token_at is not None:
        result["ttft_ms"] = round((first_token_at - start) * 1000, 1)
    result["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8001（vLLM pod，透過 port-forward）")
    ap.add_argument("--label", required=True, help="這次跑的版本標籤，例如 bf16 / fp8")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompts", default=str(SCRIPT_DIR / "prompts.json"))
    ap.add_argument("--out", default=None, help="預設寫到 scripts/fp8_eval/results/<label>-<timestamp>.json")
    ap.add_argument("--timeout", type=float, default=300, help="單題最長等待秒數（thinking 題可能較久）")
    args = ap.parse_args()

    base_url = args.base_url.rstrip("/")
    prompts = json.loads(Path(args.prompts).read_text(encoding="utf-8"))

    check_health(base_url)

    out_dir = SCRIPT_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{args.label}-{ts}.json"

    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    for i, prompt in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] {prompt['id']} ({prompt.get('category')}) ...", end=" ", flush=True)
        r = run_one(base_url, prompt, args.model, args.timeout)
        results.append(r)
        if r["status"] == "ok":
            print(f"ok  ttft={r['ttft_ms']}ms total={r['total_ms']}ms completion_tokens={r['completion_tokens']}")
        else:
            print(f"ERROR: {r['error']}")

    out = {
        "meta": {
            "label": args.label,
            "base_url": base_url,
            "model": args.model,
            "prompts_file": args.prompts,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
        "results": results,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    n_err = sum(1 for r in results if r["status"] != "ok")
    print(f"\n寫入 {out_path}（{len(results)} 題，{n_err} 題失敗）")


if __name__ == "__main__":
    main()
