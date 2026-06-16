"""Eval API — Skills evaluation review endpoints."""

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

# Security: only allow safe path segments
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")

BASE_DIR = Path(__file__).resolve().parent.parent


def _validate_segment(value: str, label: str) -> str:
    if not _SAFE_NAME.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")
    return value


# ── 1. List eval iterations ──────────────────────────────

@router.get("/skills/{name}/evals")
async def list_eval_iterations(name: str):
    """Scan skills/{name}/evals/ and return iteration summaries."""
    _validate_segment(name, "skill name")

    evals_dir = BASE_DIR / "skills" / name / "evals"
    if not evals_dir.is_dir():
        return {"iterations": []}

    iterations = []
    for child in sorted(evals_dir.iterdir()):
        if not child.is_dir():
            continue
        iter_id = child.name
        if not _SAFE_NAME.match(iter_id):
            continue

        # Count eval subdirectories
        eval_dirs = [d for d in child.iterdir() if d.is_dir() and _SAFE_NAME.match(d.name)]
        eval_count = len(eval_dirs)

        # Check benchmark
        benchmark_path = child / "benchmark.json"
        has_benchmark = benchmark_path.is_file()

        # Compute overall pass rate from benchmark
        overall_pass_rate = None
        if has_benchmark:
            try:
                bm = json.loads(benchmark_path.read_text(encoding="utf-8"))
                if "pass_rate" in bm:
                    overall_pass_rate = bm["pass_rate"]
                elif "results" in bm and isinstance(bm["results"], list):
                    total = len(bm["results"])
                    passed = sum(1 for r in bm["results"] if r.get("passed") or r.get("pass"))
                    overall_pass_rate = round(passed / total, 4) if total > 0 else 0
            except Exception:
                pass

        # Get timestamp from directory stat
        timestamp = child.stat().st_mtime

        iterations.append({
            "id": iter_id,
            "timestamp": timestamp,
            "eval_count": eval_count,
            "has_benchmark": has_benchmark,
            "overall_pass_rate": overall_pass_rate,
        })

    # Sort by timestamp descending (newest first)
    iterations.sort(key=lambda x: x["timestamp"], reverse=True)

    return {"iterations": iterations}


# ── 2. Get benchmark data ────────────────────────────────

