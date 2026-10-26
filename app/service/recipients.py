"""Recipient detection and normalisation (email addresses and phone numbers).

Used for two things: finding the recipients actually present in the user's request, and
checking that the recipient the AI claims is one of them (grounding).
"""

import re
from dataclasses import dataclass

from service.models import NotificationType

MIN_PHONE_DIGITS = 9  # national number length in Spain; also the minimum we accept anywhere
MAX_PHONE_DIGITS = 15  # E.164 maximum

_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
_EMAIL_FULL = re.compile(_EMAIL)
_EMAIL_IN_TEXT = re.compile(rf"(?<![\w.%+-]){_EMAIL}(?![\w-])")

# Either a contiguous run of digits, or digit groups separated by single spaces, dots or
# dashes ("600-111-222", "+34 600 111 222", "(91) 123 45 67"). Contiguous runs are tried
# first so "699888777 10 veces" doesn't swallow the trailing "10".
_PHONE_IN_TEXT = re.compile(
    r"(?<![\w+@.])"
    r"(?:\+?\d{%d,%d}(?!\d)|\+?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,5})"
    r"(?!\w)" % (MIN_PHONE_DIGITS, MAX_PHONE_DIGITS)
)
_PHONE_FULL = re.compile(r"\+?\d{%d,%d}" % (MIN_PHONE_DIGITS, MAX_PHONE_DIGITS))
_PHONE_SEPARATORS = re.compile(r"[\s().-]")
_URI_SCHEME = re.compile(r"^(?:mailto|tel|sms):", re.IGNORECASE)


@dataclass(frozen=True)
class Recipient:
    """A normalised recipient: an email address, or a phone number as ``+?digits``."""

    value: str
    type: NotificationType

    def matches(self, other: "Recipient") -> bool:
        if self.type != other.type:
            return False
        if self.type == "email":
            return self.value.casefold() == other.value.casefold()
        # Tolerate a country code present on one side only: "+34600111222" ~ "600111222".
        shorter, longer = sorted((self.value.lstrip("+"), other.value.lstrip("+")), key=len)
        return longer.endswith(shorter)


def parse_recipient(raw: str) -> Recipient | None:
    """Parse a single recipient value (e.g. the AI's ``to`` field). Returns None if invalid."""
    value = _URI_SCHEME.sub("", raw.strip().strip("<>").strip()).strip().rstrip(".,;:")
    if _EMAIL_FULL.fullmatch(value):
        return Recipient(value, "email")
    compact = _PHONE_SEPARATORS.sub("", value)
    if _PHONE_FULL.fullmatch(compact):
        return Recipient(compact, "sms")
    return None


def find_recipients(text: str) -> list[Recipient]:
    """All distinct recipients mentioned in free text, in order of appearance."""
    found: list[tuple[int, Recipient]] = []
    for match in _EMAIL_IN_TEXT.finditer(text):
        found.append((match.start(), Recipient(match.group(), "email")))

    # Blank out emails so digits inside them are never read as phone numbers.
    without_emails = _EMAIL_IN_TEXT.sub(lambda m: " " * len(m.group()), text)
    for match in _PHONE_IN_TEXT.finditer(without_emails):
        recipient = parse_recipient(match.group())
        if recipient is not None:
            found.append((match.start(), recipient))

    distinct: list[Recipient] = []
    for _, recipient in sorted(found, key=lambda item: item[0]):
        if not any(recipient.matches(existing) for existing in distinct):
            distinct.append(recipient)
    return distinct
