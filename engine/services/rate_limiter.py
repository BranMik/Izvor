import asyncio
import time

from core.config import get_slack_bot_token


def _get_rate_limiter_defaults() -> tuple[float, float]:
    try:
        from config_store.system_settings import system_settings_manager
        return (
            system_settings_manager.get_slack_rate_limit_per_second(),
            system_settings_manager.get_slack_rate_limit_burst(),
        )
    except Exception:  # noqa: BLE001
        return 10.0, 15.0


class SlackRateLimiter:
    """
    Asynchronous token bucket rate limiter for outbound Slack API calls.
    Protects against Slack HTTP 429 Too Many Requests during multi-agent discussions.
    Completely bypassed in test environments or when running with mock tokens.
    """

    def __init__(self, rate: float | None = None, capacity: float | None = None):
        def_rate, def_capacity = _get_rate_limiter_defaults()
        self.rate = float(rate if rate is not None else def_rate)
        self.capacity = float(capacity if capacity is not None else def_capacity)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        token = get_slack_bot_token()
        if (
            not token
            or token == "xoxb-your-bot-user-oauth-token"
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


class SlidingWindowIPRateLimiter:
    """
    Thread-safe sliding-window rate limiter keyed by client IP address.
    Enforces maximum memory bounds with automatic eviction of expired entries,
    and suppresses redundant audit logging during sustained brute-force / 429 flood attacks.
    """

    def __init__(self, max_entries: int = 10000) -> None:
        import threading
        self._lock = threading.Lock()
        self._entries: dict[str, list[float]] = {}
        self._logged_block: set[str] = set()
        self._max_entries = max_entries

    def _prune_ip_locked(self, client_ip: str, window_seconds: float, now: float) -> list[float]:
        timestamps = [t for t in self._entries.get(client_ip, []) if now - t < window_seconds]
        if timestamps:
            self._entries[client_ip] = timestamps
        else:
            self._entries.pop(client_ip, None)
            self._logged_block.discard(client_ip)
        return timestamps

    def is_blocked(
        self,
        client_ip: str,
        max_requests: int = 5,
        window_seconds: float = 60.0,
    ) -> tuple[bool, int]:
        """
        Read-only pre-check that prunes expired timestamps and returns
        (is_currently_blocked, attempts_in_window) before credential comparison.
        """
        now = time.monotonic()
        with self._lock:
            timestamps = self._prune_ip_locked(client_ip, window_seconds, now)
            count = len(timestamps)
            return count >= max_requests, count

    def mark_block_logged(self, client_ip: str) -> bool:
        """Marks rate-limit block audit as logged for client_ip; returns True once per block window."""
        with self._lock:
            if client_ip not in self._logged_block:
                self._logged_block.add(client_ip)
                return True
            return False

    def check_and_record(
        self,
        client_ip: str,
        max_requests: int = 5,
        window_seconds: float = 60.0,
    ) -> tuple[bool, int, bool]:
        """
        Records an attempt and returns:
          - is_allowed (bool): True if under limit, False if rate limited.
          - current_count (int): Total attempts in the active window.
          - should_log_audit (bool): True if this transition to blocked should emit an audit event.
        """
        now = time.monotonic()
        with self._lock:
            # Capacity guard: prune expired entries if over capacity
            if len(self._entries) >= self._max_entries and client_ip not in self._entries:
                expired = [
                    k for k, timestamps in self._entries.items()
                    if not timestamps or (now - timestamps[-1]) >= window_seconds
                ]
                for k in expired:
                    self._entries.pop(k, None)
                    self._logged_block.discard(k)

                # If still over capacity, evict oldest entry
                if len(self._entries) >= self._max_entries:
                    oldest_k = min(self._entries.keys(), key=lambda k: self._entries[k][-1] if self._entries[k] else 0.0)
                    self._entries.pop(oldest_k, None)
                    self._logged_block.discard(oldest_k)

            timestamps = [t for t in self._entries.get(client_ip, []) if now - t < window_seconds]
            timestamps.append(now)
            self._entries[client_ip] = timestamps
            count = len(timestamps)

            if count > max_requests:
                # Rate-limited
                if client_ip not in self._logged_block:
                    self._logged_block.add(client_ip)
                    return False, count, True
                return False, count, False

            # Within limits
            self._logged_block.discard(client_ip)
            return True, count, False

    def reset(self, client_ip: str | None = None) -> None:
        with self._lock:
            if client_ip is not None:
                self._entries.pop(client_ip, None)
                self._logged_block.discard(client_ip)
            else:
                self._entries.clear()
                self._logged_block.clear()


ip_rate_limiter = SlidingWindowIPRateLimiter()

__all__ = ["SlackRateLimiter", "slack_rate_limiter", "SlidingWindowIPRateLimiter", "ip_rate_limiter"]

