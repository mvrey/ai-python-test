"""API schemas and domain models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_USER_INPUT_CHARS = 2000

NotificationType = Literal["email", "sms"]


class Status(str, Enum):
    """Public request status, as defined by the API contract."""

    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class Stage(str, Enum):
    """Internal pipeline stage. Finer-grained than ``Status``; never exposed in the API contract.

    The lifecycle rules (public status of each stage, allowed transitions) live here so that
    every store implementation enforces the same state machine.
    """

    CREATED = "created"
    EXTRACTION_PENDING = "extraction_pending"
    EXTRACTING = "extracting"
    NOTIFY_PENDING = "notify_pending"
    NOTIFYING = "notifying"
    SENT = "sent"
    FAILED = "failed"

    @property
    def status(self) -> Status:
        return _STAGE_TO_STATUS[self]

    @property
    def is_terminal(self) -> bool:
        return not _ALLOWED_TRANSITIONS[self]

    def can_transition_to(self, target: "Stage") -> bool:
        return target in _ALLOWED_TRANSITIONS[self]


_STAGE_TO_STATUS = {
    Stage.CREATED: Status.QUEUED,
    Stage.EXTRACTION_PENDING: Status.QUEUED,
    Stage.EXTRACTING: Status.PROCESSING,
    Stage.NOTIFY_PENDING: Status.PROCESSING,
    Stage.NOTIFYING: Status.PROCESSING,
    Stage.SENT: Status.SENT,
    Stage.FAILED: Status.FAILED,
}

_ALLOWED_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.CREATED: frozenset({Stage.EXTRACTION_PENDING, Stage.FAILED}),
    Stage.EXTRACTION_PENDING: frozenset({Stage.EXTRACTING, Stage.FAILED}),
    Stage.EXTRACTING: frozenset({Stage.NOTIFY_PENDING, Stage.FAILED}),
    Stage.NOTIFY_PENDING: frozenset({Stage.NOTIFYING, Stage.FAILED}),
    # NOTIFYING -> NOTIFY_PENDING: a retryable provider error re-queues the notification.
    Stage.NOTIFYING: frozenset({Stage.SENT, Stage.NOTIFY_PENDING, Stage.FAILED}),
    Stage.SENT: frozenset(),
    Stage.FAILED: frozenset(),
}


# --- API schemas -------------------------------------------------------------


class CreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    user_input: str = Field(..., min_length=1, max_length=MAX_USER_INPUT_CHARS)


class CreateResponse(BaseModel):
    id: str


class StatusResponse(BaseModel):
    id: str
    status: Status


# --- Domain ------------------------------------------------------------------


class NotificationPayload(BaseModel):
    """Validated extraction result, in the exact shape the provider's /v1/notify expects."""

    model_config = ConfigDict(frozen=True)

    to: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    type: NotificationType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RequestRecord:
    id: str
    user_input: str
    stage: Stage = Stage.CREATED
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    extraction: NotificationPayload | None = None
    ai_attempts: int = 0
    notify_attempts: int = 0
    provider_id: str | None = None
    error: str | None = None  # internal failure reason, for logs and diagnostics

    @property
    def status(self) -> Status:
        return self.stage.status
