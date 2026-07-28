"""Ch5 cache hit 真跑 smoke test — 用真实 DeepSeek API 验证 prefix cache 命中。

三个场景：
  A. Raw LLM 多轮 warm-up：验证 DeepSeek cache 本身可用
  B. 项目真实 system_prompt 多轮：验证我们的 prompt 结构能命中 cache
  C. TailTrim 孤儿保护：构造 AI↔Tool 配对 → 真跑 DeepSeek → 验证无 400

运行：
    cd backend && python -m tests.smoke_cache_hit

前置：
    - DEEPSEEK_API_KEY 已配置（.env / config.json / shell env 任一）
    - 已 pip install -r requirements.txt

退出码：
    0 全部通过
    1 API 不可用或场景失败
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# 保证从 backend/ 跑时也能 import
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
# 优先加载课程根目录 .env（真实 key），后备 backend/.env
load_dotenv(BACKEND_DIR.parent.parent / ".env")  # Agent_Context进阶/.env
load_dotenv(BACKEND_DIR / ".env", override=False)  # 已有不覆盖

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


def _extract_cache_metrics(response: Any) -> dict[str, int]:
    """从 langchain-deepseek 响应里抽 prefix cache 指标。

    DeepSeek API 在 usage 里返回：
      prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, completion_tokens
    langchain-deepseek 会把原始 usage 放在 response_metadata.token_usage
    或 response.usage_metadata (canonical 字段，可能缺 cache_hit)。
    """
    prompt = hit = miss = 0
    rm = getattr(response, "response_metadata", {}) or {}
    tu = rm.get("token_usage") or {}
    prompt = tu.get("prompt_tokens", 0)
    hit = tu.get("prompt_cache_hit_tokens", 0)
    miss = tu.get("prompt_cache_miss_tokens", 0)

    # 兜底：从 canonical usage_metadata 取 input_tokens
    if not prompt:
        um = getattr(response, "usage_metadata", {}) or {}
        prompt = um.get("input_tokens", 0)
    return {"prompt": prompt, "hit": hit, "miss": miss}


def _build_llm():
    """用项目 config 统一入口构造 ChatDeepSeek。"""
    from langchain_deepseek import ChatDeepSeek
    from config import get_fallback_llm_config
    cfg = get_fallback_llm_config()
    key = cfg.get("api_key", "")
    # 同时检测空值和明显的占位符
    if not key or key.startswith("your_") or "placeholder" in key.lower() or key.endswith("_here"):
        print("[SKIP] DEEPSEEK_API_KEY 未配置或仍是占位符。")
        print("       请在以下任一位置设置真实 key：")
        print("         1. backend/.env:        DEEPSEEK_API_KEY=sk-...")
        print("         2. backend/config.json: fallback_llm.api_key")
        print("         3. shell env:           export DEEPSEEK_API_KEY=sk-...")
        sys.exit(1)
    return ChatDeepSeek(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0,
    )


# ─────────────────────────────────────────────────────────────
# Scenario A：Raw LLM 多轮 warm-up（观测型，非断言）
# ─────────────────────────────────────────────────────────────
async def scenario_a_raw_warmup(llm) -> None:
    """构造一个 >1024 token 的稳定 system，连发 3 轮完全相同的请求。

    教学目标：让学员亲眼看 DeepSeek prefix cache 从 0 到 >0 的 warm-up 过程。

    注意：DeepSeek 服务端 cache 有数小时 TTL，如果脚本在 TTL 内重复运行，
    Turn 1 就会直接命中旧 cache（`hit > 0`）——这不是 bug，是服务端特性。
    因此本场景**只打印观测**，不做 assertion；真正该守住的持续指标在 B 和 C。

    首次干净运行（冷启动）应看到：Turn 1 hit=0 → Turn 2/3 hit>0（课堂演示直观效果）。
    """
    print("\n" + "=" * 70)
    print("Scenario A：Raw LLM prefix cache warm-up（观测型）")
    print("=" * 70)

    # 长 system prompt（≥ 1024 tokens）保证过 DeepSeek cache 最小块阈值
    # 重复一段 ~100 字符中文 × 30 ≈ 3000 字符 ≈ 2000 tokens
    system_text = "你是一名资深后端工程师，精通 Python、FastAPI、LangChain、向量检索。" * 30
    system = SystemMessage(content=system_text)
    user = HumanMessage(content="用一句话解释什么是协程")

    results = []
    for i in range(1, 4):
        resp = await llm.ainvoke([system, user])
        m = _extract_cache_metrics(resp)
        results.append(m)
        print(f"  Turn {i}:  prompt={m['prompt']:>5}  "
              f"cache_hit={m['hit']:>5}  miss={m['miss']:>5}")

    turn1_hit = results[0]["hit"]
    if turn1_hit == 0:
        print(f"  [观测] 冷启动模式：Turn 1 hit=0 → Turn 2+ hit={results[1]['hit']}，cache warm-up 直观可见")
    else:
        print(f"  [观测] 服务端 cache 已预热（TTL 内重跑脚本）：Turn 1 hit={turn1_hit}，本次无法演示冷启动过程")
    print(f"         教学结论：只要 Turn N hit > 0，DeepSeek prefix cache 功能可用（核心指标看 Scenario B）")


# ─────────────────────────────────────────────────────────────
# Scenario B：项目真实 system_prompt 多轮
# ─────────────────────────────────────────────────────────────
async def scenario_b_project_prompt(llm) -> bool:
    """用 build_system_prompt 生成项目真实的 system prompt，连发 3 轮。

    预期：我们的 cache-aware 重构（MEMORY_WRITE_PROTOCOL_STATIC 合入静态前缀）
    让 system 字节稳定，Turn 2+ 应有显著 cache_hit。
    """
    print("\n" + "=" * 70)
    print("Scenario B：项目 build_system_prompt 多轮 cache 验证")
    print("=" * 70)

    from graph.prompt_builder import build_system_prompt
    from config import get_memory_backend, get_rag_mode

    system_text = build_system_prompt(
        base_dir=BACKEND_DIR,
        rag_mode=get_rag_mode(),
        memory_backend=get_memory_backend(),
        mem0_context="",
        rag_context="",
        tool_reminder=False,
    )
    print(f"  真实 system_prompt 字节数: {len(system_text.encode('utf-8'))}")

    system = SystemMessage(content=system_text)
    user = HumanMessage(content="你好，用一句话自我介绍")

    results = []
    for i in range(1, 4):
        resp = await llm.ainvoke([system, user])
        m = _extract_cache_metrics(resp)
        results.append(m)
        print(f"  Turn {i}:  prompt={m['prompt']:>5}  "
              f"cache_hit={m['hit']:>5}  miss={m['miss']:>5}")

    turn2_hit = results[1]["hit"]
    turn3_hit = results[2]["hit"]
    if turn2_hit <= 0:
        print(f"  [FAIL] Turn 2 cache_hit=0，项目 system prompt 未命中 cache。"
              f"检查 prompt_builder 静态前缀是否字节级稳定。")
        return False
    hit_ratio = turn3_hit / max(results[2]["prompt"], 1)
    print(f"  [PASS] Turn 3 命中率 {hit_ratio*100:.1f}% "
          f"({turn3_hit}/{results[2]['prompt']})")
    return True


# ─────────────────────────────────────────────────────────────
# Scenario C：TailTrim 孤儿保护 × 真实 API 校验
# ─────────────────────────────────────────────────────────────
async def scenario_c_tailtrim_orphan_safe(llm) -> bool:
    """构造含 AI↔Tool 配对的长历史，跑 TailTrim，把结果送 DeepSeek 验证无 400。

    这是 Critical #2 修复的端到端校验：TailTrim 的孤儿保护若失效，
    DeepSeek 会报 "tool_call_id without matching AIMessage.tool_calls"。
    """
    print("\n" + "=" * 70)
    print("Scenario C：TailTrim 孤儿保护 × DeepSeek API 校验")
    print("=" * 70)

    from graph.middlewares.cache import TailTrimMiddleware

    # 构造一段带工具调用的消息历史
    # head(2) + middle[AI(tc=x), Tool(x), AI_text, AI(tc=y), Tool(y)] + recent(3: 含 Tool(y) 跨界)
    msgs = [
        SystemMessage(content="你是一个简洁的助手。"),
        HumanMessage(content="第一个问题", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"name": "search", "args": {"q": "x"}, "id": "tc_x", "type": "tool_call"},
        ]),
        ToolMessage(content="x 的结果", id="tm_x", tool_call_id="tc_x"),
        AIMessage(content="基于 x 的答案", id="a2"),
        AIMessage(content="", id="a3", tool_calls=[
            {"name": "search", "args": {"q": "y"}, "id": "tc_y", "type": "tool_call"},
        ]),
        ToolMessage(content="y 的结果", id="tm_y", tool_call_id="tc_y"),
        AIMessage(content="基于 y 的最终答案", id="a4"),
        HumanMessage(content="追问", id="h2"),
    ]

    tt = TailTrimMiddleware(max_tokens=1, head_keep=2, keep_recent=3)
    result = tt.before_model({"messages": msgs}, None)
    removed_ids = {rm.id for rm in (result or {}).get("messages", [])}
    print(f"  TailTrim removed: {sorted(removed_ids)}")

    # 应用删除，得到"trim 后"的消息列表
    trimmed = [m for m in msgs if getattr(m, "id", None) not in removed_ids]
    print(f"  Trim 前 {len(msgs)} 条 → Trim 后 {len(trimmed)} 条")

    # 把 trim 后的消息直接送 DeepSeek——如果孤儿保护失效，这里会 400
    try:
        resp = await llm.ainvoke(trimmed + [HumanMessage(content="请基于以上上下文回答追问")])
        m = _extract_cache_metrics(resp)
        reply = getattr(resp, "content", "")[:80]
        print(f"  [PASS] DeepSeek 正常返回 (无 400): prompt={m['prompt']}  hit={m['hit']}")
        print(f"         reply 前 80 字: {reply}")
        return True
    except Exception as e:
        msg = str(e)
        if "400" in msg or "invalid" in msg.lower() or "tool_call" in msg.lower():
            print(f"  [FAIL] DeepSeek 拒绝请求（疑似孤儿 ToolMessage）: {type(e).__name__}: {msg[:200]}")
            return False
        print(f"  [FAIL] 意外异常: {type(e).__name__}: {msg[:200]}")
        return False


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
async def main() -> int:
    print("Ch5 cache hit 真跑 smoke test")
    llm = _build_llm()
    print(f"LLM: model={llm.model_name}")

    await scenario_a_raw_warmup(llm)              # 观测型，不影响 exit code
    ok_b = await scenario_b_project_prompt(llm)   # 核心指标 1：项目 prompt 命中率
    ok_c = await scenario_c_tailtrim_orphan_safe(llm)  # 核心指标 2：TailTrim 孤儿保护

    print("\n" + "=" * 70)
    print(f"SUMMARY:  A=观测  "
          f"B={'PASS' if ok_b else 'FAIL'}  "
          f"C={'PASS' if ok_c else 'FAIL'}")
    print("=" * 70)

    # Exit code 只看 B/C（持续指标）；A 是 Ch5 教学演示，不是回归门控
    return 0 if (ok_b and ok_c) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
