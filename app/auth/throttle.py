"""In-memory login throttling (counters reset on restart; accepted risk).

- Per client IP: 5 failures in 15 minutes -> 429 with Retry-After. Attempts being verified
  count as pending failures, so concurrent attempts cannot exceed the limit.
- Global: once failures across all IPs exceed 50 in 15 minutes, every login attempt (right or
  wrong, any IP) first waits ``min(2s * (failures - 50), 30s)``, where ``failures`` counts the
  current attempt, so the 51st failure waits 2s. It never locks out: after the wait a correct
  password always logs in. The wait is ``asyncio.sleep`` so other requests keep being served.
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_SECONDS = 15 * 60
PER_IP_LIMIT = 5
GLOBAL_THRESHOLD = 50
DELAY_STEP_SECONDS = 2.0
DELAY_CAP_SECONDS = 30.0
_MAX_TRACKED_IPS = 1024  # sweep empty per-IP entries beyond this many keys


class LoginThrottle:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self._per_ip: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._pending: dict[str, int] = {}  # attempts reserved per IP, still being verified

    def _prune(self, failures: deque[float], now: float) -> None:
        while failures and failures[0] <= now - WINDOW_SECONDS:
            failures.popleft()

    def retry_after(self, ip: str) -> int | None:
        """Seconds until ``ip`` may try again, or None when it is not blocked."""
        failures = self._per_ip.get(ip, deque())
        now = self.clock()
        self._prune(failures, now)
        if len(failures) + self._pending.get(ip, 0) < PER_IP_LIMIT:
            return None
        if len(failures) < PER_IP_LIMIT:
            return 1  # blocked only by in-flight attempts, which resolve soon
        return max(1, math.ceil(failures[0] + WINDOW_SECONDS - now))

    def reserve(self, ip: str) -> None:
        """Count an attempt as a pending failure for ``ip`` until ``release`` (call right after
        a passing ``retry_after``, with no await in between)."""
        self._pending[ip] = self._pending.get(ip, 0) + 1

    def release(self, ip: str) -> None:
        if self._pending[ip] == 1:
            del self._pending[ip]
        else:
            self._pending[ip] -= 1

    def global_failures(self) -> int:
        self._prune(self._global, self.clock())
        return len(self._global)

    def delay_seconds(self) -> float:
        extra = self.global_failures() + 1 - GLOBAL_THRESHOLD
        return min(DELAY_STEP_SECONDS * extra, DELAY_CAP_SECONDS) if extra > 0 else 0.0

    async def wait(self) -> float:
        """Serve the global progressive delay (if any) before the password check."""
        delay = self.delay_seconds()
        if delay:
            await self.sleep(delay)
        return delay

    def record_failure(self, ip: str) -> None:
        now = self.clock()
        self._global.append(now)
        self._prune(self._global, now)
        if len(self._per_ip) > _MAX_TRACKED_IPS:
            for key in list(self._per_ip):
                self._prune(self._per_ip[key], now)
                if not self._per_ip[key]:
                    del self._per_ip[key]
        failures = self._per_ip.setdefault(ip, deque())
        failures.append(now)
        self._prune(failures, now)

    def record_success(self, ip: str) -> None:
        self._per_ip.pop(ip, None)
