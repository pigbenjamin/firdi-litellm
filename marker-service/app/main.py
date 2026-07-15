from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .marker_worker import ConvertJob, MarkerWorker, MARKER_TIMEOUT

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("marker.main")

# Requests must resolve under this directory — same shared PVC ingest-worker
# writes uploads to. Rejects path traversal / out-of-bounds paths from callers
# instead of trusting pdf_path/out_dir verbatim (see README section on the
# subprocess+shell design this replaced).
ALLOWED_BASE_DIR = Path(os.getenv("MARKER_ALLOWED_BASE_DIR", "/data")).resolve()

# Give the client-facing wait a little headroom over the worker's own
# per-job timeout so a timeout from the worker (with a real error message)
# wins over a bare queue-wait TimeoutError.
SUBMIT_TIMEOUT = MARKER_TIMEOUT + 60

worker = MarkerWorker()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    worker.start()
    yield


app = FastAPI(title="Marker Service", lifespan=_lifespan)


def _resolve_under_base(raw_path: str, field_name: str) -> Path:
    resolved = Path(raw_path).resolve()
    try:
        resolved.relative_to(ALLOWED_BASE_DIR)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must resolve under {ALLOWED_BASE_DIR}, got {resolved}",
        )
    return resolved


# ── OpenAI-compatible endpoint (for LiteLLM routing) ─────────────────────────
#
# message content format (JSON string):
#   { "pdf_path": "...", "doc_id": "...", "out_dir": "...", "job_id": "..." }
#
# response: choices[0].message.content = md_path

@app.post("/v1/chat/completions")
def chat_completions(body: Dict[str, Any]):
    messages: List[Dict] = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    raw = messages[-1].get("content", "")
    try:
        params = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="message content must be a JSON string")

    pdf_path: str = params.get("pdf_path", "")
    out_dir: str = params.get("out_dir", "")
    doc_id: str = params.get("doc_id") or Path(pdf_path).stem
    job_id: str = params.get("job_id") or f"litellm-{int(time.time())}"

    if not pdf_path or not out_dir:
        raise HTTPException(status_code=400, detail="pdf_path and out_dir are required")

    resolved_pdf = _resolve_under_base(pdf_path, "pdf_path")
    resolved_out = _resolve_under_base(out_dir, "out_dir")

    if not worker.is_ready():
        raise HTTPException(status_code=503, detail="marker models still loading, retry shortly")

    job = ConvertJob(job_id=job_id, doc_id=doc_id, pdf_path=str(resolved_pdf), out_dir=str(resolved_out))

    t0 = time.perf_counter()
    try:
        md_path = worker.submit(job, timeout=SUBMIT_TIMEOUT)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    created = int(t0)
    logger.info("job %s done in %.1fs", job_id, time.perf_counter() - t0)
    return {
        "id": f"marker-{created}",
        "object": "chat.completion",
        "created": created,
        "model": body.get("model", "marker-pdf-to-md"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": md_path},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/healthz")
def health():
    ready = worker.is_ready()
    stuck = worker.is_stuck()
    body = {
        "status": "ok" if (ready and not stuck) else "unavailable",
        "models_loaded": ready,
        "job_stuck": stuck,
        "queue_depth": worker.queue_depth(),
        "current_job_elapsed_s": worker.current_job_elapsed(),
    }
    return JSONResponse(status_code=200 if (ready and not stuck) else 503, content=body)
