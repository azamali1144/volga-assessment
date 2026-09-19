"""
Job queue abstraction.

`InMemoryQueue` (asyncio.Queue) stands in for SQS/RabbitMQ/Kafka here - a
single-process demo has no need for a distributed broker, but the interface
is deliberately the same shape a real one would have (enqueue / dequeue /
ack-by-completion), so the worker in app/worker.py doesn't change at all
when this is swapped for `aiobotocore` against real SQS, or `aio-pika`
against RabbitMQ.

What a real broker adds that this mock doesn't: durability across process
restarts (a queued job here is lost if the process dies before a worker
picks it up - in production this is the difference between an in-memory
queue and SQS/RabbitMQ persisting the message to disk), and multi-worker /
multi-host fan-out. Both are called out explicitly in the README.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class JobMessage:
    job_id: str
    attempt: int = 1


class InMemoryQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[JobMessage] = asyncio.Queue()

    async def enqueue(self, job_id: str, attempt: int = 1) -> None:
        await self._queue.put(JobMessage(job_id=job_id, attempt=attempt))

    async def dequeue(self) -> JobMessage:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()
