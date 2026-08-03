"""PuddingClaw Backend — FastAPI Entry Point"""

import asyncio
import os
from builtins import BaseExceptionGroup
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _exception_leaf_summary(exc: BaseException) -> str:
    """Expose useful TaskGroup leaf errors without dumping tracebacks."""

    if isinstance(exc, BaseExceptionGroup):
        parts = [_exception_leaf_summary(item) for item in exc.exceptions]
        return "; ".join(dict.fromkeys(part for part in parts if part))
    detail = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def _warm_mcp_discovery(
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 0.25,
) -> None:
    """Prime MCP metadata while keeping startup failures non-fatal.

    A stdio MCP server can close its first cold-start handshake while its
    runtime is still settling.  Retry that transient once before surfacing a
    warning; discovery failures are not cached, so the retry is a clean spawn.
    """

    enabled_mcp: list[str] = []
    try:
        import config
        from mcp_clients import load_filtered_mcp_tools
        from mcp_clients.servers import effective_mcp_server_names

        mcp_config = config.load_config().get("mcp", {})
        enabled_mcp = effective_mcp_server_names(
            mcp_config.get("enabled", []),
            auto_enable_gbrain=bool(mcp_config.get("auto_enable_gbrain", False)),
        )
        if not enabled_mcp:
            return
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                tools = await load_filtered_mcp_tools(enabled_mcp)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(max(0.0, retry_delay_seconds))
            else:
                retry_note = " after one cold-start retry" if attempt > 1 else ""
                print(
                    f"🔌 MCP discovery warmed{retry_note}: "
                    f"{len(tools)} filtered tools"
                )
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Discovery failures are never cached; the first MCP-dependent Agent
        # request retries on demand.  Spell this out because upstream stdio
        # servers may print setup instructions that do not describe the
        # durable PuddingClaw runtime accurately.
        cause = _exception_leaf_summary(exc)
        print(
            "⚠️ MCP discovery warm-up did not complete; backend startup will continue "
            f"and first use will retry. Cause: {cause}"
        )
        if "gbrain" in enabled_mcp:
            from mcp_clients.servers import gbrain_runtime_status

            status = gbrain_runtime_status()
            if status.get("ready"):
                print(
                    "ℹ️ Dedicated GBrain configuration and Schema Pack are present. "
                    "This is only an MCP metadata warm-up failure; do not run `gbrain init`."
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: scan skills, initialize agent, build memory index."""
    import traceback

    import capabilities
    from analytics.nl2sql.result_cleanup import query_result_cleanup_manager
    from analytics.semantic_assets import get_semantic_asset_registry
    from db import init_database
    from evaluation.worker_manager import evaluation_worker_manager
    from graph.agent import agent_manager
    from graph.attachment_store import attachment_store
    from graph.deepagents_manager import deepagents_agent_manager
    from graph.memory_indexer import get_memory_indexer
    from graph.session_manager import session_manager
    from knowledge.import_worker import knowledge_import_worker_manager
    from knowledge.semantic_dimension_worker import semantic_dimension_build_worker_manager
    from projects.registry import project_registry
    from tools.skills_scanner import scan_skills

    scan_skills(BASE_DIR)
    semantic_assets = get_semantic_asset_registry(BASE_DIR).refresh()
    print(f"🧭 Semantic assets loaded: {semantic_assets.get('count', 0)}")
    project_registry.initialize(BASE_DIR)
    attachment_store.initialize(BASE_DIR)
    # SQL Evidence catalog backfill needs the durable Session owner index.
    session_manager.initialize(BASE_DIR)
    db_ready = await init_database()
    if db_ready:
        print("🗄️ Knowledge catalog database ready")
        query_result_cleanup_manager.start()
    else:
        print("⚠️ Knowledge catalog database unavailable; knowledge management API will report degraded status")
    # Confirm database startup before spawning database-backed stdio MCP
    # servers. Keep discovery ahead of capability detection because Milvus can
    # create gRPC worker threads and forking after that emits unsafe-fork
    # warnings. Awaiting discovery here also keeps first-use latency out of the
    # first Agent request whenever warm-up succeeds.
    await _warm_mcp_discovery()
    caps = await capabilities.detect_capabilities(force=True)
    print(f"🔌 Capabilities: {caps.to_dict()}")
    try:
        agent_manager.initialize(BASE_DIR)
    except Exception as e:
        print(f"⚠️ Chat Agent initialization failed (missing LLM API key?): {e}")
        traceback.print_exc()
        print("ℹ️ Server will continue running, but chat features require a valid LLM API key.")
    try:
        deepagents_agent_manager.initialize(BASE_DIR)
    except Exception as e:
        print(f"⚠️ DeepAgents initialization failed: {e}")
        traceback.print_exc()
        print("ℹ️ Server will continue running, but /api/agent requires DeepAgents runtime.")
    if db_ready:
        # LLM Wiki jobs use the Agent harness without creating a user-visible
        # conversation, so workers may only claim jobs after the harness owner
        # has been initialized.
        knowledge_import_worker_manager.start(BASE_DIR)
        semantic_dimension_build_worker_manager.start(BASE_DIR)

    # Initialize memory indexer only when RAG mode is enabled (requires Embedding API)
    from config import get_rag_mode

    if get_rag_mode():
        try:
            indexer = get_memory_indexer(BASE_DIR)
            indexer.rebuild_index()
        except Exception as e:
            print(f"⚠️ Memory index build failed: {e}")
    else:
        print("ℹ️ RAG mode disabled, skipping memory index build")

    print("✅ PuddingClaw backend ready")
    await evaluation_worker_manager.start_pending()
    try:
        yield
    finally:
        await evaluation_worker_manager.stop()
        await query_result_cleanup_manager.stop()
        await semantic_dimension_build_worker_manager.stop()
        await knowledge_import_worker_manager.stop()


app = FastAPI(title="PuddingClaw", version="0.1.0", lifespan=lifespan)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.chat import router as chat_router
from api.agent import router as agent_router
from api.files import router as files_router
from api.sessions import router as sessions_router
from api.tokens import router as tokens_router
from api.compress import router as compress_router
from api.config_api import router as config_router
from api.eval_api import router as eval_router
from api.evaluation import router as evaluation_router
from api.skills_api import router as skills_api_router
from api.stats_api import router as stats_router
from api.mcp import router as mcp_router
from api.capabilities import router as capabilities_router
from api.projects import router as projects_router
from api.permissions import router as permissions_router
from api.skill_plans import router as skill_plans_router
from api.attachments import router as attachments_router
from api.knowledge import router as knowledge_router
from api.analytics import router as analytics_router
from api.dimension_build_rules import router as dimension_build_rules_router
from api.logical_dataset_rules import router as logical_dataset_rules_router
from api.database_sql_revisions import router as database_sql_revisions_router
from api.user_input_requests import router as user_input_requests_router
from api.connectors import router as connectors_router
from api.brain_schema import router as brain_schema_router
from api.llm_wiki import router as llm_wiki_router

app.include_router(chat_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(skills_api_router, prefix="/api")  # Must come before files_router
app.include_router(files_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(tokens_router, prefix="/api")
app.include_router(compress_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
app.include_router(evaluation_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(mcp_router, prefix="/api")
app.include_router(capabilities_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(permissions_router, prefix="/api")
app.include_router(skill_plans_router, prefix="/api")
app.include_router(attachments_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(dimension_build_rules_router, prefix="/api")
app.include_router(logical_dataset_rules_router, prefix="/api")
app.include_router(database_sql_revisions_router, prefix="/api")
app.include_router(user_input_requests_router, prefix="/api")
app.include_router(connectors_router, prefix="/api")
app.include_router(brain_schema_router, prefix="/api")
app.include_router(llm_wiki_router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "PuddingClaw", "version": "0.1.0", "status": "running"}
