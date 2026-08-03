"""CLI entry point for one evaluation Experiment child process."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .repository import get_evaluation_repository
from .runner import EvaluationRunner
from .settings import get_evaluation_settings_store


async def _main(experiment_id: str) -> None:
    backend_dir = Path(__file__).resolve().parent.parent
    runner = EvaluationRunner(
        get_evaluation_repository(),
        get_evaluation_settings_store().load(),
        backend_dir,
    )
    await runner.run(experiment_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.experiment_id))


if __name__ == "__main__":
    main()
