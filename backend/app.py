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
    from tools.skills_scanner import scan_skills
    from graph.agent import agent_manager
    from graph.memory_indexer import get_memory_indexer

    scan_skills(BASE_DIR)
    agent_manager.initialize(BASE_DIR)

    # Initialize memory indexer only when RAG mode is enabled (requires Embedding API)
    from config import get_rag_mode
    if get_rag_mode():
        indexer = get_memory_indexer(BASE_DIR)
        indexer.rebuild_index()
    else:
        print("ℹ️ RAG mode disabled, skipping memory index build")

    print("✅ PuddingClaw backend ready")
    yield


app = FastAPI(title="PuddingClaw", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.chat import router as chat_router
from api.files import router as files_router
from api.sessions import router as sessions_router
from api.tokens import router as tokens_router
from api.compress import router as compress_router
from api.config_api import router as config_router
from api.eval_api import router as eval_router
from api.skills_api import router as skills_router
from api.stats_api import router as stats_router

app.include_router(chat_router, prefix="/api")
app.include_router(skills_router, prefix="/api")  # Must come before files_router
app.include_router(files_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(tokens_router, prefix="/api")
app.include_router(compress_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
app.include_router(stats_router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "PuddingClaw", "version": "0.1.0", "status": "running"}
