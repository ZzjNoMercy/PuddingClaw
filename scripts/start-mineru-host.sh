#!/bin/bash
# Fallback: start MinerU service on the host using the existing conda environment.
# This is useful when the Docker image build is blocked by network/proxy issues.
# The backend container can reach this service at host.docker.internal:8002.

set -e

CONDA_ENV="mineru"
PORT="${MINERU_PORT:-8002}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)/mineru"

echo "Starting MinerU service on host port $PORT using conda env '$CONDA_ENV'..."

# Locate conda
if [ -z "$CONDA_EXE" ]; then
    if [ -f "$HOME/miniconda3/bin/conda" ]; then
        CONDA_EXE="$HOME/miniconda3/bin/conda"
    elif [ -f "$HOME/anaconda3/bin/conda" ]; then
        CONDA_EXE="$HOME/anaconda3/bin/conda"
    elif command -v conda >/dev/null 2>&1; then
        CONDA_EXE="$(command -v conda)"
    else
        echo "ERROR: conda not found. Please install conda or set CONDA_EXE."
        exit 1
    fi
fi

# Start uvicorn with the conda env's Python
exec "$CONDA_EXE" run -n "$CONDA_ENV" --no-capture-output \
    python -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --app-dir "$APP_DIR"
