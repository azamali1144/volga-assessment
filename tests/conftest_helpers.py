"""
Shared test helpers - generates synthetic audio with ffmpeg so tests don't
need any binary fixture files checked into the repo.
"""
import subprocess
import tempfile
from pathlib import Path


def make_tone(duration_seconds: float, suffix: str = ".wav", freq: int = 440) -> Path:
    fd_dir = Path(tempfile.mkdtemp(prefix="volga_test_audio_"))
    out = fd_dir / f"tone{suffix}"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency={freq}:duration={duration_seconds}",
        "-ar", "44100", "-ac", "2",
        str(out),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out
