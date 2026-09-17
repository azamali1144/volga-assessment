# Video walkthrough — speaking notes

The form asks for a short video in English explaining your thought process,
key decisions, and how the code works, while showing your code. This is a
speaking outline, not a script to read verbatim — say it in your own words,
but hit every bullet. Target length: 6–9 minutes. Have the repo open in your
editor and `README.md` open in a second tab before you hit record.

Suggested screen order: README.md architecture diagram → app/main.py →
app/worker.py → app/audio.py → app/transcription_engine.py → app/store.py →
tests running in the terminal.

---

## 1. Restate the problem (30–45 sec)

"The task was to build a transcription pipeline: accept an audio file,
transcribe it to text with timestamps, and think through the engineering
decisions around formats, long files, concurrency, storage, retries, and
exposing it as an API — not to train or fine-tune a model."

## 2. High-level architecture (1–1.5 min)

Show the diagram in README.md while you talk through it.

"The core decision I made early was: the upload request and the actual
transcription work should not happen in the same request/response cycle.
Transcription can take anywhere from a few seconds to several minutes
depending on file length, and holding an HTTP connection open for that long
doesn't scale and isn't resilient to timeouts. So the API does the minimum
synchronous work — validate the file, save it, write a job row, enqueue a
message — and returns 202 Accepted with a job id immediately. A background
worker consumes the queue and does the actual transcription. The client
polls for status."

"Three things in this design are explicitly abstracted behind small
interfaces, because in a real deployment they'd be swapped for managed
services: object storage — local disk here, S3 in production; the job
queue — an in-memory asyncio.Queue here, SQS or RabbitMQ in production; and
the metadata database — SQLite here, Postgres in production. I'll show why
that matters when we look at the code."

## 3. Walk through the upload endpoint — app/main.py (1.5–2 min)

Open `app/main.py`, scroll to `create_transcription`.

"This is the upload endpoint. It validates the extension against an
allow-list, then streams the file to disk with a hard size cap instead of
buffering the whole upload into memory — that matters once you're accepting
files that could be hundreds of megabytes. It generates the job id up
front, so the same id is used both as the storage key and the database
primary key — one identifier for the job everywhere, rather than
reconciling two different ids later. Then it creates the job row with
status 'queued', enqueues the job id, and returns immediately."

"Every route goes through `require_api_key` as a dependency — that's where
auth and rate limiting live, so a new route can't accidentally be added
without them. I used a simple per-key sliding-window limiter for this demo
and explicitly noted in the README that it needs to move to something
shared like Redis once there's more than one API instance, because
right now it's per-process state."

## 4. Walk through the worker — app/worker.py (2–2.5 min)

Open `app/worker.py`.

"This is where the actual pipeline runs. `TranscriptionPipeline.run` is the
pure logic: normalize the audio, check its duration, and if it's under the
chunk threshold — five minutes by default — transcribe it in one call. If
it's longer, split it into overlapping chunks and transcribe them
concurrently with a bounded semaphore, then merge the results back into one
transcript."

"I kept `TranscriptionPipeline` completely separate from `Worker` on
purpose — the pipeline has no idea a queue or a database exists, it just
takes an audio path and returns a transcript. That's what let me unit-test
the whole normalize-chunk-transcribe-merge flow directly, without spinning
up the web layer or a real queue at all."

"`Worker` is the part that knows about the queue, the job store, and retry
policy. [Point at `_handle_failure`.] On any exception, if the job hasn't
exceeded `MAX_RETRIES`, it's marked 'retrying' and re-queued with
exponential backoff. Once retries are exhausted, it's marked 'failed' and I
write a dead-letter record — job id, error code, error message, timestamp —
to a directory for manual review, so a failing job doesn't just disappear or
retry forever."

*(Optional, if time allows: run `python -m unittest tests.test_worker -v`
live and point at the retry-then-succeed and dead-letter tests passing.)*

## 5. Long files and format handling — app/audio.py (1–1.5 min)

Open `app/audio.py`, scroll to `split_into_chunks`.

"For different formats — mp3, wav, m4a, flac, ogg, even the audio track of
an mp4 — I normalize everything to 16kHz mono WAV with ffmpeg before it
reaches the transcription engine, so nothing downstream has to think about
input format again. For long files, I split into overlapping chunks — the
overlap is what stops a word right at a chunk boundary from being cut in
half. On the merge side [switch to `transcription_engine.py`,
`merge_chunk_results`], I shift each chunk's local timestamps by its
offset in the original file, and drop any segment from a later chunk that
falls inside the region the previous chunk already covered, so the final
transcript doesn't have duplicated or missing audio at the seams."

## 6. What's mocked and why (45 sec – 1 min)

"I want to be upfront about what's simplified here rather than pretend it's
production-ready: SQLite instead of Postgres, local disk instead of S3, an
in-memory queue instead of SQS or RabbitMQ. All three are explicitly called
out in the README with exactly what the swap to the real thing looks like,
because I think the interface being clean is the actual engineering
decision being tested here, not whether I had an AWS account for this demo."

## 7. Tests (30–45 sec)

Run `python -m unittest discover -s tests -p "test_*.py" -v` on screen.

"18 tests, all passing, covering the chunk offset/overlap math, the merge
logic end to end — no gaps, no duplicate segments, full duration covered —
the job status lifecycle including retries and dead-lettering, and the rate
limiter. These don't need FastAPI or Whisper installed, they exercise the
pipeline logic directly, which is deliberate — it's what let me verify all
of this actually works rather than just reads correctly."

## 8. Close (20–30 sec)

"If I were taking this further, the README has a 'scaling to production'
section — the short version is: swap SQLite and the in-memory queue for
Postgres and a real broker, run multiple worker instances autoscaled on
queue depth, move the rate limiter to Redis, and add per-chunk resumable
retry for very long files instead of retrying the whole job. Happy to go
deeper into any part of this."

---

### Recording checklist

- [ ] Screen share on, resolution readable, terminal font large enough
- [ ] Recorded in English
- [ ] Shows actual code while explaining (not just talking over a blank screen)
- [ ] Under ~10 minutes
- [ ] Uploaded / linked per the form's instructions
