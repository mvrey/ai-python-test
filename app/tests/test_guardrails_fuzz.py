"""Guardrails against the real mock generator (provider/responses.py).

``generate_ai_response`` is loaded from the provider's source, and the provider's own
target/message derivation (provider/app.py, ``ai_extract``) is mirrored below, so these tests
break if the mock changes in a way the guardrails don't handle.
"""

import importlib.util
import random
import re
from collections import Counter
from pathlib import Path

import pytest

from service.guardrails import Accepted, FailureReason, Rejected, extract_payload

RESPONSES_PATH = Path(__file__).resolve().parents[2] / "provider" / "responses.py"

K6_PROMPTS = {  # prompt -> (recipient we must send to, type)
    "Enviar email a juan@example.com diciendo que su paquete está en camino.": ("juan@example.com", "email"),
    "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.": ("600111222", "sms"),
    "Manda un correo a marta@app.com: Tienes un nuevo mensaje.": ("marta@app.com", "email"),
    "Recordatorio por teléfono al 699888777: cita a las 10:00": ("699888777", "sms"),
}

# (dice, sub_dice) values that reach each branch of generate_ai_response.
DICE = [0.25, 0.55, 0.65, 0.75, 0.85, 0.95]
SUB_DICE = [0.1, 0.5, 0.9]
REFUSAL_DICE = 0.95


@pytest.fixture(scope="module")
def mock_responses():
    spec = importlib.util.spec_from_file_location("mock_responses", RESPONSES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mock_extraction_args(prompt: str) -> tuple[str, str, str]:
    """Mirror of provider/app.py: what the mock passes to generate_ai_response."""
    lowered = prompt.lower()
    email = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", lowered)
    phone = re.search(r"\b\d{3}-?\d{3}-?\d{3,4}\b", lowered)
    target = email.group(0) if email else phone.group(0)
    if "email" in lowered or (email and "sms" not in lowered):
        notif_type = "email"
    else:
        notif_type = "sms"
    msg = lowered.split(":")[-1].strip() if ":" in lowered else lowered[:50]
    return target, msg, notif_type


class ScriptedRandom:
    def __init__(self, values):
        self._values = iter(values)

    def random(self):
        return next(self._values)


@pytest.mark.parametrize("prompt", K6_PROMPTS)
@pytest.mark.parametrize("dice", DICE)
@pytest.mark.parametrize("sub_dice", SUB_DICE)
def test_every_branch_for_every_k6_prompt(mock_responses, monkeypatch, prompt, dice, sub_dice):
    monkeypatch.setattr(mock_responses, "random", ScriptedRandom([dice, sub_dice]))
    target, msg, notif_type = mock_extraction_args(prompt)
    raw = mock_responses.generate_ai_response(target, msg, notif_type)

    result = extract_payload(raw, prompt)

    if dice == REFUSAL_DICE:
        assert isinstance(result, Rejected) and result.reason is FailureReason.REFUSAL and result.retryable
    else:
        assert isinstance(result, Accepted), (raw, result)
        expected_to, expected_type = K6_PROMPTS[prompt]
        assert (result.payload.to, result.payload.type, result.payload.message) == (
            expected_to, expected_type, msg.strip(),
        )


def test_random_sampling_matches_the_expected_distribution(mock_responses, monkeypatch):
    monkeypatch.setattr(mock_responses, "random", random.Random(2026))
    outcomes: Counter[str] = Counter()
    repairs: Counter[str] = Counter()
    iterations = 4000

    prompts = list(K6_PROMPTS)
    for i in range(iterations):
        prompt = prompts[i % len(prompts)]
        result = extract_payload(mock_responses.generate_ai_response(*mock_extraction_args(prompt)), prompt)
        if isinstance(result, Accepted):
            outcomes["accepted"] += 1
            assert result.payload.to == K6_PROMPTS[prompt][0]
        else:
            outcomes[result.reason.value] += 1
        repairs.update(result.repairs)

    # Only the 10% refusal branch may fail; everything else must be recovered.
    assert set(outcomes) <= {"accepted", "refusal"}
    assert 0.07 < outcomes["refusal"] / iterations < 0.13
    print("\noutcomes:", dict(outcomes), "\nrepairs:", dict(repairs.most_common()))
