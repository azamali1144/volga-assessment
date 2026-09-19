"""
A minimal in-process, per-API-key rate limiter (sliding window over a fixed
number of one-minute buckets).

This is deliberately simple and has one known limitation, called out here
rather than hidden: it's per-process state, so it only rate-limits
correctly with a single API instance. Behind a load balancer with N
instances, this needs to move to a shared store (Redis INCR + TTL is the
standard pattern) so all instances share one counter per key - noted in the
README as the production upgrade path.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit_per_minute = limit_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.limit_per_minute:
            return False
        hits.append(now)
        return True
