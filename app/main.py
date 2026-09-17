"""
FastAPI application - the HTTP surface over the pipeline in app/worker.py.

Endpoints (all under /api/v1, versioned per the design answers):
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
      to a per-key rate limit - both enforced as FastAPI dependencies so
      they can't accidentally be skipped on a new route.
    * Errors use standard HTTP status codes with a consistent JSON body
      ({"error_code", "detail"}) rather than ad hoc shapes per endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.audio import AudioProcessingError
from app.config import settings
from app.logging_config import configure_logging
from app.queue_backend import InMemoryQueue
from app.rate_limit import RateLimiter
from app.schemas import ErrorOut, JobCreatedOut, JobStatusOut, SegmentOut, TranscriptOut
from app.storage_backend import LocalDiskStorage
from app.store import JobStore
from app.transcription_engine import get_engine
from app.worker import TranscriptionPipeline, Worker

configure_logging()
logger = logging.getLogger("volga.api")

# --- wiring (see app/config.py for every tunable) --------------------------
store = JobStore(settings.DB_PATH)
storage = LocalDiskStorage(settings.AUDIO_DIR)
queue = InMemoryQueue()
engine = get_engine(settings.TRANSCRIPTION_ENGINE, model_size=settings.WHISPER_MODEL_SIZE)
pipeline = TranscriptionPipeline(
    engine=engine,
    chunk_threshold_s=settings.CHUNK_THRESHOLD_SECONDS,
    chunk_length_s=settings.CHUNK_LENGTH_SECONDS,
    chunk_overlap_s=settings.CHUNK_OVERLAP_SECONDS,
    max_concurrency=settings.MAX_CONCURRENT_CHUNK_TRANSCRIPTIONS,
)
worker = Worker(
    queue=queue,
    store=store,
    storage=storage,
    pipeline=pipeline,
    transcript_dir=settings.TRANSCRIPT_DIR,
    dead_letter_dir=settings.DEAD_LETTER_DIR,
    inline_transcript_max_chars=settings.INLINE_TRANSCRIPT_MAX_CHARS,
    max_retries=settings.MAX_RETRIES,
    retry_backoff_base_s=settings.RETRY_BACKOFF_BASE_SECONDS,
)
rate_limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A single in-process worker is enough for this assessment; in
    # production this loop *is* the thing that runs as N separate worker
    # processes/containers, autoscaled off queue depth (Q9's "worker
    # autoscaling" point) - see README "Scaling this to production".
    worker_task = asyncio.create_task(worker.run_forever())
    logger.info("worker started (engine=%s)", settings.TRANSCRIPTION_ENGINE)
    yield
    worker.stop()
    worker_task.cancel()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",       # Swagger UI - "well-documented with OpenAPI/Swagger"
    redoc_url="/redoc",
)


# --- auth + rate limiting, as dependencies so no route can skip them -------

async def require_api_key(x_api_key: str = Header(default="")) -> str:
    if x_api_key not in settings.API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send it as the X-API-Key header.",
        )
    if not rate_limiter.allow(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({settings.RATE_LIMIT_PER_MINUTE}/min). Try again shortly.",
        )
    return x_api_key


# --- error handling ---------------------------------------------------------

@app.exception_handler(AudioProcessingError)
async def audio_processing_error_handler(request, exc: AudioProcessingError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorOut(error_code="AUDIO_PROCESSING_ERROR", detail=str(exc)).model_dump(),
    )


# --- routes ------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"status": "ok", "queue_depth": queue.qsize()}


@app.post(
    f"{settings.API_V1_PREFIX}/transcriptions",
    response_model=JobCreatedOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["transcriptions"],
)
async def create_transcription(
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
    job_id = _new_job_id()

    # Enforce the size cap while streaming to disk rather than buffering the
    # whole upload in memory first - important once uploads are large.
    saved_path, size = await _save_with_size_limit(file, job_id)
    if size > settings.MAX_UPLOAD_BYTES:
        Path(saved_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds max upload size of {settings.MAX_UPLOAD_BYTES} bytes.",
        )

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


@app.get(
    f"{settings.API_V1_PREFIX}/transcriptions/{{job_id}}",
    response_model=JobStatusOut,
    tags=["transcriptions"],
)
async def get_transcription(job_id: str, api_key: str = Depends(require_api_key)):
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    transcript_out = None
    if job.status == "completed":
        transcript_out = _load_transcript(job_id)

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


@app.get(
    f"{settings.API_V1_PREFIX}/transcriptions",
    tags=["transcriptions"],
)
async def list_transcriptions(api_key: str = Depends(require_api_key), limit: int = 50):
    jobs = store.list_jobs(user_id=api_key, limit=min(limit, 200))
    return [
        {"job_id": j.id, "status": j.status, "original_filename": j.original_filename, "created_at": j.created_at}
        for j in jobs
    ]


# --- helpers ------------------------------------------------------------------

def _new_job_id() -> str:
    import uuid
    return str(uuid.uuid4())


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


def _load_transcript(job_id: str) -> TranscriptOut | None:
    import json

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
