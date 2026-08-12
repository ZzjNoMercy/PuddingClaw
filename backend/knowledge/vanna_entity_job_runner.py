"""Subprocess runner for Vanna entity import jobs.

The FastAPI process must stay responsive while large entity dictionaries are
embedded and written to Milvus. This runner executes the heavy Vanna path in a
separate Python process and writes progress back to the shared catalog DB.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from db import get_sessionmaker
from knowledge.import_jobs import mark_job_failed, process_vanna_entity_import_job
from knowledge.models import KnowledgeImportJob
from runtime_identity.paths import PuddingClawPaths

logger = logging.getLogger(__name__)


async def run_job(job_id: str, *, base_dir: Path) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        job = await session.get(KnowledgeImportJob, job_id)
        if job is None:
            logger.error("[vanna-entity-runner] job not found: %s", job_id)
            return 2

        try:
            await process_vanna_entity_import_job(session, base_dir=base_dir, job=job)
        except Exception as exc:  # noqa: BLE001 - persist any runner failure to job state
            logger.exception("[vanna-entity-runner] failed job_id=%s", job_id)
            await mark_job_failed(session, job, exc)
            return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Vanna entity import job in an isolated subprocess.")
    parser.add_argument("job_id")
    parser.add_argument(
        "--base-dir",
        default=str(PuddingClawPaths.from_environment().root),
        help="PuddingClaw Home root; defaults to PUDDINGCLAW_HOME.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return asyncio.run(run_job(args.job_id, base_dir=Path(args.base_dir)))


if __name__ == "__main__":
    raise SystemExit(main())
