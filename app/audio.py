"""
Audio normalization and chunking, built directly on the ffmpeg CLI.

Design decisions (see README for the full rationale):
  * ffmpeg (not a Python audio library) does the heavy lifting - it already
    handles every container/codec combination we're likely to see (mp3, wav,
    m4a, flac, ogg, mp4) and is the same tool most transcription services use
    under the hood, so there's no real accuracy/format win from a Python lib.
  * Every input is normalized to 16kHz mono PCM WAV before it reaches the
    transcription engine, because that's the format Whisper (and most ASR
    engines) is tuned for - normalizing once, up front, means the rest of
    the pipeline never has to think about source format again.
  * Long files are split into overlapping chunks *before* transcription so
    each chunk can be transcribed independently (in parallel, and safely
    retried on its own) instead of holding one giant file in memory for a
    single long-running call.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegNotFoundError(RuntimeError):
    pass


class AudioProcessingError(RuntimeError):
    pass


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise FFmpegNotFoundError(
            "ffmpeg/ffprobe not found on PATH. Install ffmpeg (apt-get install ffmpeg)."
        )


def probe_duration_seconds(path: Path) -> float:
    """Return the duration of an audio file in seconds via ffprobe."""
    _require_ffmpeg()
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AudioProcessingError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioProcessingError(f"Could not read duration from ffprobe output: {data}") from exc


def normalize_to_wav(src: Path, dst: Path, sample_rate: int = 16000) -> Path:
    """
    Normalize any supported input (mp3/wav/m4a/flac/ogg/mp4/...) to
    16kHz mono PCM16 WAV - the format the transcription engine expects.

    This is also where a corrupt/unsupported file gets rejected early with a
    clear error, rather than failing deep inside the transcription call.
    """
    _require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-ac", "1",                 # mono
        "-ar", str(sample_rate),    # sample rate Whisper expects
        "-vn",                      # drop any video stream (e.g. mp4 input)
        "-f", "wav",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise AudioProcessingError(
            f"ffmpeg normalization failed for {src.name}: {proc.stderr.strip()[-800:]}"
        )
    return dst


@dataclass
class AudioChunk:
    index: int
    path: Path
    start_offset: float  # seconds into the *original* audio where this chunk starts


def split_into_chunks(
    src: Path,
    out_dir: Path,
    chunk_length_seconds: float,
    overlap_seconds: float,
) -> list[AudioChunk]:
    """
    Split a (already-normalized) WAV file into overlapping chunks.

    A small overlap (default 5s, see config.CHUNK_OVERLAP_SECONDS) means a
    word spoken right at a chunk boundary is fully present in at least one
    chunk instead of being cut in half - segment merging (transcription_engine.
    merge_chunk_results) uses the offsets recorded here to de-duplicate the
    overlapping region rather than re-emitting it twice.
    """
    _require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    total_duration = probe_duration_seconds(src)

    if total_duration <= chunk_length_seconds:
        # Nothing to split - the caller's threshold check should normally
        # prevent us getting here, but stay correct if called directly.
        return [AudioChunk(index=0, path=src, start_offset=0.0)]

    stride = chunk_length_seconds - overlap_seconds
    if stride <= 0:
        raise ValueError("overlap_seconds must be smaller than chunk_length_seconds")

    chunks: list[AudioChunk] = []
    start = 0.0
    index = 0
    while start < total_duration:
        chunk_path = out_dir / f"chunk_{index:04d}.wav"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-ss", f"{start:.3f}",
            "-t", f"{chunk_length_seconds:.3f}",
            "-ac", "1", "-ar", "16000",
            "-f", "wav",
            str(chunk_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not chunk_path.exists():
            raise AudioProcessingError(
                f"ffmpeg chunking failed at offset {start}: {proc.stderr.strip()[-800:]}"
            )
        chunks.append(AudioChunk(index=index, path=chunk_path, start_offset=start))
        index += 1
        start += stride

    return chunks
