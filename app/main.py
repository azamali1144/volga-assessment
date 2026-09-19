"""
FastAPI application entrypoint: wires the concrete singletons (store, queue,
storage, transcription engine/pipeline, worker, rate limiter) and exposes
`app` for uvicorn (`uvicorn app.main:app`).

The wired singletons are attached to `app.state` in `lifespan` below so the
route handlers (app/api/routes.py) and dependencies (app/api/deps.py) can
reach them without importing this module - see those modules for the actual
route/dependency logic this module just assembles.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.schemas import ErrorOut
from app.core.config import settings
from app.core.logging_config import configure_logging
from app.core.rate_limit import RateLimiter
from app.db.store import JobStore
from app.services.audio import AudioProcessingError
from app.services.queue_backend import InMemoryQueue
from app.services.storage_backend import LocalDiskStorage
from app.services.transcription_engine import get_engine
from app.services.worker import TranscriptionPipeline, Worker

configure_logging()
logger = logging.getLogger("volga.api")

# --- wiring (see app/core/config.py for every tunable) ----------------------
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
    # Singletons live on app.state so route handlers/dependencies can reach
    # them via `request.app.state` without importing this module.
    app.state.store = store
    app.state.queue = queue
    app.state.rate_limiter = rate_limiter

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


@app.exception_handler(AudioProcessingError)
async def audio_processing_error_handler(request, exc: AudioProcessingError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorOut(error_code="AUDIO_PROCESSING_ERROR", detail=str(exc)).model_dump(),
    )


app.include_router(router)
