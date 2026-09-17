import asyncio
import glob
import shutil
import tempfile
import unittest
from pathlib import Path

from app.queue_backend import InMemoryQueue
from app.storage_backend import LocalDiskStorage
from app.store import JobStore
from app.transcription_engine import Segment, TranscriptionResult
from app.worker import TranscriptionPipeline, Worker
from tests.conftest_helpers import make_tone


class FlakyEngine:
    """Fails `fail_times` times, then succeeds - proves retry-then-success."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def transcribe(self, wav_path):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"simulated transient failure #{self.calls}")
        return TranscriptionResult(text="ok", language="en", segments=[Segment(0, 0, 1, "ok")])


class AlwaysFailsEngine:
    def transcribe(self, wav_path):
        raise RuntimeError("permanent failure")


async def _run_worker_briefly(worker: Worker, seconds: float = 1.0):
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(seconds)
    worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestWorkerRetryAndDeadLetter(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="volga_test_worker_"))
        self.store = JobStore(self.tmp_dir / "jobs.db")
        self.storage = LocalDiskStorage(self.tmp_dir / "audio")
        self.transcript_dir = self.tmp_dir / "transcripts"
        self.dead_letter_dir = self.tmp_dir / "dead_letter"
        self.tone = make_tone(2, suffix=".wav")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_worker(self, engine, max_retries):
        pipeline = TranscriptionPipeline(
            engine, chunk_threshold_s=999, chunk_length_s=240, chunk_overlap_s=5, max_concurrency=2
        )
        queue = InMemoryQueue()
        worker = Worker(
            queue=queue, store=self.store, storage=self.storage, pipeline=pipeline,
            transcript_dir=self.transcript_dir, dead_letter_dir=self.dead_letter_dir,
            inline_transcript_max_chars=20000, max_retries=max_retries, retry_backoff_base_s=0.05,
        )
        return queue, worker

    def test_transient_failures_then_success(self):
        async def scenario():
            queue, worker = self._make_worker(FlakyEngine(fail_times=2), max_retries=3)
            with open(self.tone, "rb") as f:
                path = self.storage.save("job-flaky", self.tone.name, f)
            job = self.store.create_job("user-1", self.tone.name, path)
            await queue.enqueue(job.id)
            await _run_worker_briefly(worker, seconds=1.0)
            return self.store.get_job(job.id)

        job = asyncio.run(scenario())
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.retry_count, 2)
        transcript = self.store.get_latest_transcript(job.id)
        self.assertIsNotNone(transcript)

    def test_permanent_failure_goes_to_dead_letter(self):
        async def scenario():
            queue, worker = self._make_worker(AlwaysFailsEngine(), max_retries=2)
            with open(self.tone, "rb") as f:
                path = self.storage.save("job-fail", self.tone.name, f)
            job = self.store.create_job("user-2", self.tone.name, path)
            await queue.enqueue(job.id)
            await _run_worker_briefly(worker, seconds=1.0)
            return self.store.get_job(job.id)

        job = asyncio.run(scenario())
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.retry_count, 3)  # 1 initial attempt + 2 retries
        self.assertEqual(job.error_code, "RuntimeError")
        dead_letters = glob.glob(str(self.dead_letter_dir / "*.json"))
        self.assertEqual(len(dead_letters), 1)

    def test_long_audio_goes_through_chunked_pipeline(self):
        long_tone = make_tone(20, suffix=".wav")

        async def scenario():
            pipeline = TranscriptionPipeline(
                FlakyEngine(fail_times=0), chunk_threshold_s=10, chunk_length_s=8, chunk_overlap_s=2, max_concurrency=2
            )
            queue = InMemoryQueue()
            worker = Worker(
                queue=queue, store=self.store, storage=self.storage, pipeline=pipeline,
                transcript_dir=self.transcript_dir, dead_letter_dir=self.dead_letter_dir,
                inline_transcript_max_chars=20000, max_retries=2, retry_backoff_base_s=0.05,
            )
            with open(long_tone, "rb") as f:
                path = self.storage.save("job-long", long_tone.name, f)
            job = self.store.create_job("user-3", long_tone.name, path)
            await queue.enqueue(job.id)
            await _run_worker_briefly(worker, seconds=1.0)
            return self.store.get_job(job.id)

        job = asyncio.run(scenario())
        self.assertEqual(job.status, "completed")
        self.assertAlmostEqual(job.duration_seconds, 20.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
