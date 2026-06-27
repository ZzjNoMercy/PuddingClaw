"""探测 Higress 网关是否接受 OpenAI 风格的 thinking 参数。"""

import json
import sys
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from config import get_gateway_config, get_gateway_llm_config


def main():
    gateway_cfg = get_gateway_config()
    llm_cfg = get_gateway_llm_config()
    base_url = gateway_cfg.get("base_url") or "http://localhost:8080/v1"
    url = base_url.rstrip("/") + "/chat/completions"
    model = llm_cfg.get("model", "deepseek-v4-pro")

    # 优先用环境变量里的真实 gateway key，没有则沿用代码里的占位 key
    import os
    api_key = os.getenv("AI_GATEWAY_API_KEY") or os.getenv("OPENAI_API_KEY") or "puddingclaw-gateway"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    base_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "你好"}],
        "stream": False,
        "max_tokens": 10,
    }

    cases = [
        ("without_thinking", base_payload),
        (
            "with_reasoning_effort",
            {**base_payload, "reasoning_effort": "high"},
        ),
        (
            "with_extra_body_thinking",
            {**base_payload, "extra_body": {"thinking": {"type": "enabled"}}},
        ),
        (
            "with_both",
            {
                **base_payload,
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
        ),
    ]

    print(f"Testing Higress: {url}")
    print(f"Model: {model}")
    print(f"API key (masked): {api_key[:4]}...{api_key[-4:]}")
    print("-" * 60)

    with httpx.Client(timeout=15.0) as client:
        for name, payload in cases:
            print(f"\nCase: {name}")
            try:
                resp = client.post(url, headers=headers, json=payload)
                print(f"  status: {resp.status_code}")
                try:
                    body = resp.json()
                    print(f"  body: {json.dumps(body, ensure_ascii=False, indent=2)[:500]}")
                except Exception:
                    print(f"  text: {resp.text[:500]}")
            except Exception as exc:
                print(f"  error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
