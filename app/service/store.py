"""Request persistence.

``RequestStore`` is the interface the rest of the service depends on, so the in-memory
implementation can be swapped for Redis/Postgres without touching the pipeline. Lifecycle
rules are defined by ``Stage``; stores only enforce them.

Concurrency: the service runs in a single process on a single asyncio event loop, and no
method here awaits, so each call is atomic with respect to other coroutines. That makes
check-then-act sequences (e.g. ``get`` followed by ``transition``) safe as long as the
caller doesn't ``await`` in between. A shared, out-of-process store would need
compare-and-set semantics instead.
"""

import uuid
from dataclasses import replace
from typing import Protocol

from service.models import NotificationPayload, RequestRecord, Stage


class RequestNotFoundError(KeyError):
    pass


class InvalidTransitionError(Exception):
    def __init__(self, request_id: str, current: Stage, target: Stage):
        super().__init__(f"Request {request_id}: invalid transition {current.value} -> {target.value}")
        self.request_id = request_id
        self.current = current
        self.target = target


class RequestStore(Protocol):
    def create(self, user_input: str) -> RequestRecord: ...

    def get(self, request_id: str) -> RequestRecord | None: ...

    def transition(
        self,
        request_id: str,
        target: Stage,
        *,
        extraction: NotificationPayload | None = None,
        provider_id: str | None = None,
        error: str | None = None,
    ) -> RequestRecord:
        """Move a request to ``target``; the optional fields are set in the same atomic step.

        Raises ``RequestNotFoundError`` or ``InvalidTransitionError``.
        """
        ...

    def record_ai_attempt(self, request_id: str) -> RequestRecord: ...

    def record_notify_attempt(self, request_id: str) -> RequestRecord: ...

    def count_by_stage(self) -> dict[Stage, int]: ...


class InMemoryRequestStore:
    """Dict-backed store. Returns copies so callers can't mutate state behind the store's back."""

    def __init__(self) -> None:
        self._records: dict[str, RequestRecord] = {}

    def create(self, user_input: str) -> RequestRecord:
        record = RequestRecord(id=uuid.uuid4().hex, user_input=user_input)
        self._records[record.id] = record
        return replace(record)

    def get(self, request_id: str) -> RequestRecord | None:
        record = self._records.get(request_id)
        return replace(record) if record else None

    def transition(
        self,
        request_id: str,
        target: Stage,
        *,
        extraction: NotificationPayload | None = None,
        provider_id: str | None = None,
        error: str | None = None,
    ) -> RequestRecord:
        record = self._require(request_id)
        if not record.stage.can_transition_to(target):
            raise InvalidTransitionError(request_id, record.stage, target)
        record.stage = target
        if extraction is not None:
            record.extraction = extraction
        if provider_id is not None:
            record.provider_id = provider_id
        if error is not None:
            record.error = error
        return replace(record)

    def record_ai_attempt(self, request_id: str) -> RequestRecord:
        record = self._require(request_id)
        record.ai_attempts += 1
        return replace(record)

    def record_notify_attempt(self, request_id: str) -> RequestRecord:
        record = self._require(request_id)
        record.notify_attempts += 1
        return replace(record)

    def count_by_stage(self) -> dict[Stage, int]:
        counts = dict.fromkeys(Stage, 0)
        for record in self._records.values():
            counts[record.stage] += 1
        return counts

    def _require(self, request_id: str) -> RequestRecord:
        try:
            return self._records[request_id]
        except KeyError:
            raise RequestNotFoundError(request_id) from None
