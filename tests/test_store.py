import shutil
import tempfile
import unittest
from pathlib import Path

from app.store import JobStore


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="volga_test_db_"))
        self.store = JobStore(self.tmp_dir / "jobs.db")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_and_fetch_job(self):
        job = self.store.create_job("user-1", "audio.mp3", "/tmp/audio.mp3")
        fetched = self.store.get_job(job.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.status, "queued")
        self.assertEqual(fetched.retry_count, 0)

    def test_status_lifecycle(self):
        job = self.store.create_job("user-1", "audio.mp3", "/tmp/audio.mp3")
        self.store.mark_processing(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "processing")

        self.store.mark_retrying(job.id, error_code="TIMEOUT", error_message="boom", retry_count=1)
        retried = self.store.get_job(job.id)
        self.assertEqual(retried.status, "retrying")
        self.assertEqual(retried.retry_count, 1)
        self.assertEqual(retried.error_code, "TIMEOUT")

        self.store.mark_completed(job.id, duration_seconds=10.0, language="en")
        done = self.store.get_job(job.id)
        self.assertEqual(done.status, "completed")
        self.assertIsNone(done.error_code)  # completed clears any prior error
        self.assertEqual(done.language, "en")

    def test_mark_failed_after_retries_exhausted(self):
        job = self.store.create_job("user-1", "audio.mp3", "/tmp/audio.mp3")
        self.store.mark_failed(job.id, error_code="ENGINE_ERROR", error_message="nope", retry_count=3)
        failed = self.store.get_job(job.id)
        self.assertEqual(failed.status, "failed")
        self.assertIsNotNone(failed.failed_at)

    def test_transcript_round_trip(self):
        job = self.store.create_job("user-1", "audio.mp3", "/tmp/audio.mp3")
        self.store.save_transcript(job.id, text='{"text": "hi"}', json_path=None, version=1)
        latest = self.store.get_latest_transcript(job.id)
        self.assertEqual(latest["transcript_text"], '{"text": "hi"}')

    def test_list_jobs_filters_by_user(self):
        self.store.create_job("user-1", "a.mp3", "/tmp/a.mp3")
        self.store.create_job("user-2", "b.mp3", "/tmp/b.mp3")
        user1_jobs = self.store.list_jobs(user_id="user-1")
        self.assertEqual(len(user1_jobs), 1)
        self.assertEqual(user1_jobs[0].original_filename, "a.mp3")

    def test_custom_job_id_is_respected(self):
        job = self.store.create_job("user-1", "a.mp3", "/tmp/a.mp3", job_id="fixed-id")
        self.assertEqual(job.id, "fixed-id")
        self.assertIsNotNone(self.store.get_job("fixed-id"))


if __name__ == "__main__":
    unittest.main()
