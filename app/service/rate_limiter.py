"""Client-side pacing for the notification provider.

The provider accepts at most 50 requests in any sliding 10 s window and rejects the rest with
429. A limiter with rate ``r`` and burst ``b`` admits at most ``b + r * T`` requests in any
window of length ``T``; with the defaults (r = 4.5/s, b = 1) that is 46 per 10 s, safely under
50. Pacing ourselves is cheaper than absorbing 429s: a rejected call wastes a round trip and
still has to be retried.

``cooldown`` pauses everyone after a 429 anyway: another client (e.g. the provider-check
service) may share the same quota.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Token bucket implemented as GCRA (generic cell rate algorithm).

    Instead of topping up a fractional token count (which drifts with floating point and can
    leave a waiter re-sleeping for ever-smaller intervals), it tracks the theoretical arrival
    time ``_tat`` of the next request. A request may go at ``t`` if ``t >= _tat - tolerance``;
    sending pushes ``_tat`` one interval further. A waiter sleeps once until its slot and takes
    it, unless a cooldown moved the schedule while it slept.
    """

    def __init__(
        self,
        rate_per_sec: float,
        capacity: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_sec <= 0 or capacity < 1:
            raise ValueError("rate_per_sec must be > 0 and capacity >= 1")
        self._interval = 1.0 / rate_per_sec
        self._tolerance = (capacity - 1) * self._interval  # how far ahead of schedule a burst may go
        self._clock = clock
        self._sleep = sleep
        self._tat = clock()
        # Waiters queue on the lock (FIFO), so slots go to whoever asked first.
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request may be sent, then claim the slot."""
        async with self._lock:
            while True:
                now = self._clock()
                send_at = max(now, self._tat - self._tolerance)
                if send_at <= now:
                    break
                schedule = self._tat
                await self._sleep(send_at - now)
                if self._tat == schedule:  # no cooldown while we slept: the slot is ours
                    break
            self._tat = max(self._tat, max(self._clock(), send_at)) + self._interval

    def cooldown(self, seconds: float) -> None:
        """Pause all sends for ``seconds``; afterwards resume at the normal rate, without a burst."""
        resume_at = self._clock() + seconds
        self._tat = max(self._tat, resume_at + self._tolerance)
