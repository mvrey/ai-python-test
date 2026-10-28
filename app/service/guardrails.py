"""Turn a raw LLM extraction answer into a validated ``NotificationPayload``, or a reason why not.

Pure and synchronous: no I/O, so every rule is unit-testable. Steps:

1. Find the recipients present in the user's request. None at all -> fatal (no retry helps).
2. Find a JSON object in the answer that has at least one recognised field (lenient_json).
   None -> retryable (refusal or no JSON).
3. Normalise keys: case-insensitive, with aliases (``Recipient``, ``body``, ``channel``...).
4. Recipient, grounded in the request: the AI's ``to`` must match a recipient found in the
   user input, and the input's version is what gets sent. That blocks hallucinated or
   injected recipients. Missing, invalid or ungrounded -> fall back to the request's only
   recipient; with several candidates the choice is ambiguous -> retryable.
5. Channel follows the recipient: an email address can't receive an SMS, so the AI's
   ``type`` is only checked against it (missing -> inferred, conflicting -> corrected).
6. Message: required, non-empty -> otherwise retryable.

Every result carries ``repairs``: labels of what had to be fixed, for logs and metrics.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from service.lenient_json import iter_json_objects
from service.models import MAX_USER_INPUT_CHARS, NotificationPayload, NotificationType
from service.recipients import Recipient, find_recipients, parse_recipient

MAX_MESSAGE_CHARS = MAX_USER_INPUT_CHARS  # a message can't be longer than the request it came from
_MAX_NESTING_SEARCH = 2


class FailureReason(StrEnum):
    NO_RECIPIENT = "no_recipient"
    EMPTY_RESPONSE = "empty_response"
    REFUSAL = "refusal"
    NO_JSON = "no_json"
    AMBIGUOUS_RECIPIENT = "ambiguous_recipient"
    MISSING_MESSAGE = "missing_message"
    INVALID_MESSAGE = "invalid_message"

    @property
    def retryable(self) -> bool:
        """Whether asking the AI again could produce a different, valid answer."""
        return self is not FailureReason.NO_RECIPIENT


@dataclass(frozen=True)
class Accepted:
    payload: NotificationPayload
    repairs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejected:
    reason: FailureReason
    repairs: tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        return self.reason.retryable


GuardrailResult = Accepted | Rejected


_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "to": (
        "recipient", "destination", "destinatario", "destino", "receiver", "target", "address",
        "email", "email_address", "mail", "phone", "phone_number", "number", "telefono", "teléfono",
    ),
    "message": ("body", "text", "content", "msg", "mensaje", "texto", "contenido"),
    "type": ("channel", "method", "medium", "canal", "tipo", "via", "notification_type"),
}
_CANONICAL_KEYS = {alias: canonical for canonical, aliases in _KEY_ALIASES.items() for alias in aliases}
_CANONICAL_KEYS.update({canonical: canonical for canonical in _KEY_ALIASES})

_TYPE_SYNONYMS: dict[str, NotificationType] = {
    **dict.fromkeys(
        ["email", "e-mail", "mail", "correo", "correo electrónico", "correo electronico"], "email"
    ),
    **dict.fromkeys(
        ["sms", "text", "text message", "mensaje de texto", "phone", "teléfono", "telefono",
         "móvil", "movil", "mobile"],
        "sms",
    ),
}

_REFUSAL_MARKERS = (
    "lo siento", "no puedo", "no tengo permitido", "polític", "viola", "sorry", "cannot", "can't",
    "unable", "not allowed", "refuse", "policy", "policies",
)


def precheck(user_input: str) -> Rejected | None:
    """Reject requests that no AI answer could make valid, before spending an AI call on them."""
    if not find_recipients(user_input):
        return Rejected(FailureReason.NO_RECIPIENT)
    return None


def extract_payload(raw: Any, user_input: str) -> GuardrailResult:
    repairs: list[str] = []

    candidates = find_recipients(user_input)
    if not candidates:
        return Rejected(FailureReason.NO_RECIPIENT)

    if not isinstance(raw, str) or not raw.strip():
        return Rejected(FailureReason.EMPTY_RESPONSE)

    fields = _find_fields(raw, repairs)
    if fields is None:
        reason = FailureReason.REFUSAL if _looks_like_refusal(raw) else FailureReason.NO_JSON
        return Rejected(reason, tuple(repairs))

    recipient = _resolve_recipient(fields.get("to"), candidates, repairs)
    if recipient is None:
        return Rejected(FailureReason.AMBIGUOUS_RECIPIENT, tuple(repairs))

    notification_type = _resolve_type(fields.get("type"), recipient, repairs)

    message = _as_text(fields.get("message"))
    if message is None:
        return Rejected(FailureReason.MISSING_MESSAGE, tuple(repairs))
    if len(message) > MAX_MESSAGE_CHARS:
        return Rejected(FailureReason.INVALID_MESSAGE, tuple(repairs))

    payload = NotificationPayload(to=recipient.value, message=message, type=notification_type)
    return Accepted(payload, tuple(repairs))


# --- Step 2-3: locate fields ------------------------------------------------------------


def _find_fields(raw: str, repairs: list[str]) -> dict[str, Any] | None:
    """First object in the answer (or nested one level or two down) with a recognised key."""
    for extracted in iter_json_objects(raw):
        found = _search_nested(extracted.data, depth=0)
        if found is None:
            continue
        data, depth = found
        repairs.extend(extracted.fixes)
        if depth:
            repairs.append("nested_object")
        return _normalise_keys(data, repairs)
    return None


def _search_nested(data: Any, depth: int) -> tuple[dict, int] | None:
    if not isinstance(data, dict):
        return None
    if any(_canonical_key(key) for key in data):
        return data, depth
    if depth < _MAX_NESTING_SEARCH:
        for value in data.values():
            found = _search_nested(value, depth + 1)
            if found:
                return found
    return None


def _canonical_key(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    normalised = key.strip().casefold().replace("-", "_").replace(" ", "_")
    return _CANONICAL_KEYS.get(normalised)


def _normalise_keys(data: dict, repairs: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    aliased: list[str] = []
    dropped: list[str] = []
    # Exact canonical keys first, so {"to": ..., "recipient": ...} keeps "to".
    for key in sorted(data, key=lambda k: k not in _KEY_ALIASES):
        canonical = _canonical_key(key)
        if canonical is None or canonical in fields:
            dropped.append(str(key))
            continue
        fields[canonical] = data[key]
        if key != canonical:
            aliased.append(str(key))
    repairs.extend(f"key_alias:{key}" for key in aliased)
    if dropped:
        repairs.append("dropped_keys:" + ",".join(sorted(dropped)))
    return fields


def _looks_like_refusal(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


# --- Step 4-6: validate fields ----------------------------------------------------------


def _resolve_recipient(raw_to: Any, candidates: list[Recipient], repairs: list[str]) -> Recipient | None:
    text = _as_text(raw_to)
    claimed = parse_recipient(text) if text else None
    if claimed is not None:
        grounded = _first_match(claimed, candidates)
        if grounded is not None:
            return grounded
        repairs.append("recipient_ungrounded")
    else:
        repairs.append("recipient_invalid" if text else "recipient_missing")

    if len(candidates) == 1:
        repairs.append("recipient_from_input")
        return candidates[0]
    return None


def _first_match(claimed: Recipient, candidates: Iterable[Recipient]) -> Recipient | None:
    return next((c for c in candidates if c.matches(claimed)), None)


def _resolve_type(raw_type: Any, recipient: Recipient, repairs: list[str]) -> NotificationType:
    text = _as_text(raw_type)
    claimed = _TYPE_SYNONYMS.get(text.casefold()) if text else None
    if claimed is None:
        repairs.append("type_inferred" if text is None else "type_unrecognized")
    elif claimed != recipient.type:
        repairs.append("type_corrected")
    return recipient.type


def _as_text(value: Any) -> str | None:
    """Coerce a scalar field to stripped text; None for missing, empty or structured values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)  # e.g. a phone number emitted as a JSON number
    if isinstance(value, str):
        return value.strip() or None
    return None
