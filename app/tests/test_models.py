from itertools import pairwise

import pytest
from pydantic import ValidationError

from service.models import (
    MAX_USER_INPUT_CHARS,
    CreateRequest,
    NotificationPayload,
    RequestRecord,
    Stage,
    Status,
)

HAPPY_PATH = [
    Stage.CREATED,
    Stage.EXTRACTION_PENDING,
    Stage.EXTRACTING,
    Stage.NOTIFY_PENDING,
    Stage.NOTIFYING,
    Stage.SENT,
]


@pytest.mark.parametrize("stage", list(Stage))
def test_every_stage_maps_to_a_public_status(stage):
    assert isinstance(stage.status, Status)


def test_only_sent_and_failed_are_terminal():
    assert {s for s in Stage if s.is_terminal} == {Stage.SENT, Stage.FAILED}


@pytest.mark.parametrize("current, target", pairwise(HAPPY_PATH))
def test_happy_path_transitions_are_allowed(current, target):
    assert current.can_transition_to(target)


def test_notify_retry_can_go_back_to_pending():
    assert Stage.NOTIFYING.can_transition_to(Stage.NOTIFY_PENDING)


@pytest.mark.parametrize("stage", [s for s in Stage if not s.is_terminal])
def test_any_non_terminal_stage_can_fail(stage):
    assert stage.can_transition_to(Stage.FAILED)


@pytest.mark.parametrize("terminal", [Stage.SENT, Stage.FAILED])
@pytest.mark.parametrize("target", list(Stage))
def test_terminal_stages_are_final(terminal, target):
    assert not terminal.can_transition_to(target)


def test_stages_cannot_be_skipped():
    assert not Stage.CREATED.can_transition_to(Stage.SENT)
    assert not Stage.EXTRACTING.can_transition_to(Stage.NOTIFYING)


def test_record_status_is_derived_from_stage():
    record = RequestRecord(id="abc", user_input="hola")
    assert record.status is Status.QUEUED
    record.stage = Stage.NOTIFYING
    assert record.status is Status.PROCESSING


def test_notification_payload_is_immutable():
    payload = NotificationPayload(to="feda@test.com", message="hola", type="email")
    with pytest.raises(ValidationError):
        payload.to = "other@test.com"


def test_create_request_strips_whitespace():
    assert CreateRequest(user_input="  hola  ").user_input == "hola"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_input": ""},
        {"user_input": "   "},
        {"user_input": 123},
        {"user_input": None},
        {"user_input": "x" * (MAX_USER_INPUT_CHARS + 1)},
    ],
)
def test_create_request_rejects_invalid_input(payload):
    with pytest.raises(ValidationError):
        CreateRequest(**payload)
