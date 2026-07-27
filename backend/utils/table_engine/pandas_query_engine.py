"""PuddingClaw's lightweight PandasQueryEngine."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any

from config import get_fallback_llm_config

from .errors import PandasQueryEngineError
from .executor import render_value, safe_json
from .parser import extract_json_object
from .profiler import profile_dataframe
from .prompts import (
    ANSWER_SYNTHESIS_SYSTEM_PROMPT,
    CODE_GENERATION_SYSTEM_PROMPT,
    build_answer_synthesis_prompt,
    build_code_generation_prompt,
)
from .runner import InProcessPandasRunner, PandasCodeRunner


@dataclass
class PandasQueryEngineResult:
    answer: str
    code: str
    result_preview: str
    raw_result: Any
    profile: dict[str, Any]
    plan_explanation: str | None = None
    retries: int = 0
    semantic_context: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        metadata = {
            "engine": "puddingclaw_pandas_query_engine",
            "generated_code": self.code,
            "plan_explanation": self.plan_explanation,
            "result_preview": self.result_preview,
            "raw_result": safe_json(self.raw_result),
            "retries": self.retries,
        }
        if self.semantic_context:
            metadata["semantic_context_id"] = self.semantic_context.get("context_id")
            metadata["semantic_context_hash"] = self.semantic_context.get("content_hash")
            metadata["semantic_asset_ids"] = [
                str(item.get("id") or "")
                for key in ("semantic_assets", "references")
                for item in self.semantic_context.get(key) or []
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
        return metadata


def _openai_client_config() -> dict[str, Any]:
    cfg = get_fallback_llm_config()
    api_key = str(cfg.get("api_key") or "").strip()
    model = str(cfg.get("model") or "deepseek-chat").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    if base_url and cfg.get("provider") == "deepseek" and not base_url.rstrip("/").endswith("/v1"):
        base_url = f"{base_url.rstrip('/')}/v1"
    if not api_key:
        raise PandasQueryEngineError("LLM API Key 未配置，无法进行表格自然语言分析。请先在设置里配置模型密钥。")
    return {
        "api_key": api_key,
        "base_url": base_url or None,
        "model": model,
        "temperature": float(cfg.get("temperature", 0.1) or 0.1),
    }


class PuddingClawPandasQueryEngine:
    """Natural-language DataFrame query engine for local knowledge tables."""

    def __init__(
        self,
        df: Any,
        *,
        preview_rows: int = 5,
        max_retries: int = 1,
        runner: PandasCodeRunner | None = None,
        semantic_context: dict[str, Any] | None = None,
    ):
        self.df = df
        self.preview_rows = preview_rows
        self.max_retries = max(0, max_retries)
        self.profile = profile_dataframe(df, preview_rows=preview_rows)
        self.runner = runner or InProcessPandasRunner()
        self.semantic_context = semantic_context or {}

    def _client(self):
        try:
            import openai
        except ImportError as exc:
            raise PandasQueryEngineError(f"缺少 openai 依赖：{exc}") from exc
        cfg = _openai_client_config()
        kwargs = {"api_key": cfg["api_key"]}
        if cfg.get("base_url"):
            kwargs["base_url"] = cfg["base_url"]
        return openai.OpenAI(**kwargs), cfg

    def _generate_code(self, query: str, *, previous_error: str | None = None, previous_code: str | None = None) -> dict[str, Any]:
        client, cfg = self._client()
        completion = client.chat.completions.create(
            model=cfg["model"],
            temperature=0,
            messages=[
                {"role": "system", "content": CODE_GENERATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_code_generation_prompt(
                        query=query,
                        profile=self.profile,
                        semantic_context=self.semantic_context,
                        previous_error=previous_error,
                        previous_code=previous_code,
                    ),
                },
            ],
        )
        return extract_json_object(completion.choices[0].message.content or "")

    def _synthesize_answer(self, query: str, *, code: str, rendered_result: str) -> str:
        client, cfg = self._client()
        completion = client.chat.completions.create(
            model=cfg["model"],
            temperature=0,
            messages=[
                {"role": "system", "content": ANSWER_SYNTHESIS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_answer_synthesis_prompt(
                        query=query,
                        code=code,
                        rendered_result=rendered_result,
                        semantic_context=self.semantic_context,
                    ),
                },
            ],
        )
        return completion.choices[0].message.content or rendered_result

    def query(self, query: str) -> PandasQueryEngineResult:
        previous_error: str | None = None
        previous_code: str | None = None
        last_plan: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            plan = self._generate_code(query, previous_error=previous_error, previous_code=previous_code)
            last_plan = plan
            code = str(plan.get("code") or "").strip()
            if not code:
                raise PandasQueryEngineError("模型没有生成 pandas 代码。")
            try:
                raw_result = self.runner.run(self.df, code)
                rendered_result = render_value(raw_result)
                answer = self._synthesize_answer(query, code=code, rendered_result=rendered_result)
                return PandasQueryEngineResult(
                    answer=answer,
                    code=code,
                    result_preview=rendered_result,
                    raw_result=raw_result,
                    profile=self.profile,
                    plan_explanation=plan.get("explanation"),
                    retries=attempt,
                    semantic_context=self.semantic_context,
                )
            except Exception as exc:
                previous_code = code
                previous_error = "".join(traceback.format_exception_only(type(exc), exc))
                if attempt >= self.max_retries:
                    raise PandasQueryEngineError(
                        f"pandas 代码执行失败：{previous_error.strip()}；代码：{previous_code}"
                    ) from exc

        raise PandasQueryEngineError(f"表格分析失败：{last_plan}")
