"""Find and parse JSON objects in free-form LLM output.

LLMs wrap JSON in prose or Markdown fences, and emit near-JSON: single quotes, unquoted
keys, unescaped inner quotes, missing commas, truncated output. This module knows nothing
about notifications; it yields every object it can recover, best candidates first:

1. strict parses (``json.JSONDecoder.raw_decode`` at each ``{``), which never guess;
2. lenient parses with a tolerant recursive-descent parser, which repairs the dialects
   above. A value that was cut off mid-token is dropped rather than returned partially,
   so callers never see a silently truncated string.

Each result lists the ``fixes`` that were needed, for logging and metrics.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

MAX_TEXT_CHARS = 20_000  # bounds the work done on a pathological response
MAX_START_POSITIONS = 10  # number of "{" positions tried
MAX_DEPTH = 20

_WHITESPACE = re.compile(r"\s*")
_BARE_KEY = re.compile(r"[^\s:,{}\[\]\"']+")
_BARE_VALUE = re.compile(r"[^,}\]\n]*")
_INT = re.compile(r"-?(?:0|[1-9]\d*)")
_FLOAT = re.compile(r"-?(?:0|[1-9]\d*)\.\d+(?:[eE][+-]?\d+)?")
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_KEY_START = re.compile(r"[\"'A-Za-z_]")
# A quoted key right after a value: '... "hola" "type": ...' (missing comma).
_QUOTED_KEY_AHEAD = re.compile(r"""\s*(["'])[^"'\n]{1,64}\1\s*:""")
_LITERALS = {"true": True, "false": False, "null": None, "none": None}
_ESCAPES = {'"': '"', "'": "'", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


@dataclass(frozen=True)
class ExtractedObject:
    data: dict
    fixes: tuple[str, ...]  # empty for a clean JSON document with nothing around it


def iter_json_objects(text: str) -> Iterator[ExtractedObject]:
    """Yield the JSON objects found in ``text``: strict parses first, then lenient ones."""
    text = text[:MAX_TEXT_CHARS]
    outer_fixes: tuple[str, ...] = ()

    # Double-encoded answer: the whole text is a JSON string whose content is the JSON.
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        decoded = None
    if isinstance(decoded, str):
        text, outer_fixes = decoded[:MAX_TEXT_CHARS], ("double_encoded",)

    starts = [m.start() for m in re.finditer(r"\{", text)][:MAX_START_POSITIONS]

    decoder = json.JSONDecoder()
    for start in starts:
        try:
            data, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        yield ExtractedObject(data, outer_fixes + _context_fixes(text, start, end))

    for start in starts:
        parser = _LenientParser(text, start)
        data = parser.parse()
        if data is not None:
            fixes = ("lenient_json", *sorted(parser.fixes))
            yield ExtractedObject(data, outer_fixes + _context_fixes(text, start, parser.position) + fixes)


def _context_fixes(text: str, start: int, end: int) -> tuple[str, ...]:
    if not (text[:start].strip() or text[end:].strip()):
        return ()
    return ("code_fence",) if "```" in text else ("surrounding_text",)


class _Incomplete(Exception):
    """The current value could not be completed (cut off, or followed by garbage)."""


class _LenientParser:
    """Tolerant recursive-descent parser for one object starting at ``text[start] == "{"``.

    The top-level object is returned with every key/value pair that was complete; a nested
    value that can't be completed raises ``_Incomplete`` and its key is dropped.
    """

    def __init__(self, text: str, start: int) -> None:
        self._s = text
        self._i = start
        self.fixes: set[str] = set()
        # Precomputed so the closing-quote check is O(1) instead of rescanning the rest of the text.
        self._last_alnum = max((i for i, c in enumerate(text) if c.isalnum()), default=-1)
        self._last_quote = {q: text.rfind(q) for q in "\"'"}

    @property
    def position(self) -> int:
        return self._i

    def parse(self) -> dict | None:
        try:
            return self._object(depth=0)
        except _Incomplete:
            return None

    # --- containers --------------------------------------------------------------

    def _object(self, depth: int) -> dict:
        if depth > MAX_DEPTH:
            raise _Incomplete
        self._i += 1  # "{"
        result: dict = {}
        while True:
            self._skip_ws()
            if self._eof():
                return self._partial(result, depth, "unterminated_object")
            ch = self._s[self._i]
            if ch == "}":
                self._i += 1
                return result
            if ch == ",":  # also tolerates leading, doubled and trailing commas
                self._i += 1
                continue

            try:
                key = self._key()
                self._skip_ws()
                if self._eof() or self._s[self._i] != ":":
                    raise _Incomplete
                self._i += 1
                self._skip_ws()
                if self._eof():
                    raise _Incomplete
                value = self._value(depth)
            except _Incomplete:
                return self._partial(result, depth, "truncated")
            result[key] = value

            self._skip_ws()
            if self._eof():
                return self._partial(result, depth, "unterminated_object")
            ch = self._s[self._i]
            if ch == ",":
                self._i += 1
            elif ch == "}":
                self._i += 1
                return result
            elif _KEY_START.match(ch):
                self.fixes.add("missing_comma")
            else:  # trailing garbage such as "..."
                return self._partial(result, depth, "unterminated_object")

    def _partial(self, result: dict, depth: int, fix: str) -> dict:
        if depth > 0:
            raise _Incomplete
        self.fixes.add(fix)
        return result

    def _array(self, depth: int) -> list:
        if depth > MAX_DEPTH:
            raise _Incomplete
        self._i += 1  # "["
        items: list = []
        while True:
            self._skip_ws()
            if self._eof():
                raise _Incomplete
            ch = self._s[self._i]
            if ch == "]":
                self._i += 1
                return items
            if ch == ",":
                self._i += 1
                continue
            items.append(self._value(depth))
            self._skip_ws()
            if self._eof():
                raise _Incomplete
            ch = self._s[self._i]
            if ch == ",":
                self._i += 1
            elif ch == "]":
                self._i += 1
                return items
            else:
                raise _Incomplete

    # --- scalars -------------------------------------------------------------------

    def _key(self) -> str:
        ch = self._s[self._i]
        if ch in "\"'":
            return self._string(ch, closers=":")
        match = _BARE_KEY.match(self._s, self._i)
        if not match:
            raise _Incomplete
        self.fixes.add("unquoted_keys")
        self._i = match.end()
        return match.group()

    def _value(self, depth: int):
        ch = self._s[self._i]
        if ch == "{":
            return self._object(depth + 1)
        if ch == "[":
            return self._array(depth + 1)
        if ch in "\"'":
            return self._string(ch, closers=",}]")
        return self._bare_value()

    def _string(self, quote: str, closers: str) -> str:
        if quote == "'":
            self.fixes.add("single_quotes")
        self._i += 1  # opening quote
        out: list[str] = []
        s = self._s
        while self._i < len(s):
            ch = s[self._i]
            if ch == "\\":
                out.append(self._escape())
                continue
            if ch == quote:
                if self._is_closing_quote(self._i + 1, quote, closers):
                    self._i += 1
                    return "".join(out)
                self.fixes.add("unescaped_quotes")
            out.append(ch)
            self._i += 1
        raise _Incomplete  # input ended inside the string

    def _is_closing_quote(self, pos: int, quote: str, closers: str) -> bool:
        """Decide whether a quote ends the string or is an unescaped quote inside it.

        It closes the string when followed by a structural character (or the end of input),
        by another quoted key in value position (a missing comma), or when nothing
        meaningful follows (e.g. a trailing ``...`` or a code fence).
        """
        nxt = _WHITESPACE.match(self._s, pos).end()
        if nxt >= len(self._s) or self._s[nxt] in closers:
            return True
        if "," in closers and _QUOTED_KEY_AHEAD.match(self._s, pos):
            return True
        return self._last_quote[quote] < pos and self._last_alnum < pos

    def _escape(self) -> str:
        s, i = self._s, self._i
        if i + 1 >= len(s):
            raise _Incomplete
        nxt = s[i + 1]
        if nxt == "u":
            hex_digits = s[i + 2 : i + 6]
            if len(hex_digits) < 4:
                raise _Incomplete
            if _HEX4.fullmatch(hex_digits):
                self._i += 6
                code = int(hex_digits, 16)
                if 0xD800 <= code <= 0xDBFF and s[self._i : self._i + 2] == "\\u":
                    low = s[self._i + 2 : self._i + 6]
                    if _HEX4.fullmatch(low) and 0xDC00 <= int(low, 16) <= 0xDFFF:
                        self._i += 6
                        return chr(0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00))
                return chr(code)
        self._i += 2
        return _ESCAPES.get(nxt, "\\" + nxt)  # unknown escapes are kept literally

    def _bare_value(self):
        match = _BARE_VALUE.match(self._s, self._i)
        self._i = match.end()
        if self._eof():
            raise _Incomplete  # an unterminated bare token may have been cut off
        token = match.group().strip()
        if not token:
            raise _Incomplete
        lowered = token.lower()
        if lowered in _LITERALS:
            return _LITERALS[lowered]
        if _INT.fullmatch(token):
            return int(token)
        if _FLOAT.fullmatch(token):
            return float(token)
        self.fixes.add("unquoted_values")
        return token

    # --- helpers ---------------------------------------------------------------------

    def _skip_ws(self) -> None:
        s = self._s
        while self._i < len(s) and s[self._i].isspace():
            self._i += 1

    def _eof(self) -> bool:
        return self._i >= len(self._s)
