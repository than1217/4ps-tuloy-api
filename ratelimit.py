"""In-process fixed-window rate limiting.

Single-process only: with several workers or hosts each keeps its own counters, so the
effective limit multiplies. Put a shared limiter (gateway or Redis) in front for production.
"""
import hashlib
import threading
import time

from errors import ApiError


class FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60, max_keys: int = 50_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._counts: dict = {}

    def hit(self, key: str) -> tuple:
        """Count one request for key. Returns (allowed, seconds_until_reset)."""
        if self.limit <= 0:
            return True, 0
        key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        now = time.monotonic()
        with self._lock:
            start, count = self._counts.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            count += 1
            if len(self._counts) >= self.max_keys and key not in self._counts:
                self._counts = {k: v for k, v in self._counts.items() if now - v[0] < self.window}
                if len(self._counts) >= self.max_keys:
                    # Still full of live windows: refuse new keys rather than grow without bound.
                    return False, self.window
            self._counts[key] = (start, count)
            return count <= self.limit, max(1, int(self.window - (now - start)))

    def check(self, key: str) -> None:
        allowed, retry_after = self.hit(key)
        if not allowed:
            raise ApiError(429, "RATE_LIMITED", "Too many requests", headers={"Retry-After": str(retry_after)})

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


# Login attempts per client IP + username: slows password guessing.
login_limiter = FixedWindowLimiter(limit=10, window_seconds=300)
