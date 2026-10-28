import asyncio
from itertools import pairwise

import pytest

from service.rate_limiter import TokenBucket

PROVIDER_LIMIT, PROVIDER_WINDOW = 50, 10.0


class FakeClock:
    """Virtual time: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return FakeClock()


def bucket(clock, rate=4.5, capacity=1) -> TokenBucket:
    return TokenBucket(rate, capacity, clock=clock, sleep=clock.sleep)


async def acquire_times(limiter: TokenBucket, clock: FakeClock, n: int) -> list[float]:
    times = []
    for _ in range(n):
        await limiter.acquire()
        times.append(clock.now)
    return times


def max_in_any_window(times: list[float], window: float) -> int:
    # A window ending at each acquisition is enough to find the maximum.
    return max(sum(1 for t in times if end - window < t <= end) for end in times)


async def test_first_request_is_immediate(clock):
    limiter = bucket(clock)
    await limiter.acquire()
    assert clock.now == 1000.0


async def test_steady_rate(clock):
    times = await acquire_times(bucket(clock, rate=4.5), clock, 10)
    gaps = [b - a for a, b in pairwise(times)]
    assert all(gap == pytest.approx(1 / 4.5) for gap in gaps)


async def test_default_settings_never_exceed_the_provider_window(clock):
    times = await acquire_times(bucket(clock, rate=4.5, capacity=1), clock, 300)
    assert max_in_any_window(times, PROVIDER_WINDOW) < PROVIDER_LIMIT


async def test_idle_time_saves_at_most_capacity_tokens(clock):
    limiter = bucket(clock, rate=1, capacity=3)
    await acquire_times(limiter, clock, 3)  # drain
    clock.now += 60  # long idle period

    start = clock.now
    times = await acquire_times(limiter, clock, 5)

    assert times[:3] == [start] * 3  # burst of exactly `capacity`
    assert times[3] == pytest.approx(start + 1)


async def test_concurrent_waiters_are_served_in_order_at_the_rate(clock):
    limiter = bucket(clock, rate=2)
    served = []

    async def worker(name):
        await limiter.acquire()
        served.append((name, clock.now))

    await asyncio.gather(*(worker(i) for i in range(6)))

    assert [name for name, _ in served] == list(range(6))
    assert [t - 1000.0 for _, t in served] == pytest.approx([0, 0.5, 1.0, 1.5, 2.0, 2.5])


async def test_cooldown_pauses_and_prevents_a_burst_afterwards(clock):
    limiter = bucket(clock, rate=1, capacity=5)
    limiter.cooldown(2.0)

    times = await acquire_times(limiter, clock, 2)

    assert times[0] == pytest.approx(1002.0)
    assert times[1] == pytest.approx(1003.0)  # saved-up tokens were dropped


async def test_cooldown_never_shortens_an_existing_pause(clock):
    limiter = bucket(clock)
    limiter.cooldown(5.0)
    limiter.cooldown(1.0)
    await limiter.acquire()
    assert clock.now == pytest.approx(1005.0)


async def test_cooldown_during_a_wait_extends_it(clock):
    limiter = bucket(clock, rate=1)
    await limiter.acquire()  # bucket now empty, next token at +1 s

    async def pause_soon():  # first runs while acquire() is suspended in its sleep
        limiter.cooldown(3.0)

    await asyncio.gather(limiter.acquire(), pause_soon())
    assert clock.now >= 1003.0


@pytest.mark.parametrize("rate, capacity", [(0, 1), (-1, 1), (1, 0)])
def test_invalid_configuration(rate, capacity):
    with pytest.raises(ValueError, match="rate_per_sec"):
        TokenBucket(rate, capacity)


async def test_no_float_drift_over_many_acquisitions(clock):
    """Regression: a fractional-token bucket re-slept ~1e-14 s forever once the clock stopped moving."""
    sleeps = 0

    async def counting_sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        await clock.sleep(seconds)

    limiter = TokenBucket(4.5, 1, clock=clock, sleep=counting_sleep)
    for _ in range(1000):
        await limiter.acquire()

    assert sleeps == 999  # exactly one sleep per paced acquisition
    assert clock.now == pytest.approx(1000.0 + 999 / 4.5)


async def test_real_clock_never_runs_faster_than_the_rate():
    # OS timer granularity (~15.6 ms on Windows) can only make real sleeps longer, i.e. slower.
    limiter = TokenBucket(50, 1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(6):
        await limiter.acquire()
    elapsed = loop.time() - start
    assert 5 / 50 - 0.02 <= elapsed < 0.5
