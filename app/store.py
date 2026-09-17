"""
Job persistence layer, backed by SQLite.

Why SQLite instead of the PostgreSQL described in the design-questions
answers: for a small, single-process assessment service, SQLite gives real
ACID persistence (jobs survive a process restart, which an in-memory dict
would not) with zero setup. The schema below is written to be a direct,
drop-in match for the PostgreSQL schema described in the README/answers -
swapping the DB_PATH connection for a psycopg2/SQLAlchemy Postgres
connection is a config change, not a rewrite (see README "Scaling this to
production").

Job status lifecycle:
    queued -> processing -> completed
                  |-> retrying -> processing (loop, up to MAX_RETRIES)
                  |-> failed  (moved to dead-letter after MAX_RETRIES)
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    duration_seconds REAL,
    status          TEXT NOT NULL,          -- queued|processing|completed|retrying|failed
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    error_message   TEXT,
    failed_at       TEXT,
    language        TEXT,
    trans_version   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    transcript_text TEXT,             -- populated when small enough to inline
    transcript_json_path TEXT,        -- populated when written to file storage instead
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL,    -- stored|archived
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_transcripts_job_id ON transcripts(job_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    user_id: str
    original_filename: str
    file_path: str
    status: str
    duration_seconds: Optional[float] = None
    retry_count: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    failed_at: Optional[str] = None
    language: Optional[str] = None
    trans_version: int = 1
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(**{k: row[k] for k in row.keys()})


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent readers + one writer
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- job lifecycle -------------------------------------------------

    def create_job(self, user_id: str, original_filename: str, file_path: str, job_id: Optional[str] = None) -> Job:
        job = Job(
            id=job_id or str(uuid.uuid4()),
            user_id=user_id,
            original_filename=original_filename,
            file_path=file_path,
            status="queued",
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, user_id, original_filename, file_path, status,
                    retry_count, trans_version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.id, job.user_id, job.original_filename, job.file_path, job.status,
                 job.retry_count, job.trans_version, job.created_at, job.updated_at),
            )
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return Job.from_row(row) if row else None

    def list_jobs(self, user_id: Optional[str] = None, limit: int = 50) -> list[Job]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [Job.from_row(r) for r in rows]

    def set_status(self, job_id: str, status: str, **fields) -> None:
        """Generic status + arbitrary-column update, always bumping updated_at."""
        fields["status"] = status
        fields["updated_at"] = _now()
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))

    def mark_processing(self, job_id: str) -> None:
        self.set_status(job_id, "processing")

    def mark_completed(self, job_id: str, duration_seconds: float, language: str) -> None:
        self.set_status(
            job_id, "completed",
            duration_seconds=duration_seconds, language=language,
            error_code=None, error_message=None, failed_at=None,
        )

    def mark_retrying(self, job_id: str, error_code: str, error_message: str, retry_count: int) -> None:
        self.set_status(
            job_id, "retrying",
            error_code=error_code, error_message=error_message, retry_count=retry_count,
        )

    def mark_failed(self, job_id: str, error_code: str, error_message: str, retry_count: int) -> None:
        self.set_status(
            job_id, "failed",
            error_code=error_code, error_message=error_message,
            retry_count=retry_count, failed_at=_now(),
        )

    # ---- transcripts -----------------------------------------------------

    def save_transcript(
        self,
        job_id: str,
        *,
        text: Optional[str],
        json_path: Optional[str],
        version: int,
    ) -> str:
        transcript_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO transcripts
                   (id, job_id, transcript_text, transcript_json_path, version, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'stored', ?)""",
                (transcript_id, job_id, text, json_path, version, _now()),
            )
        return transcript_id

    def get_latest_transcript(self, job_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM transcripts WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                (job_id,),
            ).fetchone()
