import time

import pytest

from service.lenient_json import MAX_DEPTH, iter_json_objects

CLEAN = {"to": "a@b.com", "message": "hola", "type": "email"}


def first(text):
    return next(iter_json_objects(text), None)


# --- strict path -----------------------------------------------------------------


def test_clean_json_needs_no_fixes():
    result = first('{"to": "a@b.com", "message": "hola", "type": "email"}')
    assert result.data == CLEAN
    assert result.fixes == ()


@pytest.mark.parametrize(
    "text, fix",
    [
        ('He extraído:\n```json\n{"to": "a@b.com", "message": "hola", "type": "email"}\n```', "code_fence"),
        ('Output:\n```\n{"to": "a@b.com", "message": "hola", "type": "email"}\n```', "code_fence"),
        ('Claro. En formato JSON: {"to": "a@b.com", "message": "hola", "type": "email"} ¡Listo!', "surrounding_text"),
        ('[{"to": "a@b.com", "message": "hola", "type": "email"}]', "surrounding_text"),
    ],
)
def test_wrapped_json_is_found_strictly(text, fix):
    result = first(text)
    assert result.data == CLEAN
    assert result.fixes == (fix,)


def test_double_encoded_json():
    result = first('"{\\"to\\": \\"a@b.com\\", \\"message\\": \\"hola\\", \\"type\\": \\"email\\"}"')
    assert result.data == CLEAN
    assert result.fixes == ("double_encoded",)


def test_strict_candidates_come_before_lenient_ones():
    text = "{to: 'x@y.com'} and then {\"to\": \"a@b.com\"}"
    results = list(iter_json_objects(text))
    assert results[0].data == {"to": "a@b.com"}
    assert "lenient_json" in results[-1].fixes


def test_no_object_yields_nothing():
    assert list(iter_json_objects("Lo siento, no puedo procesar datos personales.")) == []
    assert list(iter_json_objects("")) == []


# --- lenient path ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, fix",
    [
        ("{'to': 'a@b.com', 'message': 'hola', 'type': 'email'}", "single_quotes"),
        ('{to: "a@b.com", message: "hola", type: "email"}', "unquoted_keys"),
        ('{"to": "a@b.com", "message": "hola", "type": "email" ...', "unterminated_object"),
        ('{"to": "a@b.com", "message": "hola", "type": "email"', "unterminated_object"),
        ('{"to": "a@b.com", "message": "hola", "type": "email",}', "lenient_json"),
        ('{"to": "a@b.com" "message": "hola" "type": "email"}', "missing_comma"),
        ("{to: a@b.com, message: hola, type: email}", "unquoted_values"),
    ],
)
def test_lenient_repairs(text, fix):
    result = first(text)
    assert result.data == CLEAN
    assert "lenient_json" in result.fixes
    assert fix in result.fixes


def test_truncated_fenced_block():
    result = first('```json\n{"to": "a@b.com", "message": "hola", "type": "email"\n```')
    assert result.data == CLEAN


def test_value_cut_off_mid_string_is_dropped_not_returned_partially():
    result = first('{"to": "a@b.com", "message": "hola, te escrib')
    assert result.data == {"to": "a@b.com"}
    assert "truncated" in result.fixes


def test_key_cut_off_is_dropped():
    assert first('{"to": "a@b.com", "mess').data == {"to": "a@b.com"}


def test_bare_value_cut_off_is_dropped():
    assert first('{to: "a@b.com", type: ema').data == {"to": "a@b.com"}


def test_unescaped_inner_quotes_are_kept():
    result = first('{"to": "a@b.com", "message": "dice "hola" a todos", "type": "email"}')
    assert result.data["message"] == 'dice "hola" a todos'
    assert result.data["type"] == "email"
    assert "unescaped_quotes" in result.fixes


def test_apostrophe_inside_single_quoted_value():
    result = first("{'to': 'a@b.com', 'message': 'it's ready', 'type': 'email'}")
    assert result.data["message"] == "it's ready"


def test_colon_inside_value_with_unquoted_keys():
    result = first('{to: "699888777", message: "cita a las 10:00", type: "sms"}')
    assert result.data == {"to": "699888777", "message": "cita a las 10:00", "type": "sms"}


def test_escape_sequences_are_decoded():
    result = first("{'message': 'l\\u00ednea\\n\\'dos\\' \\ud83d\\ude00 \\q'}")
    assert result.data["message"] == "línea\n'dos' \U0001F600 \\q"


def test_scalars_and_nesting():
    result = first("{a: 1, b: -2.5, c: true, d: null, e: None, f: +34600111222, g: [1, 'x'], h: {i: 'j'}}")
    assert result.data == {
        "a": 1, "b": -2.5, "c": True, "d": None, "e": None,
        "f": "+34600111222", "g": [1, "x"], "h": {"i": "j"},
    }


def test_incomplete_nested_value_is_dropped():
    assert first("{a: 'x', b: {c: 'd'").data == {"a": "x"}
    assert first("{a: 'x', b: [1, 2").data == {"a": "x"}


def test_deep_nesting_does_not_crash():
    text = "{a: " * (MAX_DEPTH + 5) + "1" + "}" * (MAX_DEPTH + 5)
    result = first(text)
    assert result is None or isinstance(result.data, dict)


@pytest.mark.parametrize(
    "text",
    [
        "{" * 50_000,
        '{"message": "' + '"x' * 20_000,  # thousands of inner quotes, never closed
        "{a: '" + "' " * 20_000,
    ],
    ids=["braces", "double-quotes", "single-quotes"],
)
def test_pathological_input_is_fast(text):
    started = time.perf_counter()
    list(iter_json_objects(text))
    assert time.perf_counter() - started < 1.0
