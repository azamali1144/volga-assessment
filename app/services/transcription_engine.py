"""
Pluggable transcription engine.

`TranscriptionEngine` is a tiny interface with one method, `transcribe`.
Two implementations are provided:

  * WhisperEngine - the real thing, wraps openai-whisper. This is what runs
    in production (TRANSCRIPTION_ENGINE=whisper, the default).
  * MockEngine    - a deterministic fake used in tests and in any
    environment where downloading a multi-GB model isn't practical
    (TRANSCRIPTION_ENGINE=mock). It still exercises the full pipeline
    (upload -> queue -> normalize -> chunk -> "transcribe" -> merge -> store)
    end to end, which is what actually matters for testing engineering
    decisions rather than model behavior - the brief explicitly says the
    focus is engineering decisions, not the model itself.

Swapping engines is a one-line env var change; no call site outside this
module needs to know which one is active.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.services.audio import AudioChunk


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    text: str
    language: str
    segments: list[Segment] = field(default_factory=list)


class TranscriptionEngine(Protocol):
    def transcribe(self, wav_path: Path) -> TranscriptionResult: ...


class WhisperEngine:
    """Real transcription via openai-whisper. Model is loaded once and reused."""

    def __init__(self, model_size: str = "base"):
        try:
            import whisper  # imported lazily so the mock engine has zero heavy deps
        except ImportError as exc:  # pragma: no cover - exercised only when whisper is absent
            raise RuntimeError(
                "openai-whisper is not installed. Run `pip install -r requirements.txt`, "
                "or set TRANSCRIPTION_ENGINE=mock to run without it."
            ) from exc
        self._whisper = whisper
        self._model = whisper.load_model(model_size)

    def transcribe(self, wav_path: Path) -> TranscriptionResult:
        result = self._model.transcribe(str(wav_path))
        segments = [
            Segment(
                id=seg["id"],
                start=round(float(seg["start"]), 2),
                end=round(float(seg["end"]), 2),
                text=seg["text"].strip(),
            )
            for seg in result.get("segments", [])
        ]
        return TranscriptionResult(
            text=result.get("text", "").strip(),
            language=result.get("language", "unknown"),
            segments=segments,
        )


class MockEngine:
    """
    Deterministic fake engine for tests / offline demos.

    It doesn't listen to the audio - it derives a short, reproducible
    "transcript" from the file's duration and a hash of its bytes, so the
    same input always produces the same output (useful for asserting on
    merge/retry behavior without a real model in the loop).
    """

    def transcribe(self, wav_path: Path) -> TranscriptionResult:
        from app.services.audio import probe_duration_seconds

        duration = probe_duration_seconds(wav_path)
        digest = hashlib.sha1(wav_path.read_bytes()).hexdigest()[:8]
        text = f"[mock transcript chunk={wav_path.stem} digest={digest} duration={duration:.1f}s]"
        # Emit one segment per ~2 seconds so merge logic has something real to do.
        segments = []
        t = 0.0
        seg_id = 0
        while t < duration:
            end = min(t + 2.0, duration)
            segments.append(Segment(id=seg_id, start=round(t, 2), end=round(end, 2), text=f"word{seg_id}"))
            t = end
            seg_id += 1
        return TranscriptionResult(text=text, language="en", segments=segments)


def get_engine(engine_name: str, model_size: str = "base") -> TranscriptionEngine:
    if engine_name == "mock":
        return MockEngine()
    if engine_name == "whisper":
        return WhisperEngine(model_size=model_size)
    raise ValueError(f"Unknown TRANSCRIPTION_ENGINE: {engine_name!r}")


def merge_chunk_results(
    chunk_results: list[tuple[AudioChunk, TranscriptionResult]],
    overlap_seconds: float,
) -> TranscriptionResult:
    """
    Merge per-chunk transcription results back into one transcript with
    globally-correct timestamps.

    Each chunk's segment timestamps are local to that chunk, so they're
    shifted by the chunk's start_offset first. Then, because consecutive
    chunks overlap by `overlap_seconds`, any segment from a *later* chunk
    that starts before the previous chunk's end (minus a small tolerance)
    is dropped - it's a re-transcription of audio the previous chunk
    already covered, not new content.
    """
    chunk_results_sorted = sorted(chunk_results, key=lambda cr: cr[0].index)

    merged_segments: list[Segment] = []
    texts: list[str] = []
    language = "unknown"
    previous_chunk_end_global = -1.0
    next_id = 0

    for chunk, result in chunk_results_sorted:
        if result.language and result.language != "unknown":
            language = result.language

        chunk_texts = []
        for seg in result.segments:
            global_start = chunk.start_offset + seg.start
            global_end = chunk.start_offset + seg.end

            # Skip segments that fall entirely inside the overlap region
            # already covered by the previous chunk.
            if global_start < previous_chunk_end_global - 0.05:
                continue

            merged_segments.append(
                Segment(id=next_id, start=round(global_start, 2), end=round(global_end, 2), text=seg.text)
            )
            chunk_texts.append(seg.text)
            next_id += 1

        if chunk_texts:
            texts.append(" ".join(chunk_texts))
        elif not result.segments and result.text:
            # Engine returned plain text with no segment breakdown at all -
            # keep it. If it *did* return segments but every one of them was
            # fully inside the previous chunk's overlap region, this chunk
            # is genuinely redundant and contributes nothing (not a fallback
            # to its raw, un-trimmed text, which would duplicate content).
            texts.append(result.text)

        chunk_duration_covered = chunk.start_offset + (
            result.segments[-1].end if result.segments else 0.0
        )
        previous_chunk_end_global = max(previous_chunk_end_global, chunk_duration_covered)

    return TranscriptionResult(
        text=" ".join(t for t in texts if t).strip(),
        language=language,
        segments=merged_segments,
    )
