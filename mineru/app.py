"""MinerU PDF parsing service.

Provides a simple FastAPI wrapper around the `mineru` CLI so that other
services in the Docker Compose stack can parse PDFs to markdown via HTTP.
"""

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="MinerU PDF Parser", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse_pdf(
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
):
    """Parse a PDF file or URL into markdown.

    Returns JSON: {"markdown": "...", "source": "file|url"}
    """
    if not file and not url:
        return JSONResponse(
            status_code=400,
            content={"error": "Either file or url must be provided"},
        )

    work_dir = tempfile.mkdtemp(prefix="mineru_")
    input_path = Path(work_dir) / "input.pdf"
    output_dir = Path(work_dir) / "output"

    try:
        if file:
            with open(input_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            source = "file"
        else:
            # Download URL to local file using urllib (no curl in image)
            import urllib.request
            try:
                urllib.request.urlretrieve(url, str(input_path))
            except Exception as e:
                return JSONResponse(
                    status_code=502,
                    content={"error": f"Failed to download PDF: {str(e)}"},
                )
            source = "url"

        # Run MinerU CLI
        cmd = [
            "mineru",
            "-p",
            str(input_path),
            "-o",
            str(output_dir),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode != 0:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "MinerU parsing failed",
                    "stderr": proc.stderr,
                    "stdout": proc.stdout,
                },
            )

        # Find generated markdown file
        md_files = glob.glob(str(output_dir / "**" / "*.md"), recursive=True)
        if not md_files:
            return JSONResponse(
                status_code=500,
                content={"error": "No markdown output generated"},
            )

        # Prefer the main markdown file; MinerU often names it {input}_content.md
        md_files.sort(key=lambda p: (len(p), p))
        md_content = Path(md_files[0]).read_text(encoding="utf-8")

        return {
            "markdown": md_content,
            "source": source,
            "output_files": md_files,
        }

    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=504,
            content={"error": "MinerU parsing timed out"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Unexpected error: {str(e)}"},
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
