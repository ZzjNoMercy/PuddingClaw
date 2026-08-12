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
        enabled_mcp = effective_mcp_server_names(mcp_config.get("enabled", []))
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
                print(f"🔌 MCP discovery warmed{retry_note}: {len(tools)} filtered tools")
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
async def _install_cli_runtime_in_background() -> None:
    """Install the optional CLI after the backend has become ready."""

    from cli_runtime import ensure_cli_runtime

    try:
        status = await asyncio.to_thread(ensure_cli_runtime, BASE_DIR)
        if status.get("installed"):
            print(f"🧩 Worker CLI ready: {status.get('command')} v{status.get('version')} ({status.get('path')})")
        else:
            print(
                "⚠️ Worker CLI remains unavailable; backend is still usable. "
                f"{status.get('install_message') or 'install it separately when needed.'}"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"⚠️ Worker CLI background setup failed; backend will continue: {exc}")


async def lifespan(app: FastAPI):
    """Startup: scan skills, initialize agent, build memory index."""
    import traceback

    print("🚀 Initializing PuddingClaw backend...")

    import capabilities
    from analytics.nl2sql.result_cleanup import query_result_cleanup_manager
    from analytics.semantic_assets import get_semantic_asset_registry
    from cli_runtime import detect_cli_runtime
    from db import init_database
    from evaluation.worker_manager import evaluation_worker_manager
    from graph.agent import agent_manager
    from graph.attachment_store import attachment_store
    from graph.deepagents_manager import deepagents_agent_manager
    from graph.session_manager import session_manager
    from knowledge.import_worker import knowledge_import_worker_manager
    from knowledge.portal_search import knowledge_catalog_watcher
    from knowledge.semantic_dimension_worker import semantic_dimension_build_worker_manager
    from projects.registry import project_registry
    from runtime_identity.migration import (
        migrate_definitions_and_data,
        migrate_home_layout,
        migrate_project_trust_registry,
        migrate_projects_and_memory,
        migrate_runtime_artifacts,
        migrate_runtime_home,
        migrate_workspace_artifacts,
    )
    from runtime_identity.paths import PuddingClawPaths
    from tools.skills_scanner import scan_skills
    from worker_access import worker_access_store

    user_paths = PuddingClawPaths.from_environment()
    user_paths.ensure_layout()
    migration = migrate_runtime_home(BASE_DIR, user_paths)
    definitions_data_migration = migrate_definitions_and_data(BASE_DIR, user_paths)
    projects_memory_migration = migrate_projects_and_memory(BASE_DIR, user_paths)
    project_trust_migration = migrate_project_trust_registry(user_paths)
    runtime_artifacts_migration = migrate_runtime_artifacts(BASE_DIR, user_paths)
    workspace_artifacts_migration = migrate_workspace_artifacts(BASE_DIR, user_paths)
    home_layout_migration = migrate_home_layout(user_paths)
    if migration.get("conflicts"):
        print(f"⚠️ Runtime migration retained {len(migration['conflicts'])} conflicts for review")
    if definitions_data_migration.get("conflicts"):
        print(
            "⚠️ Definitions/data migration retained "
            f"{len(definitions_data_migration['conflicts'])} conflicts for review"
        )
    if projects_memory_migration.get("conflicts"):
        print(
            "⚠️ Projects/memory migration retained "
            f"{len(projects_memory_migration['conflicts'])} conflicts for review"
        )
    if project_trust_migration.get("upgraded"):
        print(
            "🔐 Preserved trust for "
            f"{project_trust_migration['upgraded']} migrated projects"
        )
    if runtime_artifacts_migration.get("conflicts"):
        print(
            "⚠️ Runtime artifact migration retained "
            f"{len(runtime_artifacts_migration['conflicts'])} conflicts for review"
        )
    if workspace_artifacts_migration.get("conflicts"):
        print(
            "⚠️ Workspace artifact migration retained "
            f"{len(workspace_artifacts_migration['conflicts'])} conflicts for review"
        )
    if home_layout_migration.get("conflicts"):
        print(
            "⚠️ Home layout migration retained "
            f"{len(home_layout_migration['conflicts'])} conflicts for review"
        )
    scan_skills(
        BASE_DIR,
        user_root=user_paths.user_skills(),
        snapshot_path=user_paths.skill_management() / "SKILLS_SNAPSHOT.md",
    )
    semantic_assets = get_semantic_asset_registry(user_paths.user_definitions()).refresh()
    print(f"🧭 Semantic assets loaded: {semantic_assets.get('count', 0)}")
    project_registry.initialize(user_paths.root)
    attachment_store.initialize(
        user_paths.root,
    )
    knowledge_catalog_watcher.start(user_paths.root)
    # SQL Evidence catalog backfill needs the durable Session owner index.
    session_manager.initialize(sessions_dir=user_paths.sessions())
    worker_access_store.initialize(user_paths.root)
    cli_status = detect_cli_runtime(BASE_DIR)
    if not cli_status.get("installed"):
        print(
            "ℹ️ Worker CLI not ready yet; backend startup will continue. "
            f"Policy={cli_status.get('install_policy')}; "
            "an optional background setup may install it."
        )
        if cli_status.get("install_policy") in {"auto", "prompt"}:
            app.state.cli_runtime_install_task = asyncio.create_task(
                _install_cli_runtime_in_background(),
                name="puddingclaw-cli-runtime-setup",
            )
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
    # LEGACY compatibility bootstrap. /api/chat and one deep-research helper
    # still depend on it while they await migration; new flows must not do so.
    try:
        agent_manager.initialize(BASE_DIR, sessions_dir=user_paths.sessions())
    except Exception as e:
        print(f"⚠️ Legacy Chat compatibility runtime initialization failed: {e}")
        traceback.print_exc()
        print("ℹ️ Server will continue running; the maintained Agent runtime initializes separately.")
    try:
        deepagents_agent_manager.initialize(BASE_DIR, user_root=user_paths.root)
    except Exception as e:
        print(f"⚠️ DeepAgents initialization failed: {e}")
        traceback.print_exc()
        print("ℹ️ Server will continue running, but /api/agent requires DeepAgents runtime.")
    if db_ready:
        # LLM Wiki jobs use the Agent harness without creating a user-visible
        # conversation, so workers may only claim jobs after the harness owner
        # has been initialized.
        knowledge_import_worker_manager.start(user_paths.root)
        semantic_dimension_build_worker_manager.start(user_paths.root)

    print("✅ PuddingClaw backend ready")
    await evaluation_worker_manager.start_pending()
    try:
        yield
    finally:
        cli_task = getattr(app.state, "cli_runtime_install_task", None)
        if cli_task is not None and not cli_task.done():
            cli_task.cancel()
            await asyncio.gather(cli_task, return_exceptions=True)
        await evaluation_worker_manager.stop()
        await query_result_cleanup_manager.stop()
        await semantic_dimension_build_worker_manager.stop()
        await knowledge_import_worker_manager.stop()
        await knowledge_catalog_watcher.stop()


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

