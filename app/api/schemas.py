"""Pydantic request/response models for the public API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SegmentOut(BaseModel):
    id: int
    start: float
    end: float
    text: str


class TranscriptOut(BaseModel):
    text: str
    language: str
    segments: list[SegmentOut]


class JobCreatedOut(BaseModel):
    job_id: str
    status: str
    status_url: str


class JobStatusOut(BaseModel):
    job_id: str
    status: str = Field(description="queued|processing|completed|retrying|failed")
    original_filename: str
    duration_seconds: Optional[float] = None
    language: Optional[str] = None
    retry_count: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
    transcript: Optional[TranscriptOut] = None


class ErrorOut(BaseModel):
    error_code: str
    detail: str
