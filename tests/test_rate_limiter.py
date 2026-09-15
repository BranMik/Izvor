import asyncio
import time
from unittest.mock import patch

from services.rate_limiter import SlackRateLimiter


def test_rate_limiter_bypass_when_mock_token():
    async def _run():
        limiter = SlackRateLimiter(rate=1.0, capacity=1.0)
        # By default in pytest, acquire is bypassed immediately
        start = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    asyncio.run(_run())


def test_rate_limiter_token_consumption_and_refill():
    async def _run():
        limiter = SlackRateLimiter(rate=5.0, capacity=2.0)
        # Simulate production environment: real token and remove "pytest" from sys.modules check
        with patch("services.rate_limiter.SLACK_BOT_TOKEN", "xoxb-real-production-token-123"):
            with patch("sys.modules", {}):
                # Initially tokens = 2.0
                assert limiter.tokens == 2.0

                # Acquire 1 token
                await limiter.acquire()
                assert limiter.tokens <= 1.0

                # Acquire second token immediately
                await limiter.acquire()
                assert limiter.tokens < 0.1

                # Third token requires refill delay (1.0 / 5.0 = 0.2s)
                start = time.monotonic()
                await limiter.acquire()
                elapsed = time.monotonic() - start
                assert elapsed >= 0.15

    asyncio.run(_run())


def test_rate_limiter_concurrency():
    async def _run():
        limiter = SlackRateLimiter(rate=10.0, capacity=5.0)
        with patch("services.rate_limiter.SLACK_BOT_TOKEN", "xoxb-real-production-token-123"):
            with patch("sys.modules", {}):
                # Launch 5 tasks concurrently
                tasks = [limiter.acquire() for _ in range(5)]
                await asyncio.gather(*tasks)
                assert limiter.tokens <= 0.5

    asyncio.run(_run())
