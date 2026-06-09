#!/usr/bin/env python3
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "http://localhost:4000"
LOCAL_USER_KEY = "dev-local-key-001"
PAID_USER_KEY = "dev-paid-key-001"
RATE_LIMIT_USER_KEY = "dev-rate-limit-key-001"


@dataclass
class HttpResult:
    status: int
    body: str
    headers: dict[str, str]


def request_json(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> HttpResult:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(
                status=response.status,
                body=response.read().decode("utf-8", errors="replace"),
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as error:
        return HttpResult(
            status=error.code,
            body=error.read().decode("utf-8", errors="replace"),
            headers=dict(error.headers.items()),
        )


def request_stream(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int = 120,
) -> HttpResult:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    chunks: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                chunks.append(line)
                if line == "data: [DONE]":
                    break
                if len(chunks) >= 8:
                    break
            return HttpResult(
                status=response.status,
                body="\n".join(chunks),
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as error:
        return HttpResult(
            status=error.code,
            body=error.read().decode("utf-8", errors="replace"),
            headers=dict(error.headers.items()),
        )


def chat_payload(model: str, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly one short sentence.",
            }
        ],
        "max_tokens": 16,
        "temperature": 0,
        "stream": stream,
    }


def print_result(name: str, result: HttpResult, expected: set[int]) -> bool:
    ok = result.status in expected
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name}: HTTP {result.status}")
    if not ok:
        print(trim_body(result.body))
    return ok


def trim_body(body: str, limit: int = 700) -> str:
    compact = body.strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "... <trimmed>"


def test_invalid_auth(base_url: str) -> bool:
    result = request_json(
        "POST",
        f"{base_url}/v1/chat/completions",
        "invalid-key-that-does-not-exist",
        chat_payload("local-qwen3.5-9b"),
    )
    return print_result("invalid key is rejected with 401", result, {401})


def test_models(base_url: str) -> bool:
    result = request_json("GET", f"{base_url}/v1/models", LOCAL_USER_KEY)
    return print_result("local user can list models", result, {200})


def test_local_user_allowed_model(base_url: str, model: str) -> bool:
    result = request_json(
        "POST",
        f"{base_url}/v1/chat/completions",
        LOCAL_USER_KEY,
        chat_payload(model),
    )
    return print_result(f"local user can call {model}", result, {200})


def test_local_user_forbidden_model(base_url: str, forbidden_model: str) -> bool:
    result = request_json(
        "POST",
        f"{base_url}/v1/chat/completions",
        LOCAL_USER_KEY,
        chat_payload(forbidden_model),
    )
    return print_result(
        f"local user is blocked from {forbidden_model}",
        result,
        {400, 401, 403},
    )


def test_paid_user_model(base_url: str, model: str) -> bool:
    result = request_json(
        "POST",
        f"{base_url}/v1/chat/completions",
        PAID_USER_KEY,
        chat_payload(model),
    )
    return print_result(f"paid user can call {model}", result, {200})


def test_streaming(base_url: str, model: str) -> bool:
    result = request_stream(
        f"{base_url}/v1/chat/completions",
        LOCAL_USER_KEY,
        chat_payload(model, stream=True),
    )
    ok = print_result(f"local user streaming call to {model}", result, {200})
    if ok:
        first_lines = "\n".join(result.body.splitlines()[:3])
        print(first_lines)
    return ok


def test_rate_limit(base_url: str, model: str) -> bool:
    statuses: list[int] = []
    for index in range(4):
        result = request_json(
            "POST",
            f"{base_url}/v1/chat/completions",
            RATE_LIMIT_USER_KEY,
            chat_payload(model),
        )
        statuses.append(result.status)
        print(f"  burst request {index + 1}: HTTP {result.status}")
        time.sleep(0.2)

    ok = 429 in statuses
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] rate limit user eventually receives HTTP 429: {statuses}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test LiteLLM custom auth.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--local-model", default="local-qwen3.5-9b")
    parser.add_argument("--forbidden-model", default="gpt-4o-mini")
    parser.add_argument("--paid-model", default="")
    parser.add_argument("--gpt-oss-model", default="local-gpt-oss-20b")
    parser.add_argument("--skip-gpt-oss", action="store_true")
    parser.add_argument("--skip-rate-limit", action="store_true")
    parser.add_argument("--skip-stream", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    checks = [
        test_invalid_auth(base_url),
        test_models(base_url),
        test_local_user_allowed_model(base_url, args.local_model),
        test_local_user_forbidden_model(base_url, args.forbidden_model),
    ]

    if not args.skip_gpt_oss:
        checks.append(test_paid_user_model(base_url, args.gpt_oss_model))
        checks.append(test_local_user_forbidden_model(base_url, args.gpt_oss_model))

    if args.paid_model:
        checks.append(test_paid_user_model(base_url, args.paid_model))

    if not args.skip_stream:
        checks.append(test_streaming(base_url, args.local_model))

    if not args.skip_rate_limit:
        checks.append(test_rate_limit(base_url, args.local_model))

    passed = sum(1 for check in checks if check)
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