@router.get("/skills/{name}/evals/{iteration}/benchmark")
async def get_benchmark(name: str, iteration: str):
    """Return benchmark.json for a given iteration."""
    _validate_segment(name, "skill name")
    _validate_segment(iteration, "iteration id")

    benchmark_path = BASE_DIR / "skills" / name / "evals" / iteration / "benchmark.json"
    if not benchmark_path.is_file():
        raise HTTPException(status_code=404, detail="Benchmark not found")

    try:
        data = json.loads(benchmark_path.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Malformed benchmark.json")


# ── 3. Get grading data ──────────────────────────────────

@router.get("/skills/{name}/evals/{iteration}/{eval_id}/grading")
async def get_grading(name: str, iteration: str, eval_id: str):
    """Return grading.json for a specific eval within an iteration."""
    _validate_segment(name, "skill name")
    _validate_segment(iteration, "iteration id")
    _validate_segment(eval_id, "eval id")

    grading_path = (
        BASE_DIR / "skills" / name / "evals" / iteration / eval_id / "grading.json"
    )
    if not grading_path.is_file():
        raise HTTPException(status_code=404, detail="Grading not found")

    try:
        data = json.loads(grading_path.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Malformed grading.json")


# ── 4. Save feedback / reviews ───────────────────────────

class FeedbackRequest(BaseModel):
    verdict: Literal["approve", "reject", "needs_work"]
    notes: str = ""
    tags: list[str] = []


@router.post("/skills/{name}/evals/{iteration}/feedback")
async def save_feedback(name: str, iteration: str, body: FeedbackRequest):
    """Save a review to reviews.json in the iteration directory."""
    _validate_segment(name, "skill name")
    _validate_segment(iteration, "iteration id")

    iter_dir = BASE_DIR / "skills" / name / "evals" / iteration
    if not iter_dir.is_dir():
        # Create directory if it doesn't exist yet
        iter_dir.mkdir(parents=True, exist_ok=True)

    reviews_path = iter_dir / "reviews.json"

    # Load existing reviews
    existing: list[dict[str, Any]] = []
    if reviews_path.is_file():
        try:
            existing = json.loads(reviews_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except Exception:
            existing = []

    review_entry = {
        "timestamp": time.time(),
        "verdict": body.verdict,
        "notes": body.notes,
        "tags": body.tags,
    }
    existing.append(review_entry)

    reviews_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {"success": True, "review": review_entry}


# ── 5. Save five-dimension eval result ───────────────────

class EvalResultRequest(BaseModel):
    skill_name: str
    timestamp: float
    total_score: int
    grade: str
    verdict_note: str = ""
    dimensions: list[dict] = []
    strengths: list[dict] = []
    weaknesses: list[dict] = []
    session_id: str = ""


@router.post("/skills/{name}/eval-result")
async def save_eval_result(name: str, body: EvalResultRequest, version: str = Query(default="")):
    """Save five-dimension evaluation result. Use ?version=v1.0 for versioned results."""
    _validate_segment(name, "skill name")

    if version and version != "current":
        # Validate version label format (allow dots for version labels like v1.0)
        if not re.match(r"^[a-zA-Z0-9._-]+$", version):
            raise HTTPException(status_code=400, detail=f"Invalid version: {version!r}")
        result_path = BASE_DIR / "skills" / name / "versions" / version / "five-dim-result.json"
        if not result_path.parent.is_dir():
            raise HTTPException(status_code=404, detail=f"Version '{version}' not found")
    else:
        evals_dir = BASE_DIR / "skills" / name / "evals"
        evals_dir.mkdir(parents=True, exist_ok=True)
        result_path = evals_dir / "five-dim-result.json"

    result_data = body.model_dump()
    result_path.write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"success": True}


# ── 6. Get five-dimension eval result ────────────────────

@router.get("/skills/{name}/eval-result")
async def get_eval_result(name: str, version: str = Query(default="")):
    """Get five-dimension evaluation result. Use ?version=v1.0 for versioned results."""
    _validate_segment(name, "skill name")

    if version and version != "current":
        if not re.match(r"^[a-zA-Z0-9._-]+$", version):
            raise HTTPException(status_code=400, detail=f"Invalid version: {version!r}")
        result_path = BASE_DIR / "skills" / name / "versions" / version / "five-dim-result.json"
    else:
        result_path = BASE_DIR / "skills" / name / "evals" / "five-dim-result.json"

    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="Eval result not found")

    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Malformed eval result")


# ── 7. List all eval results across versions ─────────────

@router.get("/skills/{name}/eval-results-list")
async def list_eval_results(name: str):
    """List all five-dimension eval results across current and versioned snapshots."""
    _validate_segment(name, "skill name")

    results = []

    # Check current (evals/five-dim-result.json)
    current_path = BASE_DIR / "skills" / name / "evals" / "five-dim-result.json"
    if current_path.is_file():
        try:
            data = json.loads(current_path.read_text(encoding="utf-8"))
            results.append({
                "version": "current",
                "total_score": data.get("total_score", 0),
                "grade": data.get("grade", ""),
                "timestamp": data.get("timestamp", 0),
            })
        except Exception:
            pass

    # Check each version directory
    versions_dir = BASE_DIR / "skills" / name / "versions"
    if versions_dir.is_dir():
        for ver_dir in sorted(versions_dir.iterdir()):
            if not ver_dir.is_dir():
                continue
            result_path = ver_dir / "five-dim-result.json"
            if result_path.is_file():
                try:
                    data = json.loads(result_path.read_text(encoding="utf-8"))
                    results.append({
                        "version": ver_dir.name,
                        "total_score": data.get("total_score", 0),
                        "grade": data.get("grade", ""),
                        "timestamp": data.get("timestamp", 0),
                    })
                except Exception:
                    pass

    # Sort by timestamp descending
    results.sort(key=lambda x: x["timestamp"], reverse=True)

    return {"results": results}
