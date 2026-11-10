import asyncio
import time

from core.config import configure_token, reset_token
from services.rate_limiter import SlackRateLimiter


def test_rate_limiter_bypass_when_mock_token():
    async def _run():
        limiter = SlackRateLimiter(rate=1.0, capacity=1.0)
        # When SLACK_BOT_TOKEN is unset or placeholder, acquire is bypassed immediately
        configure_token("xoxb-your-bot-user-oauth-token")
        try:
            start = time.monotonic()
            await limiter.acquire()
            await limiter.acquire()
            elapsed = time.monotonic() - start
            assert elapsed < 0.1
        finally:
            reset_token()

    asyncio.run(_run())


def test_rate_limiter_token_consumption_and_refill():
    async def _run():
        limiter = SlackRateLimiter(rate=5.0, capacity=2.0)
        # With real token, rate limiting engages
        configure_token("xoxb-real-production-token-123")
        try:
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
        finally:
            reset_token()

    asyncio.run(_run())


def test_rate_limiter_concurrency():
    async def _run():
        limiter = SlackRateLimiter(rate=10.0, capacity=5.0)
        configure_token("xoxb-real-production-token-123")
        try:
            # Launch 5 tasks concurrently
            tasks = [limiter.acquire() for _ in range(5)]
            await asyncio.gather(*tasks)
            assert limiter.tokens <= 0.5
        finally:
            reset_token()

    asyncio.run(_run())
