"""Concurrency control: the part that breaks first at 50 concurrent leads.

Two layers, both deliberate:
  1. TokenBucket  -- caps *rate* (requests/minute) per upstream API.
  2. Semaphore    -- caps *parallelism* across leads (set in the run loop).

Plus `with_backoff`, an exponential-backoff-with-jitter retry wrapper for
429s and transient 5xxs. Retry counts are tracked so you can report
"the day Tavily rate-limited us" with actual numbers.
"""
import asyncio
import logging
import random
import time

log = logging.getLogger("leadloom.ratelimit")


class TokenBucket:
    """Classic token bucket: `rate_per_minute` tokens refill continuously."""

    def __init__(self, rate_per_minute: int, name: str = "bucket"):
        self.capacity = float(rate_per_minute)
        self.tokens = float(rate_per_minute)
        self.rate_per_sec = rate_per_minute / 60.0
        self.updated = time.monotonic()
        self.name = name
        self._lock = asyncio.Lock()
        self.waits = 0  # times a caller had to sleep — surfaced in /api/stats

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_sec)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate_per_sec
                self.waits += 1
                log.debug("%s throttled, sleeping %.2fs", self.name, wait)
                await asyncio.sleep(wait)


class RetryStats:
    def __init__(self):
        self.retries = 0
        self.rate_limit_hits = 0


retry_stats = RetryStats()


class RateLimitedError(Exception):
    """Raise this from a call site to signal a retryable failure (429/5xx)."""


async def with_backoff(fn, *, max_retries: int, base_delay: float = 1.0, what: str = "call"):
    """Run `fn()` (async, no args) with exponential backoff + full jitter."""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except RateLimitedError as e:
            retry_stats.rate_limit_hits += 1
            if attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt) * (0.5 + random.random())
            retry_stats.retries += 1
            log.warning("%s hit rate limit (attempt %d), backing off %.1fs: %s",
                        what, attempt + 1, delay, e)
            await asyncio.sleep(delay)
