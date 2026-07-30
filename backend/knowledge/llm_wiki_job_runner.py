"""Run an LLM Wiki Ingest inside the durable knowledge task queue."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.import_jobs import update_job_progress
from knowledge.llm_wiki import get_llm_wiki_service
from knowledge.llm_wiki_compiler_agent import LlmWikiCompilerAgent
from knowledge.models import KnowledgeImportEvent, KnowledgeImportJob
from knowledge.service import KnowledgeServiceError


async def process_llm_wiki_ingest_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    job: KnowledgeImportJob,
) -> KnowledgeImportJob:
    metadata = dict(job.job_metadata or {})
    raw_paths = [str(path) for path in metadata.get("raw_paths") or [] if str(path)]
    expected_bundle_hash = str(metadata.get("bundle_hash") or "")
    compiler_model_id = str(metadata.get("compiler_model_id") or "")
    expected_raw_hashes = {
        str(path): str(digest)
        for path, digest in (metadata.get("raw_hashes") or {}).items()
        if str(path)
    }
    if not raw_paths or not expected_bundle_hash:
        raise KnowledgeServiceError("LLM Wiki 编译任务缺少不可变输入快照。")

    wiki = get_llm_wiki_service(base_dir)
    context = wiki.operation_context("ingest", raw_paths=raw_paths)
    active_bundle_hash = str((context.get("schema_bundle") or {}).get("bundle_hash") or "")
    if active_bundle_hash != expected_bundle_hash:
        raise KnowledgeServiceError("活动 Schema Bundle 已变化，请刷新工作台后重新提交编译任务。")
    active_hashes = {
        str(item.get("snapshot_path") or ""): str(item.get("sha256") or "")
        for item in context.get("raw_manifest") or []
        if isinstance(item, dict)
    }
    changed_raw = [path for path in raw_paths if active_hashes.get(path) != expected_raw_hashes.get(path)]
    if changed_raw:
        raise KnowledgeServiceError(f"Raw 快照已变化或完整性异常，请重新提交：{', '.join(changed_raw)}")

    await update_job_progress(
        session,
        job,
        step="context_ready",
        progress=15,
        message="已锁定 AGENTS.md、Schema Bundle 与 Raw 快照",
    )

    prompt = "\n\n".join(
        [
            "执行一次完整的 LLM Wiki Ingest。这是任务中心创建的后台编译任务，不要请求用户交互。",
            "必须先调用 llm_wiki_context(operation=ingest)，只读取下面精确选择的不可变 raw_paths；按照返回的 AGENTS.md 与活动 Schema，将 Raw 编译成可读、可追溯且互相链接的 Wiki 页面。",
            "完成后调用 llm_wiki_publish 提交完整页面；页面 slug 和 wikilink 都相对于 wiki/ 根目录，必须使用 concepts/<slug> 等 [[<type-directory>/<slug>]] 完整路径，不得再次添加 wiki/。frontmatter 的 sources 必须逐字复制 context 返回的 snapshot_path，不得添加 raw/ 前缀。确保 frontmatter、index 和 append-only log 均通过校验；最后必须调用 llm_wiki_lint 报告结果。不要使用通用文件工具写 raw/、wiki/、index.md 或 log.md。",
            f"compiler_model: {metadata.get('compiler_model') or compiler_model_id or 'agent-default'}",
            f"raw_paths: {json.dumps(raw_paths, ensure_ascii=False)}",
        ]
    )

    async def on_tool_event(phase: str, tool: str, _payload: dict[str, Any]) -> None:
        if phase != "start":
            return
        if tool == "llm_wiki_context":
            await update_job_progress(
                session, job, step="loading_context", progress=22, message="专属 Wiki Agent 正在读取编译契约与输入快照"
            )
        elif tool == "llm_wiki_publish":
            await update_job_progress(
                session, job, step="publishing", progress=68, message="专属 Wiki Agent 已生成页面，正在发布"
            )
        elif tool == "llm_wiki_lint":
            await update_job_progress(
                session, job, step="linting", progress=88, message="专属 Wiki Agent 正在执行 Lint"
            )

    try:
        run_result = await LlmWikiCompilerAgent(
            base_dir=base_dir,
            model_id=compiler_model_id,
        ).run(prompt, job_id=job.id, raw_paths=raw_paths, on_tool_event=on_tool_event)
        called = run_result["called"]
        final_outcome = {
            "outcome": run_result.get("outcome") or "completed",
            "final_text": run_result.get("final_text") or "",
            "runtime": "llm_wiki_compiler_agent",
        }

        deterministic_lint = wiki.lint()
        if not deterministic_lint.get("ok"):
            raise KnowledgeServiceError("发布后的 Wiki Lint 未通过。")
        workspace = wiki.workspace_status()
        selected = {item["snapshot_path"]: item for item in workspace.get("raw") or [] if isinstance(item, dict)}
        not_compiled = [path for path in raw_paths if not selected.get(path, {}).get("compiled")]
        if not_compiled:
            raise KnowledgeServiceError(f"发布完成但 Raw 未形成成功编译凭据：{', '.join(not_compiled)}")

        publish_result = called["llm_wiki_publish"]
        published_pages = publish_result.get("pages") or publish_result.get("published_pages") or []
        job.status = "succeeded"
        job.current_step = "done"
        job.progress = 100
        job.finished_at = datetime.now(timezone.utc)
        job.error_message = None
        job.job_metadata = {
            **metadata,
            "published_pages": published_pages,
            "publish_result": publish_result,
            "lint_ok": True,
            "lint_result": deterministic_lint,
            "run_outcome": final_outcome,
        }
        session.add(
            KnowledgeImportEvent(
                job_id=job.id,
                level="info",
                message=f"LLM Wiki 编译完成，共处理 {len(raw_paths)} 个 Raw 快照",
                event_metadata={"published_pages": published_pages, "lint_ok": True},
            )
        )
        await session.commit()
        await session.refresh(job)
        return job
    except RuntimeError as exc:
        raise KnowledgeServiceError(str(exc)) from exc
