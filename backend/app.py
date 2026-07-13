"""PuddingClaw Backend — FastAPI Entry Point"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: scan skills, initialize agent, build memory index."""
    import traceback
    from db import init_database
    from tools.skills_scanner import scan_skills
    from graph.agent import agent_manager
    from graph.deepagents_manager import deepagents_agent_manager
    from graph.attachment_store import attachment_store
    from graph.memory_indexer import get_memory_indexer
    from knowledge.import_worker import knowledge_import_worker_manager
    from knowledge.semantic_dimension_worker import semantic_dimension_build_worker_manager
    from projects.registry import project_registry
    from analytics.semantic_assets import get_semantic_asset_registry
    import capabilities

    scan_skills(BASE_DIR)
    semantic_assets = get_semantic_asset_registry(BASE_DIR).refresh()
    print(f"🧭 Semantic assets loaded: {semantic_assets.get('count', 0)}")
    project_registry.initialize(BASE_DIR)
    attachment_store.initialize(BASE_DIR)
    db_ready = await init_database()
    if db_ready:
        print("🗄️ Knowledge catalog database ready")
        knowledge_import_worker_manager.start(BASE_DIR)
        semantic_dimension_build_worker_manager.start(BASE_DIR)
    else:
        print("⚠️ Knowledge catalog database unavailable; knowledge management API will report degraded status")
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
    try:
        yield
    finally:
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
from api.skills_api import router as skills_api_router
from api.stats_api import router as stats_router
from api.mcp import router as mcp_router
from api.capabilities import router as capabilities_router
from api.projects import router as projects_router
from api.permissions import router as permissions_router
from api.attachments import router as attachments_router
from api.knowledge import router as knowledge_router
from api.analytics import router as analytics_router
from api.dimension_build_rules import router as dimension_build_rules_router
from api.logical_dataset_rules import router as logical_dataset_rules_router
from api.database_sql_revisions import router as database_sql_revisions_router

app.include_router(chat_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(skills_api_router, prefix="/api")  # Must come before files_router
app.include_router(files_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(tokens_router, prefix="/api")
app.include_router(compress_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(mcp_router, prefix="/api")
app.include_router(capabilities_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(permissions_router, prefix="/api")
app.include_router(attachments_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(dimension_build_rules_router, prefix="/api")
app.include_router(logical_dataset_rules_router, prefix="/api")
app.include_router(database_sql_revisions_router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "PuddingClaw", "version": "0.1.0", "status": "running"}
