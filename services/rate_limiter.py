import asyncio
import time

from core.config import SLACK_BOT_TOKEN, is_test_environment


class SlackRateLimiter:
    """
    Asynchronous token bucket rate limiter for outbound Slack API calls.
    Protects against Slack HTTP 429 Too Many Requests during multi-agent discussions.
    Completely bypassed in test environments or when running with mock tokens.
    """

    def __init__(self, rate: float = 10.0, capacity: float = 15.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        if (
            is_test_environment()
            or not SLACK_BOT_TOKEN
            or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        ):
            return

        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < 1.0:
                needed = 1.0 - self.tokens
                delay = needed / self.rate
                await asyncio.sleep(delay)
                self.tokens = 0.0
                self.last_refill = time.monotonic()
            else:
                self.tokens -= 1.0


# Global singleton rate limiter
slack_rate_limiter = SlackRateLimiter()

__all__ = ["SlackRateLimiter", "slack_rate_limiter"]
