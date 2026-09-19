# volga-assessment — Audio Transcription Pipeline

A small, production-shaped service that accepts an audio file, transcribes it
with [Whisper](https://github.com/openai/whisper), and returns text with
per-segment timestamps — built for the Volga Partners software engineer
assessment. The brief asked for engineering decisions, not a trained model,
so this README spends most of its words on *why* the service is put together
this way, not on Whisper itself.

## Contents

- [Quick start](#quick-start)
  - [macOS / Linux](#macos--linux)
  - [Windows 11 (PowerShell)](#windows-11-powershell)
  - [Windows + Docker Desktop troubleshooting](#windows--docker-desktop-troubleshooting)
- [Architecture](#architecture)
- [API](#api)
- [Inspecting local data (DB, audio, transcripts)](#inspecting-local-data-db-audio-transcripts)
- [Design decisions](#design-decisions) — one subsection per assessment question
- [What's real vs. mocked, and why](#whats-real-vs-mocked-and-why)
- [Scaling this to production](#scaling-this-to-production)
- [Testing](#testing)
- [Known limitations / what I'd do with more time](#known-limitations--what-id-do-with-more-time)

## Quick start

Two ways to run it: Docker (simplest — ffmpeg and everything else is baked
into the image), or a native virtualenv (needed if you want `--reload`
iteration on the code). Pick whichever suits you; commands are given for
both macOS/Linux and Windows 11.

### macOS / Linux

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # defaults are already sane for local dev

# Run the whole pipeline without downloading the Whisper model, using the
# deterministic mock engine (see "What's real vs. mocked" below):
TRANSCRIPTION_ENGINE=mock uvicorn app.main:app --reload

# Or run it for real:
TRANSCRIPTION_ENGINE=whisper uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs` for interactive Swagger UI.

```bash
# upload a file (API key defaults to "dev-local-key" locally, see .env.example)
curl -s -X POST http://localhost:8000/api/v1/transcriptions \
  -H "X-API-Key: dev-local-key" \
  -F "file=@sample.mp3" | tee /tmp/job.json

# poll for the result
JOB_ID=$(python3 -c "import json;print(json.load(open('/tmp/job.json'))['job_id'])")
curl -s http://localhost:8000/api/v1/transcriptions/$JOB_ID -H "X-API-Key: dev-local-key"
```

**Docker:**

```bash
docker build -t volga-assessment .
docker run -p 8000:8000 volga-assessment
```

**Tests** (no fastapi/whisper required — see [Testing](#testing)):

```bash
python -m unittest discover -s tests -p "test_*.py" -v
# or: pytest -v
```

### Windows 11 (PowerShell)

The commands above are bash; here's the PowerShell equivalent, plus the
Windows-specific gotchas that don't show up on macOS/Linux.

**Option A — Docker (recommended: sidesteps the ffmpeg-on-PATH and
Whisper/torch install-time issues below).**

```powershell
docker build -t volga-assessment .

# mock engine (default baked into the image, no model download):
docker run -p 8000:8000 -v ${PWD}\storage:/srv/storage volga-assessment

# real Whisper engine, with your own .env for API keys etc.:
docker run -p 8000:8000 --env-file .env -e TRANSCRIPTION_ENGINE=whisper -v ${PWD}\storage:/srv/storage volga-assessment
```

The `-v ${PWD}\storage:/srv/storage` mount matters: without it, everything
under `/srv/storage` (the SQLite DB, uploaded audio, transcripts) lives only
in that specific container's writable layer and is gone the moment the
container is removed — and every `docker run` without `--rm` creates a
*new* container, so repeated runs quietly pile up disconnected containers
each with their own empty storage. Mounting a host folder makes the data
persist across restarts and lets you open `storage\jobs.db` directly from
Windows. See [Windows + Docker Desktop troubleshooting](#windows--docker-desktop-troubleshooting)
if `docker build`/`docker run` fail outright, and
[Inspecting local data](#inspecting-local-data-db-audio-transcripts) for how
to query the DB and browse stored files.

Open `http://localhost:8000/docs` for Swagger UI — note it's `localhost`,
not the `0.0.0.0` shown in the container's own startup log (`0.0.0.0` is
where the server *listens*, not an address a browser can navigate to).

**Option B — native venv.**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

`ffmpeg` is required regardless of engine — `app/audio.py` shells out to
`ffmpeg`/`ffprobe` on every upload (format normalization + chunking), not
just in `whisper` mode. It's a runtime dependency, not just a Whisper one.
Install it and restart your terminal so PATH picks it up:

```powershell
winget install Gyan.FFmpeg
# or: choco install ffmpeg
```

Run with the mock engine (no model download):

```powershell
$env:TRANSCRIPTION_ENGINE = "mock"
uvicorn app.main:app --reload
```

Or with real Whisper:

```powershell
$env:TRANSCRIPTION_ENGINE = "whisper"
uvicorn app.main:app --reload
```

If `pip install -r requirements.txt` fails while building `openai-whisper`
with `ModuleNotFoundError: No module named 'pkg_resources'`, that's a known
incompatibility between this pinned Whisper release's old `setup.py` and
recent `setuptools` releases that dropped `pkg_resources` by default — not
a problem with this repo's own code. Fix it by constraining `setuptools`
for the build:

```powershell
pip install "setuptools<81"
pip install -r requirements.txt
```

(The Dockerfile applies the equivalent fix automatically via
`PIP_CONSTRAINT`, so Option A doesn't need this.)

**Trying the API** (PowerShell equivalent of the curl example above; note
the default API key is only `dev-local-key` if you didn't pass `.env` to
the container — see the callout above):

```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/transcriptions" `
  -Method Post -Headers @{ "X-API-Key" = "dev-local-key" } `
  -Form @{ file = Get-Item "sample.mp3" }
$resp

Invoke-RestMethod -Uri "http://localhost:8000/api/v1/transcriptions/$($resp.job_id)" `
  -Headers @{ "X-API-Key" = "dev-local-key" }
```

**Tests:**

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
# or: pytest -v
```

`test_audio.py` and parts of `test_worker.py` shell out to real `ffmpeg`,
so install it first (see above) for a fully green run.

### Windows + Docker Desktop troubleshooting

Two Docker Desktop issues are common enough on Windows to call out
explicitly:

- **`permission denied while trying to connect to the docker API at
  npipe:////./pipe/docker_engine`** — your Windows account isn't a member
  of the `docker-users` local group, so it can't reach the named pipe even
  though Docker Desktop itself is running. Fix it from an **elevated**
  PowerShell:

  ```powershell
  net localgroup docker-users "<your-username>" /add
  ```

  Then **sign out and back in to Windows** (not just restart the
  terminal — group membership changes need a fresh login token). Check
  membership with `net localgroup docker-users`, and check the engine
  itself is actually up with `docker info` or the Docker Desktop UI
  ("Engine running" bottom-left).

- **`.env` silently ignored** — nothing in this app calls `python-dotenv`
  or otherwise auto-loads `.env`; `app/config.py` only reads real
  environment variables via `os.getenv(...)`. `.env` is just a file *you*
  are expected to turn into env vars (`Copy-Item .env.example .env` plus
  either `docker run --env-file .env ...`, or setting `$env:VAR` values
  yourself for the native venv path). Running `docker run` without
  `--env-file .env` means the container uses the defaults baked into
  `app/config.py` (e.g. API key `dev-local-key`), regardless of what your
  `.env` says.

## Architecture

```
                    ┌───────────────────────────────────────────┐
                    │                FastAPI app                │
                    │                                             │
  client  ───POST──▶│  /api/v1/transcriptions                   │
  (upload)           │    - validate extension                   │
                    │    - stream to disk with a size cap        │───▶ storage_backend
                    │    - create job row (status=queued)         │    (LocalDiskStorage;
                    │    - enqueue job id                         │     S3 in prod)
                    │    - 202 Accepted {job_id, status_url}      │
                    └───────────────────┬───────────────────────┘
                                        │ enqueue                       ┌────────────┐
                                        ▼                               │  JobStore   │
                    ┌───────────────────────────────────────────┐     │  (SQLite;   │
                    │              queue_backend                 │◀───▶│  Postgres   │
                    │         (InMemoryQueue; SQS/RabbitMQ        │     │  in prod)   │
                    │              /Kafka in prod)                │     └────────────┘
                    └───────────────────┬───────────────────────┘
                                        │ dequeue
                                        ▼
                    ┌───────────────────────────────────────────┐
                    │                  Worker                     │
                    │  1. mark job "processing"                   │
                    │  2. normalize_to_wav (ffmpeg)                │
                    │  3. duration > threshold?                    │
                    │        no  → transcribe once                │
                    │        yes → split_into_chunks (overlap)     │
                    │              → transcribe each (parallel,    │
                    │                bounded concurrency)          │
                    │              → merge_chunk_results           │
                    │                (de-dupes the overlap)        │
                    │  4. persist transcript (inline or file)      │
                    │  5. mark "completed"                         │
                    │                                               │
                    │  on exception: retry with backoff up to       │
                    │  MAX_RETRIES, then "failed" + dead-letter     │
                    │  record for manual review                     │
                    └───────────────────┬───────────────────────┘
                                        │
                                        ▼
                    ┌───────────────────────────────────────────┐
  client  ───GET───▶│  /api/v1/transcriptions/{job_id}            │
  (poll)             │    → status, and transcript once completed  │
                    └───────────────────────────────────────────┘
```

Every box on the right-hand side of an arrow (`storage_backend`, `queue_backend`,
`TranscriptionEngine`) is a small interface with exactly one production-grade
implementation swapped in later — see
[What's real vs. mocked](#whats-real-vs-mocked-and-why).

**Code layout:**

```
app/
  main.py                 FastAPI routes, auth/rate-limit dependencies, wiring
  worker.py                TranscriptionPipeline (pure logic) + Worker (queue/retry loop)
  audio.py                 ffmpeg normalization, duration probing, chunking
  transcription_engine.py  WhisperEngine / MockEngine + chunk-merge logic
  store.py                 SQLite-backed job + transcript persistence
  storage_backend.py       Object storage abstraction (local disk today)
  queue_backend.py         Job queue abstraction (in-memory asyncio.Queue today)
  rate_limit.py            Per-API-key sliding-window rate limiter
  schemas.py                Pydantic request/response models
  config.py                 All tunables, one place, env-var driven
  logging_config.py         JSON structured logging
tests/                     18 tests covering audio, merge logic, store, worker retry/dead-letter, rate limiting
```

## API

All routes are under `/api/v1` and require an `X-API-Key` header.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/transcriptions` | Upload an audio file. Returns `202` with `{job_id, status, status_url}` immediately. |
| `GET` | `/api/v1/transcriptions/{job_id}` | Poll status; includes the transcript (text + timestamped segments) once `status=completed`. |
| `GET` | `/api/v1/transcriptions` | List the caller's recent jobs. |
| `GET` | `/healthz` | Unauthenticated liveness probe for a load balancer. |

Full interactive docs (OpenAPI/Swagger) at `/docs` once the app is running.

## Inspecting local data (DB, audio, transcripts)

Everything lands under `STORAGE_ROOT` (`storage/` locally, `/srv/storage`
inside the Docker image — see [config.py](app/config.py)):

- `storage/jobs.db` — SQLite, one row per job (`jobs` table) plus a
  `transcripts` table (see [store.py](app/store.py)'s `SCHEMA`).
- `storage/audio/<job_id>/<original_filename>` — the uploaded audio.
- `storage/transcripts/` — transcripts too large to store inline in the DB
  (over `INLINE_TRANSCRIPT_MAX_CHARS`, default ~20k chars).
- `storage/dead_letter/` — one record per job that exhausted its retries.

**Running natively:** these are plain folders/files next to the repo —
open `storage/jobs.db` in any SQLite browser (e.g.
[DB Browser for SQLite](https://sqlitebrowser.org/)), or query it inline:

```bash
python3 -c "import sqlite3; c=sqlite3.connect('storage/jobs.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('SELECT * FROM jobs')]"
```

**Running in Docker:** if you started the container with a volume mount
(`-v ${PWD}\storage:/srv/storage` on Windows, `-v $(pwd)/storage:/srv/storage`
on macOS/Linux — see [Quick start](#quick-start)), the same files show up
directly in your local `storage/` folder, no different from the native
case. If you *didn't* mount a volume, the data only exists inside that
container's writable layer; reach it via `docker exec`/`docker cp`:

```powershell
docker ps                                  # find the container id/name

docker exec -it <container> python3 -c "import sqlite3; c=sqlite3.connect('/srv/storage/jobs.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('SELECT * FROM jobs')]"

docker cp <container>:/srv/storage/jobs.db .\jobs.db     # pull the DB out to inspect with a GUI tool
docker cp <container>:/srv/storage ./storage-from-container   # or pull everything
```

(The slim Docker base image doesn't ship the `sqlite3` CLI binary, which is
why the examples above use Python's built-in `sqlite3` module instead.)

## Design decisions

This section answers the same questions asked in the assessment form,
mapped directly to what's actually implemented (not just described).

### Accepting audio & validating it

`POST /api/v1/transcriptions` takes `multipart/form-data`, checks the file
extension against an allow-list (`.wav .mp3 .m4a .flac .ogg .mp4`), and
streams the upload to disk with a hard size cap (`MAX_UPLOAD_BYTES`) rather
than buffering the whole file in memory first — see `_save_with_size_limit`
in `app/main.py`. Extension checking is a first line of defense only;
`app/audio.py` normalizes with ffmpeg immediately afterward, which is where
a genuinely corrupt or mislabeled file actually gets rejected (ffmpeg errors
out with a clear message instead of failing deep inside the transcription
call).

### Transcribing speech to text, with per-segment timestamps

`app/transcription_engine.py` wraps Whisper (`model.transcribe(path)`) and
maps its `segments` output to a small `Segment(id, start, end, text)`
dataclass. Timestamps are seconds, rounded to 2 decimal places, matching
the shape asked for in the assessment's own example code.

### Handling different audio formats

Every upload is normalized to 16kHz mono PCM WAV via ffmpeg
(`app/audio.py::normalize_to_wav`) before it ever reaches the transcription
engine — one normalization step, once, rather than teaching every
downstream component about every possible input format. ffmpeg was chosen
over a Python audio library specifically because it already handles the
full format/codec matrix (mp3, wav, m4a, flac, ogg, and mp4's audio track)
without extra dependencies, and it's the same tool most production ASR
pipelines use under the hood.

### Dealing with long audio files

Files longer than `CHUNK_THRESHOLD_SECONDS` (default 5 minutes) are split
into overlapping chunks (`CHUNK_LENGTH_SECONDS` / `CHUNK_OVERLAP_SECONDS`,
default 4 minutes / 5 seconds) by `app/audio.py::split_into_chunks`, each
chunk is transcribed independently with bounded concurrency
(`asyncio.Semaphore(MAX_CONCURRENT_CHUNK_TRANSCRIPTIONS)` in
`app/worker.py::TranscriptionPipeline.run`), and the results are stitched
back together by `merge_chunk_results`. The overlap exists so a word spoken
right at a chunk boundary is fully captured by at least one chunk instead of
being cut in half; `merge_chunk_results` then drops any segment from a
later chunk whose start time falls inside the region the previous chunk
already covered, so the merged transcript has no duplicated or missing
audio at the seams. This is covered directly by
`tests/test_transcription_engine.py::test_merge_chunk_results_dedupes_overlap_and_spans_full_duration`,
which asserts the merged timeline has no gaps, no backwards jumps, and no
duplicate segments.

### Handling concurrent uploads

The API is stateless (no in-process session state tied to a specific
instance), so it already scales horizontally behind a load balancer as-is.
The upload endpoint does the minimum synchronous work — validate, stream to
storage, write one DB row, enqueue one message — and returns `202` in
milliseconds; the actual transcription work happens asynchronously on a
worker pulling from a queue, decoupled from request/response timing
entirely. That decoupling is *the* mechanism that makes concurrent uploads
tractable: N simultaneous uploads become N fast API calls plus N queued
jobs that workers drain at whatever rate they can sustain, instead of N
requests each blocking a connection for however long transcription takes.
In production, `storage_backend.presigned_upload_url` is the next step —
letting the client upload bytes directly to S3 instead of proxying them
through the API process at all.

### Storing audio and transcripts

- **Audio**: saved to object storage (`storage_backend.py`; local disk here,
  S3 in production) under a key derived from the job id, so retrieval never
  depends on which API instance handled the original upload.
- **Metadata**: one row per job in the `jobs` table (`app/store.py`) —
  `id, user_id, original_filename, file_path, duration_seconds, status,
  retry_count, error_code, error_message, failed_at, language,
  trans_version, created_at, updated_at`. This is a direct match for the
  schema described in the assessment answers, just running on SQLite
  instead of PostgreSQL for this demo (see next section).
- **Transcripts**: a separate `transcripts` table, versioned
  (`trans_version` on the job increments on reprocessing). Small transcripts
  are stored inline as JSON text; transcripts over
  `INLINE_TRANSCRIPT_MAX_CHARS` (default ~20k characters) are written to a
  file in `TRANSCRIPT_DIR` instead, with only the path stored in the row —
  see `Worker._persist_transcript`. This keeps the database table small and
  fast to query for the common case (short/medium recordings) while not
  blowing out row size for a two-hour meeting transcript.
- **Encryption**: not implemented in this demo (no KMS available locally),
  but the storage/DB abstractions are exactly the seam where
  encryption-at-rest would be added — S3 server-side encryption for audio,
  a KMS-encrypted column or a database-level encryption feature for
  transcript text containing sensitive content.

### Retrying / recovering failed transcriptions

Every job has a `status` (`queued → processing → completed`, or
`processing → retrying → processing` in a loop, or `→ failed`) and a
`retry_count`. On any exception during processing (`Worker._handle_failure`
in `app/worker.py`):

1. If `retry_count <= MAX_RETRIES`, the job is marked `retrying` with the
   error code/message recorded, and re-enqueued after an exponential
   backoff (`RETRY_BACKOFF_BASE_SECONDS * 2^(attempt-1)`).
2. Once retries are exhausted, the job is marked `failed` (with
   `failed_at` set) and a dead-letter record — job id, error code, error
   message, timestamp — is written to `DEAD_LETTER_DIR` for manual review,
   rather than being silently dropped or retried forever.

This whole lifecycle is exercised by real (not mocked-away) tests in
`tests/test_worker.py`: `test_transient_failures_then_success` proves a job
that fails twice then succeeds ends up `completed` with `retry_count == 2`,
and `test_permanent_failure_goes_to_dead_letter` proves a job that always
fails ends up `failed` with exactly one dead-letter file written.

### Exposing this as an API

- **Auth**: every route (except `/healthz`) requires `X-API-Key`
  (`app/main.py::require_api_key`), checked against a configured key set.
- **Rate limiting**: a per-key sliding-window limiter
  (`app/rate_limit.py`), enforced in the same dependency as auth so no new
  route can accidentally skip it.
- **Validation & error handling**: Pydantic models for every
  request/response; a global exception handler maps domain errors
  (`AudioProcessingError`) to a `422` with a structured
  `{error_code, detail}` body rather than a raw stack trace; standard HTTP
  status codes throughout (`400` bad input, `401` auth, `404` not found,
  `413` too large, `422` unprocessable, `429` rate limited).
- **Versioning**: every route lives under `/api/v1`, so a breaking `v2` can
  ship alongside it.
- **Docs**: FastAPI's built-in OpenAPI/Swagger UI at `/docs` — every
  request/response model above is what generates that documentation, not a
  hand-maintained spec that can drift from the code.
- **Logging**: structured JSON logs (`app/logging_config.py`), so
  `job_id`/`status`/`error_code` are queryable fields in a log aggregator,
  not substrings to grep for.
- **Async & scalable by construction**: heavy work never happens inline in
  a request handler; see "Handling concurrent uploads" above.

## What's real vs. mocked, and why

The assessment explicitly allows mock data/infrastructure where needed, so
here's exactly where this demo simplifies, and what the swap to the real
thing looks like — because *that seam being clean* is itself the design
decision worth evaluating:

| Concern | This repo | Production | Swap cost |
|---|---|---|---|
| Object storage | `LocalDiskStorage` (disk) | S3 / GCS / Azure Blob | New class implementing the same 3-method interface (`app/storage_backend.py`) |
| Job queue | `InMemoryQueue` (`asyncio.Queue`) | SQS / RabbitMQ / Kafka | New class implementing `enqueue`/`dequeue` (`app/queue_backend.py`) |
| Metadata DB | SQLite | PostgreSQL | Same schema (see `store.py`'s `SCHEMA`); swap the connection for `psycopg2`/SQLAlchemy |
| Rate limiter | In-process sliding window | Redis-backed shared counter | Needed as soon as there's more than one API instance — see limitation below |
| Transcription engine | `TRANSCRIPTION_ENGINE=mock` for tests/CI, `whisper` for real use | Same `whisper` engine, possibly a larger model size or a hosted ASR API | One env var; no code change (`app/transcription_engine.py::get_engine`) |

## Scaling this to production

If this were going into production rather than an assessment, in priority
order:

1. **Swap SQLite → PostgreSQL and the in-memory queue → SQS/RabbitMQ/Kafka.**
   These are the two places this demo trades durability for zero-setup
   simplicity — a process restart today loses anything still in the
   in-memory queue. Both interfaces are already isolated exactly so this is
   a config/class swap, not a rewrite.
2. **Run N worker processes/containers, autoscaled on queue depth**, instead
   of the single in-process worker task started in `main.py`'s lifespan —
   the `Worker` class already has no dependency on being in the same
   process as the API, it just currently is for demo simplicity.
3. **Move the rate limiter to Redis** so it's correct across multiple API
   instances instead of per-process (see limitations below).
4. **Direct-to-storage uploads via presigned URLs** (`storage_backend.
   presigned_upload_url` is already the intended seam) so large files never
   pass through the API process's memory/disk at all.
5. **Add authentication beyond a static API key** — OAuth2/JWT with actual
   user identity, since `user_id` today is just the raw API key.
6. **Encryption at rest** for both audio and transcript storage.
7. **Per-chunk resumable retry** for very long files — today a failed job
   retries the whole file from scratch; for a 2-hour recording split into
   30 chunks, retrying only the chunk(s) that actually failed would be a
   meaningful efficiency win.

## Testing

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

18 tests, all currently passing, none requiring FastAPI or Whisper to be
installed (they exercise the pipeline logic directly, using the `MockEngine`
and real ffmpeg calls against synthetically generated tones) — deliberately
so they run fast in CI and don't need a multi-GB model download to prove the
*engineering* is correct:

- `test_audio.py` — ffmpeg normalization, duration probing, chunk offsets/overlap
- `test_transcription_engine.py` — deterministic mock engine, chunk-merge correctness (no gaps, no duplicate segments, full-duration coverage)
- `test_store.py` — job status lifecycle, transcript persistence, per-user listing
- `test_worker.py` — retry-then-succeed, permanent-failure-to-dead-letter, and the long-audio chunked path, end to end through the real `Worker`
- `test_rate_limit.py` — per-key sliding window behavior

The FastAPI HTTP layer (`app/main.py`) itself is not covered by an automated
test in this submission — it was hand-verified for correctness (route
signatures, dependency wiring, status codes) but couldn't be exercised with
`TestClient` in the environment this was built in. Given more time, I'd add
`tests/test_api.py` using FastAPI's `TestClient` against the `mock` engine
to cover the upload → poll → completed round trip and the auth/rate-limit/
validation error paths at the HTTP layer specifically.

## Known limitations / what I'd do with more time

- **In-memory queue loses unprocessed jobs on restart.** Acceptable for a
  demo; unacceptable in production — see "Scaling this to production" #1.
- **Rate limiter is per-process**, so it under-limits once there's more than
  one API instance behind a load balancer — see #3 above.
- **Retry re-runs the whole job**, not just the failed chunk, for chunked
  (long-audio) jobs — see #7 above.
- **No authentication beyond a static API key** — fine for this assessment,
  not fine for real user-facing multi-tenant auth.
- **No HTTP-layer automated tests** (see Testing section) — the highest-value
  next addition given more time.
