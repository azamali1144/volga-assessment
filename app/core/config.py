"""
Centralized configuration, loaded from environment variables (12-factor style).

Every value has a sane local-dev default so the service runs out of the box
with `uvicorn app.main:app --reload`, but every value that matters in
production (API keys, retry limits, storage backend) is overridable via env
vars so the same image can be promoted across environments without a rebuild.
"""
import os
from pathlib import Path


class Settings:
    # --- API ---
    API_V1_PREFIX: str = "/api/v1"
    APP_NAME: str = "Volga Transcription Service"

    # --- Auth ---
    # Comma-separated list of valid API keys. In production this would be
    # backed by a secrets manager / IAM, not an env var — kept simple here
    # since the brief calls for engineering decisions, not a full auth system.
    API_KEYS: set[str] = set(
        k.strip() for k in os.getenv("API_KEYS", "dev-local-key").split(",") if k.strip()
    )

    # --- Rate limiting ---
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

    # --- Storage (local disk stands in for S3/GCS/Azure Blob) ---
    STORAGE_ROOT: Path = Path(os.getenv("STORAGE_ROOT", "storage"))
    AUDIO_DIR: Path = STORAGE_ROOT / "audio"
    TRANSCRIPT_DIR: Path = STORAGE_ROOT / "transcripts"
    DEAD_LETTER_DIR: Path = STORAGE_ROOT / "dead_letter"

    # --- Database (SQLite stands in for PostgreSQL) ---
    DB_PATH: Path = STORAGE_ROOT / "jobs.db"

    # --- Upload constraints ---
    ALLOWED_EXTENSIONS: set[str] = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4"}
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))  # 500 MB

    # --- Transcription / chunking ---
    # Files longer than this are split into overlapping chunks and
    # transcribed independently (Q: "how do you deal with long audio files?").
    CHUNK_THRESHOLD_SECONDS: float = float(os.getenv("CHUNK_THRESHOLD_SECONDS", "300"))  # 5 min
    CHUNK_LENGTH_SECONDS: float = float(os.getenv("CHUNK_LENGTH_SECONDS", "240"))  # 4 min
    CHUNK_OVERLAP_SECONDS: float = float(os.getenv("CHUNK_OVERLAP_SECONDS", "5"))
    MAX_CONCURRENT_CHUNK_TRANSCRIPTIONS: int = int(os.getenv("MAX_CONCURRENT_CHUNKS", "3"))

    # Transcript is stored inline in the DB below this size; above it, it's
    # written to a JSON file in TRANSCRIPT_DIR and only the path is stored
    # in the DB (Q: "how would you store audio and transcripts?").
    INLINE_TRANSCRIPT_MAX_CHARS: int = int(os.getenv("INLINE_TRANSCRIPT_MAX_CHARS", "20000"))

    WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")

    # Swap the transcription engine without touching any call site — used to
    # run the full pipeline in CI/tests/this-sandbox without pulling a
    # multi-GB Whisper model. See app/transcription_engine.py.
    TRANSCRIPTION_ENGINE: str = os.getenv("TRANSCRIPTION_ENGINE", "whisper")  # "whisper" | "mock"

    # --- Retry / dead-letter ---
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_BACKOFF_BASE_SECONDS: float = float(os.getenv("RETRY_BACKOFF_BASE_SECONDS", "2"))

    def ensure_dirs(self) -> None:
        for d in (self.AUDIO_DIR, self.TRANSCRIPT_DIR, self.DEAD_LETTER_DIR):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