from api.agent import router as agent_router
from api.analytics import router as analytics_router
from api.attachments import router as attachments_router
from api.brain_schema import router as brain_schema_router
from api.capabilities import router as capabilities_router
from api.chat import router as chat_router  # LEGACY: compatibility only; no longer maintained.
from api.compress import router as compress_router
from api.config_api import router as config_router
from api.connectors import router as connectors_router
from api.database_sql_revisions import router as database_sql_revisions_router
from api.dimension_build_rules import router as dimension_build_rules_router
from api.eval_api import router as eval_router
from api.evaluation import router as evaluation_router
from api.files import router as files_router
from api.headless import router as headless_router
from api.headless import worker_access_router
from api.knowledge import router as knowledge_router
from api.llm_wiki import router as llm_wiki_router
from api.logical_dataset_rules import router as logical_dataset_rules_router
from api.mcp import router as mcp_router
from api.permissions import router as permissions_router
from api.projects import router as projects_router
from api.read_later import router as read_later_router
from api.sessions import router as sessions_router
from api.skill_plans import router as skill_plans_router
from api.skill_secret_requests import router as skill_secret_requests_router
from api.skills_api import router as skills_api_router
from api.stats_api import router as stats_router
from api.tokens import router as tokens_router
from api.toolchains import router as toolchains_router
from api.user_input_requests import router as user_input_requests_router
from api.kernel_fallback_requests import router as kernel_fallback_requests_router
from api.web_search_config import router as web_search_config_router

app.include_router(chat_router, prefix="/api")  # LEGACY: new conversations use /api/agent.
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
app.include_router(skill_secret_requests_router, prefix="/api")
app.include_router(attachments_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(dimension_build_rules_router, prefix="/api")
app.include_router(logical_dataset_rules_router, prefix="/api")
app.include_router(database_sql_revisions_router, prefix="/api")
app.include_router(user_input_requests_router, prefix="/api")
app.include_router(kernel_fallback_requests_router, prefix="/api")
app.include_router(connectors_router, prefix="/api")
app.include_router(toolchains_router, prefix="/api")
app.include_router(brain_schema_router, prefix="/api")
app.include_router(llm_wiki_router, prefix="/api")
app.include_router(read_later_router, prefix="/api")
app.include_router(headless_router, prefix="/api")
app.include_router(worker_access_router, prefix="/api")
app.include_router(web_search_config_router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "PuddingClaw", "version": "0.1.0", "status": "running"}
