"""Store behaviour through its public interface. The lifecycle rules themselves are tested in test_models."""

import pytest

from service.models import NotificationPayload, Stage, Status
from service.store import InMemoryRequestStore, InvalidTransitionError, RequestNotFoundError

PAYLOAD = NotificationPayload(to="feda@test.com", message="hola", type="email")
TO_NOTIFYING = [Stage.EXTRACTION_PENDING, Stage.EXTRACTING, Stage.NOTIFY_PENDING, Stage.NOTIFYING]


@pytest.fixture
def store():
    return InMemoryRequestStore()


def advance(store, request_id, stages):
    for stage in stages:
        store.transition(request_id, stage)


def test_create_and_get(store):
    created = store.create("Manda un mail a feda@test.com diciendo hola")

    fetched = store.get(created.id)

    assert fetched == created
    assert fetched.stage is Stage.CREATED
    assert fetched.status is Status.QUEUED


def test_ids_are_unique(store):
    ids = {store.create("hola").id for _ in range(100)}
    assert len(ids) == 100


def test_get_unknown_returns_none(store):
    assert store.get("missing") is None


def test_returned_records_are_copies(store):
    record = store.create("hola")
    record.stage = Stage.SENT
    record.error = "tampered"

    fetched = store.get(record.id)

    assert fetched.stage is Stage.CREATED
    assert fetched.error is None


def test_full_happy_path(store):
    record = store.create("hola")

    store.transition(record.id, Stage.EXTRACTION_PENDING)
    store.transition(record.id, Stage.EXTRACTING)
    store.record_ai_attempt(record.id)
    store.transition(record.id, Stage.NOTIFY_PENDING, extraction=PAYLOAD)
    store.transition(record.id, Stage.NOTIFYING)
    store.record_notify_attempt(record.id)
    final = store.transition(record.id, Stage.SENT, provider_id="p-1234")

    assert final.status is Status.SENT
    assert final.extraction == PAYLOAD
    assert (final.ai_attempts, final.notify_attempts, final.provider_id) == (1, 1, "p-1234")


def test_transition_keeps_fields_that_are_not_passed(store):
    record = store.create("hola")
    advance(store, record.id, TO_NOTIFYING[:2])
    store.transition(record.id, Stage.NOTIFY_PENDING, extraction=PAYLOAD)

    after = store.transition(record.id, Stage.NOTIFYING)

    assert after.extraction == PAYLOAD


def test_failure_records_the_reason(store):
    record = store.create("hola")
    failed = store.transition(record.id, Stage.FAILED, error="no_recipient")
    assert (failed.status, failed.error) == (Status.FAILED, "no_recipient")


@pytest.mark.parametrize(
    "path_to_terminal",
    [TO_NOTIFYING + [Stage.SENT], [Stage.FAILED]],
    ids=["sent", "failed"],
)
def test_terminal_requests_cannot_move(store, path_to_terminal):
    record = store.create("hola")
    advance(store, record.id, path_to_terminal)

    with pytest.raises(InvalidTransitionError):
        store.transition(record.id, Stage.EXTRACTION_PENDING)
    assert store.get(record.id).stage is path_to_terminal[-1]


def test_rejected_transition_leaves_record_untouched(store):
    record = store.create("hola")

    with pytest.raises(InvalidTransitionError) as exc_info:
        store.transition(record.id, Stage.SENT, provider_id="p-1")

    assert exc_info.value.current is Stage.CREATED
    unchanged = store.get(record.id)
    assert (unchanged.stage, unchanged.provider_id) == (Stage.CREATED, None)


def test_attempt_counters_increment_and_touch_timestamp(store):
    record = store.create("hola")

    store.record_ai_attempt(record.id)
    store.record_ai_attempt(record.id)
    updated = store.record_notify_attempt(record.id)

    assert (updated.ai_attempts, updated.notify_attempts) == (2, 1)
    assert updated.updated_at >= record.updated_at
    assert updated.stage is Stage.CREATED


def test_count_by_stage(store):
    assert set(store.count_by_stage().values()) == {0}
    first, second = store.create("a"), store.create("b")
    store.create("c")
    store.transition(first.id, Stage.FAILED)
    store.transition(second.id, Stage.EXTRACTION_PENDING)

    counts = store.count_by_stage()

    assert counts[Stage.CREATED] == 1
    assert counts[Stage.FAILED] == 1
    assert counts[Stage.EXTRACTION_PENDING] == 1
    assert sum(counts.values()) == 3


def test_unknown_id_raises(store):
    with pytest.raises(RequestNotFoundError):
        store.record_ai_attempt("missing")
    with pytest.raises(RequestNotFoundError):
        store.transition("missing", Stage.FAILED)
