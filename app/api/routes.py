"""
HTTP routes for the transcription API (all under settings.API_V1_PREFIX,
versioned per the design answers):

    POST /api/v1/transcriptions        upload an audio file, get a job id back immediately
    GET  /api/v1/transcriptions/{id}   poll job status; includes the transcript once completed
    GET  /api/v1/transcriptions        list recent jobs
    GET  /healthz                      liveness probe (unauthenticated, for the load balancer)

Design decisions embodied here (see README for the full write-up):
    * Upload returns 202 Accepted with a job id immediately; the actual
      transcription runs on a background worker consuming an internal
      queue, not inline in the request - this is what makes "concurrent
      uploads" and "long audio files" tractable at all (Q9/Q10 in the
      design-questions answers).
    * Every route requires an API key (`X-API-Key` header) and is subject
      to a per-key rate limit - both enforced via the `require_api_key`
      dependency (app/api/deps.py) so they can't accidentally be skipped
      on a new route.

Wired singletons (store, queue, rate_limiter - see app/main.py) are reached
via `request.app.state` rather than imported directly, so this module has
no import-time dependency on how/when those are constructed.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from app.api.deps import require_api_key
from app.api.schemas import ErrorOut, JobCreatedOut, JobStatusOut, SegmentOut, TranscriptOut
from app.core.config import settings

logger = logging.getLogger("volga.api")

router = APIRouter()


@router.get("/healthz", tags=["ops"])
async def healthz(request: Request):
    return {"status": "ok", "queue_depth": request.app.state.queue.qsize()}


@router.post(
    f"{settings.API_V1_PREFIX}/transcriptions",
    response_model=JobCreatedOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["transcriptions"],
)
async def create_transcription(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Depends(require_api_key),
):
    """
    Accept an uploaded audio file, validate it, persist it, and enqueue a
    transcription job. Returns immediately (202) with a job id - the caller
    polls GET /transcriptions/{job_id} for progress and the final result.
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(settings.ALLOWED_EXTENSIONS)}",
        )

    # Generate the job id up front so the same id is used for the DB row
    # *and* the storage key/folder - one identifier for this job everywhere,
    # rather than a throwaway upload id that has to be reconciled later.
    job_id = str(uuid.uuid4())

    # Enforce the size cap while streaming to disk rather than buffering the
    # whole upload in memory first - important once uploads are large.
    saved_path, size = await _save_with_size_limit(file, job_id)
    if size > settings.MAX_UPLOAD_BYTES:
        Path(saved_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds max upload size of {settings.MAX_UPLOAD_BYTES} bytes.",
        )

    store = request.app.state.store
    queue = request.app.state.queue

    # user_id would normally come from the authenticated principal (JWT
    # claim / session), not the raw API key - simplified for this assessment.
    job = store.create_job(user_id=api_key, original_filename=file.filename, file_path=saved_path, job_id=job_id)
    await queue.enqueue(job.id)
    logger.info("job %s queued (%s)", job.id, file.filename)

    return JobCreatedOut(
        job_id=job.id,
        status=job.status,
        status_url=f"{settings.API_V1_PREFIX}/transcriptions/{job.id}",
    )


@router.get(
    f"{settings.API_V1_PREFIX}/transcriptions/{{job_id}}",
    response_model=JobStatusOut,
    tags=["transcriptions"],
)
async def get_transcription(job_id: str, request: Request, api_key: str = Depends(require_api_key)):
    store = request.app.state.store
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    transcript_out = None
    if job.status == "completed":
        transcript_out = _load_transcript(store, job_id)

    return JobStatusOut(
        job_id=job.id,
        status=job.status,
        original_filename=job.original_filename,
        duration_seconds=job.duration_seconds,
        language=job.language,
        retry_count=job.retry_count,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        transcript=transcript_out,
    )


@router.get(
    f"{settings.API_V1_PREFIX}/transcriptions",
    tags=["transcriptions"],
)
async def list_transcriptions(request: Request, api_key: str = Depends(require_api_key), limit: int = 50):
    store = request.app.state.store
    jobs = store.list_jobs(user_id=api_key, limit=min(limit, 200))
    return [
        {"job_id": j.id, "status": j.status, "original_filename": j.original_filename, "created_at": j.created_at}
        for j in jobs
    ]


# --- helpers ------------------------------------------------------------------

async def _save_with_size_limit(file: UploadFile, job_id: str) -> tuple[str, int]:
    # Uses UploadFile's async .read() (not the underlying sync file object
    # directly) so a large upload doesn't block the event loop while it's
    # being streamed to disk - Starlette runs the actual blocking I/O in a
    # thread pool under the hood.
    dest = settings.AUDIO_DIR / job_id / (file.filename or "upload")
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    chunk_size = 1024 * 1024
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            out.write(chunk)
            if size > settings.MAX_UPLOAD_BYTES:
                break  # caller checks `size` and rejects/deletes
    return str(dest), size


def _load_transcript(store, job_id: str) -> TranscriptOut | None:
    row = store.get_latest_transcript(job_id)
    if row is None:
        return None
    if row["transcript_text"] is not None:
        payload = json.loads(row["transcript_text"])
    else:
        payload = json.loads(Path(row["transcript_json_path"]).read_text(encoding="utf-8"))
    return TranscriptOut(
        text=payload["text"],
        language=payload["language"],
        segments=[SegmentOut(**s) for s in payload["segments"]],
    )
