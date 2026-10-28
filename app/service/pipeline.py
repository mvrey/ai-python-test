"""Asynchronous processing pipeline.

    /process -> extraction queue -> extraction workers (AI + guardrails)
             -> notify queue     -> notify workers (rate limiter + provider) -> sent | failed

Two stages with separate worker pools: AI calls are slow (1.5-3 s) but unlimited, so many
extraction workers run in parallel; the provider accepts ~5 notifications/s, so a few notify
workers share one rate limiter. Neither stage blocks the other, and the HTTP layer only enqueues.

Retry policy:
- AI: at most ``max_ai_attempts`` calls per request in total, whatever the failure (the provider
  counts every call). Requests without any recipient fail before the first call.
- Notify: retryable errors are re-queued with exponential backoff up to ``max_notify_attempts``;
  a 429 also pauses the shared limiter.

Every worker iteration is wrapped so an unexpected error fails that one request and the worker
keeps running.
"""

import asyncio
import logging
import random
from collections import Counter
from typing import Protocol

from service.ai_client import AIRequestError
from service.config import Settings
from service.guardrails import Accepted, FailureReason, extract_payload, precheck
from service.models import NotificationPayload, Stage
from service.notifier import NotifyError
from service.prompts import ChatMessage, build_messages
from service.store import InvalidTransitionError, RequestNotFoundError, RequestStore

logger = logging.getLogger(__name__)


class Extractor(Protocol):
    async def extract(self, messages: list[ChatMessage]) -> str: ...


class NotificationSender(Protocol):
    async def send(self, payload: NotificationPayload, trace_id: str) -> str | None: ...


