#!/usr/bin/env python3
"""31b FP8 品質評測：讀 run_eval.py 產出的 bf16 / fp8 transcript，逐題把兩邊輸出
（順序隨機，避免 judge 位置偏誤）丟給本地 gemma-4-26b 當 judge 做 pairwise 比較。

JSON 解析失敗時不會自動判負分（避免重蹈 docblock rerank_hits 的舊坑：「JSON 失敗給 0 分」
會把量化模型的正常輸出誤判成品質差）——會重試一次，仍失敗就標成 needs_manual_review，
不計入自動勝率統計，留給人工複核。

用法：
    kubectl port-forward -n ai-platform svc/gemma-4-26b-vllm-service 8003:8000 &
    python3 judge.py --baseline results/bf16-xxx.json --candidate results/fp8-xxx.json \\
        --judge-base-url http://127.0.0.1:8003
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_JUDGE_MODEL = "google/gemma-4-26B-A4B-it"

REGRESSION_VOCAB = [
    "tool_call_broken", "thinking_leaked", "off_topic",
    "language_mixing", "truncated", "factual_error", "other",
]

JUDGE_INSTRUCTIONS = f"""你是嚴謹的 LLM 輸出品質評審。你會看到同一個使用者請求的兩個模型回覆（A 和 B），
其中一個是原始 bf16 精度模型的輸出、另一個是 FP8 量化後模型的輸出，但你「不知道」哪個是哪個，
請單純比較品質。

判斷標準：正確性、有沒有回答到問題、格式是否符合要求（例如有沒有要求輸出 JSON 卻夾雜其他文字、
tool call 的 JSON 參數格式是否正確）、有沒有語言錯亂或明顯胡言亂語、思考過程有沒有洩漏到不該出現的地方。

