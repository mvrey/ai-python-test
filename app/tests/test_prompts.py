import json

import pytest

from service.guardrails import Accepted, FailureReason, Rejected, extract_payload
from service.prompts import FEW_SHOT, SYSTEM_PROMPT, build_messages

USER_INPUT = "Manda un correo a marta@app.com: Tienes un nuevo mensaje."


def test_layout_system_then_examples_then_user_input_last():
    messages = build_messages(USER_INPUT)

    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1:-1] == FEW_SHOT
    assert messages[-1] == {"role": "user", "content": USER_INPUT}


@pytest.mark.parametrize("reason", [None, *FailureReason])
def test_user_input_is_always_last_and_verbatim(reason):
    # The mock provider reads only the last user message, and derives the body from its raw text.
    messages = build_messages(USER_INPUT, retry_reason=reason)
    assert messages[-1] == {"role": "user", "content": USER_INPUT}
    assert [m for m in messages if m["role"] == "user"][-1]["content"] == USER_INPUT


@pytest.mark.parametrize("reason", list(FailureReason))
def test_retry_adds_one_system_hint_before_the_request(reason):
    first, retry = build_messages(USER_INPUT), build_messages(USER_INPUT, retry_reason=reason)

    assert len(retry) == len(first) + 1
    hint = retry[-2]
    assert hint["role"] == "system"
    assert "JSON object" in hint["content"]


def test_refusal_hint_addresses_the_refusal():
    assert "refused" in build_messages(USER_INPUT, FailureReason.REFUSAL)[-2]["content"]


def test_few_shot_alternates_user_and_assistant():
    assert [m["role"] for m in FEW_SHOT] == ["user", "assistant"] * (len(FEW_SHOT) // 2)


@pytest.mark.parametrize("index", range(0, len(FEW_SHOT), 2))
def test_few_shot_answers_follow_the_output_contract(index):
    answer = json.loads(FEW_SHOT[index + 1]["content"])
    assert list(answer) == ["to", "message", "type"]
    assert answer["type"] in ("email", "sms", None)


@pytest.mark.parametrize("index", range(0, len(FEW_SHOT), 2))
def test_few_shot_answers_are_consistent_with_our_own_guardrails(index):
    """The examples teach exactly what the guardrails accept, with no repairs needed."""
    request, answer = FEW_SHOT[index]["content"], FEW_SHOT[index + 1]["content"]

    result = extract_payload(answer, request)

    if json.loads(answer)["to"] is None:
        assert isinstance(result, Rejected) and result.reason is FailureReason.NO_RECIPIENT
    else:
        assert isinstance(result, Accepted)
        assert result.repairs == ()


def test_system_prompt_states_the_contract():
    for required in ('"to"', '"message"', '"type"', '"email"', '"sms"', "null", "JSON object only"):
        assert required in SYSTEM_PROMPT
