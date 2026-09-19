"""
Background worker: pulls jobs off the queue and runs the transcription
pipeline (normalize -> chunk if long -> transcribe -> merge -> persist).

Retry / dead-letter behaviour (Q: "how do you retry or recover failed
transcriptions?"):
  * Any exception during processing is caught. If the job's retry_count is
    below MAX_RETRIES, the job is marked "retrying", re-queued with
    exponential backoff, and picked up again later.
  * Once MAX_RETRIES is exceeded, the job is marked "failed" and a record is
    written to the dead-letter directory for manual review - it is not
    silently dropped, and it is not retried forever.
  * A job that fails partway through chunk transcription (e.g. chunk 3 of 5
    errors) is retried as a whole rather than resumed chunk-by-chunk, to
    keep the state machine simple for this assessment; the README calls out
    resumable per-chunk retry as the natural next step for very long files.

This module intentionally has no FastAPI import - it only depends on the
queue/store/storage/audio/engine modules, so it (and the pipeline it drives)
can be unit-tested without spinning up the web layer at all.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from app.db.store import JobStore
from app.services.audio import AudioProcessingError, normalize_to_wav, probe_duration_seconds, split_into_chunks
from app.services.queue_backend import InMemoryQueue, JobMessage
from app.services.storage_backend import LocalDiskStorage
from app.services.transcription_engine import TranscriptionEngine, merge_chunk_results

logger = logging.getLogger("volga.worker")


class TranscriptionPipeline:
    """
    Pure pipeline logic - no queue, no retry policy, just:
    input audio path -> normalized -> (maybe chunked) -> merged transcript.

    Kept separate from `Worker` so it can be called directly in tests and
    reused by a future "reprocess this one job synchronously" admin
    endpoint without going through the async queue at all.
    """

    def __init__(self, engine: TranscriptionEngine, chunk_threshold_s: float,
                 chunk_length_s: float, chunk_overlap_s: float, max_concurrency: int):
        self.engine = engine
        self.chunk_threshold_s = chunk_threshold_s
        self.chunk_length_s = chunk_length_s
        self.chunk_overlap_s = chunk_overlap_s
        self.max_concurrency = max_concurrency

    async def run(self, src_audio_path: Path, work_dir: Path):
        work_dir.mkdir(parents=True, exist_ok=True)
        normalized_path = work_dir / "normalized.wav"
        normalize_to_wav(src_audio_path, normalized_path)
        duration = probe_duration_seconds(normalized_path)

        if duration <= self.chunk_threshold_s:
            result = await asyncio.to_thread(self.engine.transcribe, normalized_path)
            return result, duration

        chunks = split_into_chunks(
            normalized_path, work_dir / "chunks",
            chunk_length_seconds=self.chunk_length_s,
            overlap_seconds=self.chunk_overlap_s,
        )
        logger.info("split %s into %d chunk(s) (duration=%.1fs)", src_audio_path.name, len(chunks), duration)

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def transcribe_chunk(chunk):
            async with semaphore:
                result = await asyncio.to_thread(self.engine.transcribe, chunk.path)
                return chunk, result

        chunk_results = await asyncio.gather(*(transcribe_chunk(c) for c in chunks))
        merged = merge_chunk_results(list(chunk_results), overlap_seconds=self.chunk_overlap_s)
        return merged, duration


class Worker:
    def __init__(
        self,
        queue: InMemoryQueue,
        store: JobStore,
        storage: LocalDiskStorage,
        pipeline: TranscriptionPipeline,
        transcript_dir: Path,
        dead_letter_dir: Path,
        inline_transcript_max_chars: int,
        max_retries: int,
        retry_backoff_base_s: float,
    ):
        self.queue = queue
        self.store = store
        self.storage = storage
        self.pipeline = pipeline
        self.transcript_dir = transcript_dir
        self.dead_letter_dir = dead_letter_dir
        self.inline_transcript_max_chars = inline_transcript_max_chars
        self.max_retries = max_retries
        self.retry_backoff_base_s = retry_backoff_base_s
        self._stop = False

    async def run_forever(self) -> None:
        while not self._stop:
            message = await self.queue.dequeue()
            try:
                await self._process_once(message)
            finally:
                self.queue.task_done()

    def stop(self) -> None:
        self._stop = True

    async def _process_once(self, message: JobMessage) -> None:
        job = self.store.get_job(message.job_id)
        if job is None:
            logger.warning("job %s not found - dropping message", message.job_id)
            return

        self.store.mark_processing(job.id)
        work_dir = Path(tempfile.mkdtemp(prefix=f"job_{job.id}_"))
        try:
            result, duration = await self.pipeline.run(Path(job.file_path), work_dir)
            self._persist_transcript(job.id, result, job.trans_version)
            self.store.mark_completed(job.id, duration_seconds=duration, language=result.language)
            logger.info("job %s completed (%.1fs audio, %d segments)", job.id, duration, len(result.segments))

        except Exception as exc:  # noqa: BLE001 - a job-processing failure must never crash the worker
            await self._handle_failure(job.id, message.attempt, exc)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _persist_transcript(self, job_id: str, result, version: int) -> None:
        import json

        payload = {
            "text": result.text,
            "language": result.language,
            "segments": [vars(s) for s in result.segments],
        }
        serialized = json.dumps(payload, ensure_ascii=False)

        if len(serialized) <= self.inline_transcript_max_chars:
            self.store.save_transcript(job_id, text=serialized, json_path=None, version=version)
        else:
            # Large transcript: write to file storage, keep only the path in the DB
            # (Q: "how would you store audio and transcripts?").
            out_path = self.transcript_dir / f"{job_id}_v{version}.json"
            out_path.write_text(serialized, encoding="utf-8")
            self.store.save_transcript(job_id, text=None, json_path=str(out_path), version=version)

    async def _handle_failure(self, job_id: str, attempt: int, exc: Exception) -> None:
        error_code = type(exc).__name__ if not isinstance(exc, AudioProcessingError) else "AUDIO_PROCESSING_ERROR"
        error_message = str(exc)[:2000]
        logger.error("job %s failed on attempt %d: %s: %s", job_id, attempt, error_code, error_message)

        if attempt <= self.max_retries:
            self.store.mark_retrying(job_id, error_code=error_code, error_message=error_message, retry_count=attempt)
            backoff = self.retry_backoff_base_s * (2 ** (attempt - 1))
            asyncio.get_running_loop().call_later(
                backoff, lambda: asyncio.ensure_future(self.queue.enqueue(job_id, attempt=attempt + 1))
            )
        else:
            self.store.mark_failed(job_id, error_code=error_code, error_message=error_message, retry_count=attempt)
            self._write_dead_letter(job_id, error_code, error_message)

    def _write_dead_letter(self, job_id: str, error_code: str, error_message: str) -> None:
        import json
        from datetime import datetime, timezone

        self.dead_letter_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "job_id": job_id,
            "error_code": error_code,
            "error_message": error_message,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        out_path = self.dead_letter_dir / f"{job_id}_{uuid.uuid4().hex[:8]}.json"
        out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        logger.error("job %s moved to dead-letter: %s", job_id, out_path)