只能輸出一個 JSON object，不要有 JSON 以外的任何文字、不要用 markdown code fence 包起來，格式：
{{"winner": "A" | "B" | "tie", "regression_flags": [從這個詞彙表挑: {REGRESSION_VOCAB}], "notes": "一句話說明理由"}}
"""


def load_transcript(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in data["results"]}
    return {"meta": data["meta"], "by_id": by_id}


def render_side(r: dict) -> str:
    parts = []
    if r.get("reasoning"):
        parts.append(f"【思考過程】\n{r['reasoning']}")
    if r.get("content"):
        parts.append(f"【回覆內容】\n{r['content']}")
    if r.get("tool_calls"):
        tc_lines = []
        for tc in r["tool_calls"]:
            tc_lines.append(f"- {tc.get('name')}({tc.get('arguments')})")
        parts.append("【Tool Calls】\n" + "\n".join(tc_lines))
    if r.get("status") != "ok":
        parts.append(f"【錯誤】{r.get('error')}")
    return "\n\n".join(parts) if parts else "(空輸出)"


def order_for(prompt_id: str) -> bool:
    """回傳 True 表示 baseline 放 A、False 表示 baseline 放 B。用 id 的 hash 決定，穩定可重現。"""
    h = hashlib.sha256(prompt_id.encode()).hexdigest()
    return int(h, 16) % 2 == 0


def extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def call_judge(judge_url: str, judge_model: str, prompt_text: str, side_a: str, side_b: str,
                timeout: float, retry_note: bool = False) -> tuple[dict | None, str]:
    user_content = (
        f"【原始使用者請求】\n{prompt_text}\n\n"
        f"【回覆 A】\n{side_a}\n\n【回覆 B】\n{side_b}"
    )
    if retry_note:
        user_content += "\n\n（提醒：只准輸出剛才規定的 JSON，不要有其他任何文字。）"

    payload = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": JUDGE_INSTRUCTIONS},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
    }
    resp = requests.post(f"{judge_url}/v1/chat/completions", json=payload, timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return extract_json(raw), raw


def summarize_prompt_text(messages: list) -> str:
    user_msgs = [m["content"] for m in messages if m["role"] == "user"]
    return "\n---\n".join(user_msgs) if user_msgs else "(no user message)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="bf16 transcript json（run_eval.py 產出）")
    ap.add_argument("--candidate", required=True, help="fp8 transcript json（run_eval.py 產出）")
    ap.add_argument("--prompts", default=str(SCRIPT_DIR / "prompts.json"))
    ap.add_argument("--judge-base-url", required=True, help="e.g. http://127.0.0.1:8003（26b，透過 port-forward）")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--out", default=None)
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    baseline = load_transcript(args.baseline)
    candidate = load_transcript(args.candidate)
    prompts = {p["id"]: p for p in json.loads(Path(args.prompts).read_text(encoding="utf-8"))}

    judge_url = args.judge_base_url.rstrip("/")

    verdicts = []
    for pid, prompt in prompts.items():
        b = baseline["by_id"].get(pid)
        c = candidate["by_id"].get(pid)
        if b is None or c is None:
            print(f"[SKIP] {pid}: 兩份 transcript 沒有同時存在，略過")
            continue

        v = {"id": pid, "category": prompt.get("category")}

        if b.get("status") != "ok" or c.get("status") != "ok":
            v["outcome"] = "skipped_error"
            v["note"] = f"baseline_status={b.get('status')} candidate_status={c.get('status')}"
            verdicts.append(v)
            print(f"[{pid}] skipped_error（生成本身失敗，不列入自動評分）")
            continue

        baseline_is_a = order_for(pid)
        side_a = render_side(b if baseline_is_a else c)
        side_b = render_side(c if baseline_is_a else b)
        prompt_text = summarize_prompt_text(prompt["messages"])

        parsed, raw = call_judge(judge_url, args.judge_model, prompt_text, side_a, side_b, args.timeout)
        if parsed is None:
            parsed, raw = call_judge(judge_url, args.judge_model, prompt_text, side_a, side_b,
                                      args.timeout, retry_note=True)

        if parsed is None:
            v["outcome"] = "needs_manual_review"
            v["note"] = "judge JSON 解析兩次都失敗，不計入自動勝率"
            v["judge_raw"] = raw
            verdicts.append(v)
            print(f"[{pid}] needs_manual_review（judge 沒吐出合法 JSON）")
            continue

        winner_letter = parsed.get("winner")
        if winner_letter == "tie":
            winner = "tie"
        elif winner_letter == "A":
            winner = "baseline" if baseline_is_a else "candidate"
        elif winner_letter == "B":
            winner = "candidate" if baseline_is_a else "baseline"
        else:
            v["outcome"] = "needs_manual_review"
            v["note"] = f"judge 回傳未知 winner 值：{winner_letter!r}"
            v["judge_raw"] = raw
            verdicts.append(v)
            print(f"[{pid}] needs_manual_review（winner 欄位不是 A/B/tie）")
            continue

        v["outcome"] = "judged"
        v["winner"] = winner  # "baseline" (bf16) / "candidate" (fp8) / "tie"
        v["regression_flags"] = parsed.get("regression_flags", [])
        v["notes"] = parsed.get("notes", "")
        verdicts.append(v)
        flag_str = f" flags={v['regression_flags']}" if v["regression_flags"] else ""
        print(f"[{pid}] winner={winner}{flag_str}")

    judged = [v for v in verdicts if v["outcome"] == "judged"]
    n_bf16 = sum(1 for v in judged if v["winner"] == "baseline")
    n_fp8 = sum(1 for v in judged if v["winner"] == "candidate")
    n_tie = sum(1 for v in judged if v["winner"] == "tie")
    n_manual = sum(1 for v in verdicts if v["outcome"] == "needs_manual_review")
    n_skip = sum(1 for v in verdicts if v["outcome"] == "skipped_error")
    flagged_ids = [v["id"] for v in judged if v.get("regression_flags")]

    summary = {
        "total": len(verdicts),
        "judged": len(judged),
        "bf16_win": n_bf16,
        "fp8_win": n_fp8,
        "tie": n_tie,
        "needs_manual_review": n_manual,
        "skipped_error": n_skip,
        "fp8_flagged_regression_ids": flagged_ids,
        "needs_manual_review_ids": [v["id"] for v in verdicts if v["outcome"] == "needs_manual_review"],
        "skipped_error_ids": [v["id"] for v in verdicts if v["outcome"] == "skipped_error"],
    }

    out_dir = SCRIPT_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"judge-{ts}.json"

    out_path.write_text(json.dumps({
        "meta": {
            "baseline_file": args.baseline,
            "candidate_file": args.candidate,
            "judge_model": args.judge_model,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "summary": summary,
        "verdicts": verdicts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 總結 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n寫入 {out_path}")
    if flagged_ids:
        print(f"\n[注意] 以下題目被 judge 標記可能有功能性退化，Phase 3 要人工複核：{flagged_ids}")
    if summary["needs_manual_review_ids"]:
        print(f"[注意] judge 解析失敗，需要人工複核：{summary['needs_manual_review_ids']}")


if __name__ == "__main__":
    main()
