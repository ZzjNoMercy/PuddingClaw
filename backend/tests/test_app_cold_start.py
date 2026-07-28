from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_app_imports_in_fresh_python_process() -> None:
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-c", "import app; print('APP_IMPORT_OK')"],
        cwd=backend_dir,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "APP_IMPORT_OK" in result.stdout