class RateLimiter(Protocol):
    async def acquire(self) -> None: ...

    def cooldown(self, seconds: float) -> None: ...


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: RequestStore,
        extractor: Extractor,
        sender: NotificationSender,
        limiter: RateLimiter,
        *,
        sleep=asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._extractor = extractor
        self._sender = sender
        self._limiter = limiter
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._extraction_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_maxsize)
        # Unbounded on purpose: notifications drain at ~5/s while extraction finishes ~10x faster,
        # so this backlog must be able to grow. Each request is in it at most once.
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._delayed: set[asyncio.Task] = set()
        self.stats: Counter[str] = Counter()

    # --- lifecycle -----------------------------------------------------------------

    def submit(self, request_id: str) -> bool:
        """Enqueue a request for extraction without blocking. Returns False if the queue is full."""
        try:
            self._extraction_queue.put_nowait(request_id)
        except asyncio.QueueFull:
            return False
        return True

    async def start(self) -> None:
        self._workers = [
            *(asyncio.create_task(self._run(self._extraction_queue, self._extract), name=f"extract-{i}")
              for i in range(self._settings.extraction_workers)),
            *(asyncio.create_task(self._run(self._notify_queue, self._notify), name=f"notify-{i}")
              for i in range(self._settings.notify_workers)),
        ]
        logger.info(
            "pipeline started: %d extraction workers, %d notify workers",
            self._settings.extraction_workers, self._settings.notify_workers,
        )

    async def stop(self) -> None:
        tasks = [*self._workers, *self._delayed]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._delayed.clear()

    def queue_sizes(self) -> dict[str, int]:
        return {"extraction": self._extraction_queue.qsize(), "notify": self._notify_queue.qsize()}

    async def _run(self, queue: asyncio.Queue[str], handle) -> None:
        while True:
            request_id = await queue.get()
            try:
                await handle(request_id)
            except Exception:
                logger.exception("unexpected error id=%s", request_id)
                self._fail(request_id, "internal_error")
            finally:
                queue.task_done()

    # --- extraction stage -------------------------------------------------------------

    async def _extract(self, request_id: str) -> None:
        record = self._store.get(request_id)
        if record is None or record.stage is not Stage.EXTRACTION_PENDING:
            logger.warning("skipping extraction id=%s stage=%s", request_id, record and record.stage.value)
            return
        self._store.transition(request_id, Stage.EXTRACTING)

        rejected = precheck(record.user_input)
        if rejected:
            self._fail(request_id, rejected.reason.value)
            return

        retry_reason: FailureReason | None = None
        last_error = "unknown"
        for attempt in range(1, self._settings.max_ai_attempts + 1):
            self._store.record_ai_attempt(request_id)
            self.stats["ai_calls"] += 1
            try:
                raw = await self._extractor.extract(build_messages(record.user_input, retry_reason))
            except AIRequestError as exc:
                self.stats[f"ai_error:{exc.reason}"] += 1
                last_error = f"ai_{exc.reason}"
                logger.warning("ai call failed id=%s attempt=%d reason=%s", request_id, attempt, exc.reason)
                if not exc.retryable:
                    break
                retry_reason = None  # the model never answered: nothing to correct
                if attempt < self._settings.max_ai_attempts:
                    await self._sleep(self._backoff(self._settings.ai_retry_backoff_sec, attempt))
                continue

            result = extract_payload(raw, record.user_input)
            self.stats.update(f"repair:{repair}" for repair in result.repairs)
            if isinstance(result, Accepted):
                self.stats["extraction_accepted"] += 1
                logger.info(
                    "extracted id=%s attempt=%d type=%s repairs=%s",
                    request_id, attempt, result.payload.type, list(result.repairs),
                )
                self._store.transition(request_id, Stage.NOTIFY_PENDING, extraction=result.payload)
                self._notify_queue.put_nowait(request_id)
                return

            self.stats[f"extraction_rejected:{result.reason.value}"] += 1
            last_error = result.reason.value
            logger.warning(
                "extraction rejected id=%s attempt=%d reason=%s repairs=%s raw=%.200r",
                request_id, attempt, result.reason.value, list(result.repairs), raw,
            )
            if not result.retryable:
                break
            retry_reason = result.reason

        self._fail(request_id, last_error)

    # --- notification stage --------------------------------------------------------------

    async def _notify(self, request_id: str) -> None:
        record = self._store.get(request_id)
        if record is None or record.stage is not Stage.NOTIFY_PENDING or record.extraction is None:
            logger.warning("skipping notification id=%s stage=%s", request_id, record and record.stage.value)
            return

        await self._limiter.acquire()
        self._store.transition(request_id, Stage.NOTIFYING)
        attempt = self._store.record_notify_attempt(request_id).notify_attempts
        self.stats["notify_calls"] += 1
        try:
            provider_id = await self._sender.send(record.extraction, trace_id=request_id)
        except NotifyError as exc:
            self._handle_notify_error(request_id, attempt, exc)
            return

        self.stats["sent"] += 1
        self._store.transition(request_id, Stage.SENT, provider_id=provider_id)
        logger.info("sent id=%s attempt=%d provider_id=%s", request_id, attempt, provider_id)

    def _handle_notify_error(self, request_id: str, attempt: int, exc: NotifyError) -> None:
        self.stats[f"notify_error:{exc.reason}"] += 1
        if exc.reason == "http_429":
            self._limiter.cooldown(exc.retry_after or self._settings.notify_cooldown_sec)

        if not exc.retryable or attempt >= self._settings.max_notify_attempts:
            logger.error("notification failed id=%s attempt=%d reason=%s", request_id, attempt, exc.reason)
            self._fail(request_id, f"notify_{exc.reason}")
            return

        delay = self._backoff(self._settings.notify_retry_backoff_sec, attempt)
        logger.warning(
            "notification retry id=%s attempt=%d reason=%s delay=%.2fs%s",
            request_id, attempt, exc.reason, delay,
            " (may have been delivered: at-least-once)" if exc.maybe_delivered else "",
        )
        self._store.transition(request_id, Stage.NOTIFY_PENDING, error=f"notify_{exc.reason}")
        self._requeue_later(request_id, delay)

    def _requeue_later(self, request_id: str, delay: float) -> None:
        """Re-queue after ``delay`` without holding a worker while waiting."""

        async def requeue() -> None:
            await self._sleep(delay)
            self._notify_queue.put_nowait(request_id)

        task = asyncio.create_task(requeue())
        self._delayed.add(task)
        task.add_done_callback(self._delayed.discard)

    # --- helpers ---------------------------------------------------------------------------

    def _backoff(self, base: float, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^(attempt-1), capped, scaled by [0.5, 1)."""
        delay = min(self._settings.max_backoff_sec, base * 2 ** (attempt - 1))
        return delay * self._rng.uniform(0.5, 1.0)

    def _fail(self, request_id: str, reason: str) -> None:
        try:
            self._store.transition(request_id, Stage.FAILED, error=reason)
        except (InvalidTransitionError, RequestNotFoundError):
            logger.warning("could not mark id=%s as failed (reason=%s)", request_id, reason)
            return
        self.stats[f"failed:{reason}"] += 1
        logger.info("failed id=%s reason=%s", request_id, reason)
