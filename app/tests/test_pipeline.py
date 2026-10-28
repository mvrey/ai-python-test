import asyncio
import random

import pytest

from service.ai_client import AIRequestError
from service.config import Settings
from service.models import NotificationPayload, Stage, Status
from service.notifier import NotifyError
from service.pipeline import Pipeline
from service.store import InMemoryRequestStore

EMAIL_INPUT = "Manda un correo a marta@app.com: Tienes un nuevo mensaje."
GOOD_ANSWER = '{"to": "marta@app.com", "message": "Tienes un nuevo mensaje.", "type": "email"}'
REFUSAL = "Lo siento, como IA no tengo permitido procesar datos de contacto personales."
PAYLOAD = NotificationPayload(to="marta@app.com", message="Tienes un nuevo mensaje.", type="email")


# --- fakes ----------------------------------------------------------------------------------


class FakeExtractor:
    """Answers from a script; the last entry repeats. Entries are strings or exceptions."""

    def __init__(self, *script):
        self.script = list(script) or [GOOD_ANSWER]
        self.calls: list[list[dict]] = []

    async def extract(self, messages):
        self.calls.append(messages)
        answer = self.script[min(len(self.calls), len(self.script)) - 1]  # fixed before yielding
        await asyncio.sleep(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeSender:
    def __init__(self, *script):
        self.script = list(script) or ["p-1"]
        self.calls: list[tuple[NotificationPayload, str]] = []

    async def send(self, payload, trace_id):
        self.calls.append((payload, trace_id))
        outcome = self.script[min(len(self.calls), len(self.script)) - 1]  # fixed before yielding
        await asyncio.sleep(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeLimiter:
    def __init__(self):
        self.acquired = 0
        self.cooldowns: list[float] = []

    async def acquire(self):
        self.acquired += 1

    def cooldown(self, seconds):
        self.cooldowns.append(seconds)


class Harness:
    def __init__(self, extractor=None, sender=None, **settings):
        self.store = InMemoryRequestStore()
        self.extractor = extractor or FakeExtractor()
        self.sender = sender or FakeSender()
        self.limiter = FakeLimiter()
        self.sleeps: list[float] = []
        defaults = {"extraction_workers": 3, "notify_workers": 2}
        self.pipeline = Pipeline(
            Settings(**{**defaults, **settings}),
            self.store,
            self.extractor,
            self.sender,
            self.limiter,
            sleep=self._sleep,
            rng=random.Random(0),
        )

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def submit(self, user_input=EMAIL_INPUT) -> str:
        request_id = self.store.create(user_input).id
        self.store.transition(request_id, Stage.EXTRACTION_PENDING)
        assert self.pipeline.submit(request_id)
        return request_id

    async def wait_terminal(self, *request_ids, timeout=2.0):
        async def all_done():
            while not all(self.store.get(r).stage.is_terminal for r in request_ids):
                await asyncio.sleep(0.001)

        await asyncio.wait_for(all_done(), timeout)
        return [self.store.get(r) for r in request_ids]


@pytest.fixture
async def make():
    harnesses: list[Harness] = []

    async def factory(*args, **kwargs) -> Harness:
        harness = Harness(*args, **kwargs)
        await harness.pipeline.start()
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.pipeline.stop()


# --- happy path ---------------------------------------------------------------------------------


async def test_happy_path(make):
    h = await make()
    request_id = h.submit()

    (record,) = await h.wait_terminal(request_id)

    assert record.status is Status.SENT
    assert (record.ai_attempts, record.notify_attempts, record.provider_id) == (1, 1, "p-1")
    assert record.extraction == PAYLOAD
    assert h.sender.calls == [(PAYLOAD, request_id)]  # trace_id is the request id
    assert h.limiter.acquired == 1
    assert h.extractor.calls[0][-1] == {"role": "user", "content": EMAIL_INPUT}


async def test_many_requests_all_get_sent_through_the_limiter(make):
    h = await make()
    ids = [h.submit() for _ in range(50)]

    records = await h.wait_terminal(*ids)

    assert all(r.status is Status.SENT for r in records)
    assert h.limiter.acquired == 50 == len(h.sender.calls)
    assert h.pipeline.stats["sent"] == 50


# --- AI stage ------------------------------------------------------------------------------------


async def test_refusal_then_valid_answer_retries_once_with_a_hint(make):
    h = await make(FakeExtractor(REFUSAL, GOOD_ANSWER))
    (record,) = await h.wait_terminal(h.submit())

    assert record.status is Status.SENT
    assert record.ai_attempts == 2
    first, retry = h.extractor.calls
    assert len(retry) == len(first) + 1
    assert "refused" in retry[-2]["content"]
    assert retry[-1]["content"] == EMAIL_INPUT
    assert h.sleeps == []  # a bad answer is retried immediately


async def test_attempt_budget_is_a_hard_limit(make):
    h = await make(FakeExtractor(REFUSAL))
    (record,) = await h.wait_terminal(h.submit())

    assert record.status is Status.FAILED
    assert record.error == "refusal"
    assert record.ai_attempts == len(h.extractor.calls) == 2
    assert h.sender.calls == []


async def test_budget_is_shared_between_transport_errors_and_bad_answers(make):
    h = await make(FakeExtractor(AIRequestError("timeout", retryable=True), REFUSAL, GOOD_ANSWER))
    (record,) = await h.wait_terminal(h.submit())

    assert record.status is Status.FAILED
    assert record.error == "refusal"
    assert len(h.extractor.calls) == 2


async def test_transient_ai_error_is_retried_after_a_backoff(make):
    h = await make(FakeExtractor(AIRequestError("http_503", retryable=True), GOOD_ANSWER))
    (record,) = await h.wait_terminal(h.submit())

    assert record.status is Status.SENT
    assert len(h.sleeps) == 1 and 0.25 <= h.sleeps[0] < 0.5  # base 0.5 s with jitter
    assert len(h.extractor.calls[1]) == len(h.extractor.calls[0])  # no hint: the model never answered


async def test_permanent_ai_error_is_not_retried(make):
    h = await make(FakeExtractor(AIRequestError("http_401", retryable=False)))
    (record,) = await h.wait_terminal(h.submit())

    assert (record.status, record.error) == (Status.FAILED, "ai_http_401")
    assert len(h.extractor.calls) == 1


async def test_request_without_recipient_fails_without_calling_the_ai(make):
    h = await make()
    (record,) = await h.wait_terminal(h.submit("Avisa a Pedro de que mañana no hay clase"))

    assert (record.status, record.error) == (Status.FAILED, "no_recipient")
    assert h.extractor.calls == []
    assert record.ai_attempts == 0


async def test_more_attempts_can_be_configured(make):
    h = await make(FakeExtractor(REFUSAL, REFUSAL, GOOD_ANSWER), max_ai_attempts=3)
    (record,) = await h.wait_terminal(h.submit())
    assert record.status is Status.SENT and record.ai_attempts == 3


# --- notification stage ---------------------------------------------------------------------------


async def test_429_pauses_the_limiter_and_is_retried(make):
    h = await make(sender=FakeSender(
        NotifyError("http_429", retryable=True, retry_after=3.0),
        NotifyError("http_429", retryable=True),
        "p-9",
    ))
    (record,) = await h.wait_terminal(h.submit())

    assert (record.status, record.provider_id, record.notify_attempts) == (Status.SENT, "p-9", 3)
    assert h.limiter.cooldowns == [3.0, 2.0]  # Retry-After, then the configured default
    assert h.limiter.acquired == 3
    # Re-queue backoff on top of the cooldown: base 1 s, then 2 s, each jittered to [0.5, 1) x base.
    assert len(h.sleeps) == 2
    assert 0.5 <= h.sleeps[0] < 1.0 and 1.0 <= h.sleeps[1] < 2.0


async def test_permanent_notify_error_fails_without_retry(make):
    h = await make(sender=FakeSender(NotifyError("http_401", retryable=False)))
    (record,) = await h.wait_terminal(h.submit())

    assert (record.status, record.error, record.notify_attempts) == (Status.FAILED, "notify_http_401", 1)


async def test_notify_attempts_are_bounded(make):
    h = await make(sender=FakeSender(NotifyError("http_503", retryable=True)), max_notify_attempts=4)
    (record,) = await h.wait_terminal(h.submit())

    assert (record.status, record.error) == (Status.FAILED, "notify_http_503")
    assert len(h.sender.calls) == record.notify_attempts == 4
    assert h.limiter.cooldowns == []  # only a 429 pauses everyone


async def test_backoff_grows_and_is_capped(make):
    h = await make(
        sender=FakeSender(NotifyError("http_500", retryable=True)),
        max_notify_attempts=6, notify_retry_backoff_sec=1.0, max_backoff_sec=4.0,
    )
    await h.wait_terminal(h.submit())

    bases = [1, 2, 4, 4, 4]
    assert len(h.sleeps) == 5
    assert all(0.5 * base <= s < base for s, base in zip(h.sleeps, bases))


async def test_maybe_delivered_is_retried_with_the_same_trace_id(make):
    h = await make(sender=FakeSender(NotifyError("timeout:ReadTimeout", retryable=True, maybe_delivered=True), "p-2"))
    request_id = h.submit()
    (record,) = await h.wait_terminal(request_id)

    assert record.status is Status.SENT
    assert [trace for _, trace in h.sender.calls] == [request_id, request_id]


# --- robustness ------------------------------------------------------------------------------------


async def test_unexpected_error_fails_one_request_and_the_worker_survives(make):
    h = await make(FakeExtractor(RuntimeError("bug"), GOOD_ANSWER), extraction_workers=1)
    broken, healthy = h.submit(), h.submit()

    first, second = await h.wait_terminal(broken, healthy)

    assert (first.status, first.error) == (Status.FAILED, "internal_error")
    assert second.status is Status.SENT


async def test_unexpected_notify_error_fails_the_request(make):
    h = await make(sender=FakeSender(KeyError("bug"), "p-1"), notify_workers=1)
    broken, healthy = h.submit(), h.submit()

    first, second = await h.wait_terminal(broken, healthy)

    assert (first.status, first.error) == (Status.FAILED, "internal_error")
    assert second.status is Status.SENT


async def test_duplicate_submission_calls_the_ai_once(make):
    h = await make()
    request_id = h.submit()
    h.pipeline.submit(request_id)
    h.pipeline.submit(request_id)

    await h.wait_terminal(request_id)
    await asyncio.sleep(0.01)

    assert len(h.extractor.calls) == 1
    assert len(h.sender.calls) == 1


async def test_snapshot_reports_workers_queues_and_ratio(make):
    h = await make(FakeExtractor(REFUSAL, GOOD_ANSWER))
    await h.wait_terminal(h.submit(), h.submit())

    snapshot = h.pipeline.snapshot()

    assert snapshot["healthy"]
    assert snapshot["workers"] == {
        "extraction": {"alive": 3, "expected": 3},
        "notify": {"alive": 2, "expected": 2},
    }
    assert snapshot["queues"] == {"extraction": 0, "notify": 0, "notify_retries_scheduled": 0}
    assert snapshot["ai_calls_per_request"] == 1.5  # 3 AI calls for 2 requests
    assert snapshot["counters"]["submitted"] == 2
    assert snapshot["counters"]["extraction_rejected:refusal"] == 1


async def test_snapshot_is_unhealthy_when_a_worker_dies(make):
    h = await make()
    h.pipeline._notify_workers[0].cancel()
    await asyncio.sleep(0)

    snapshot = h.pipeline.snapshot()

    assert not snapshot["healthy"]
    assert snapshot["workers"]["notify"]["alive"] == 1


async def test_snapshot_before_any_request(make):
    h = await make()
    assert h.pipeline.snapshot()["ai_calls_per_request"] is None


async def test_submit_reports_a_full_queue():
    pipeline = Pipeline(Settings(queue_maxsize=2), InMemoryRequestStore(), FakeExtractor(), FakeSender(), FakeLimiter())
    assert pipeline.submit("a") and pipeline.submit("b")
    assert not pipeline.submit("c")


async def test_stop_cancels_workers_and_pending_retries():
    h = Harness(sender=FakeSender(NotifyError("http_503", retryable=True)))

    async def never_wake(seconds):
        await asyncio.Event().wait()

    h.pipeline._sleep = never_wake
    await h.pipeline.start()
    request_id = h.submit()
    while h.store.get(request_id).notify_attempts == 0:
        await asyncio.sleep(0.001)

    await asyncio.wait_for(h.pipeline.stop(), timeout=1.0)

    assert h.store.get(request_id).stage is Stage.NOTIFY_PENDING  # left for a durable queue to resume
