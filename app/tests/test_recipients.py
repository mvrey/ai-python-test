import pytest

from service.recipients import Recipient, find_recipients, parse_recipient


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("feda@test.com", Recipient("feda@test.com", "email")),
        ("  <Juan.Perez+tag@mail.example.es>  ", Recipient("Juan.Perez+tag@mail.example.es", "email")),
        ("mailto:feda@test.com", Recipient("feda@test.com", "email")),
        ("feda@test.com.", Recipient("feda@test.com", "email")),
        ("600-111-222", Recipient("600111222", "sms")),
        ("699888777", Recipient("699888777", "sms")),
        ("+34 600 111 222", Recipient("+34600111222", "sms")),
        ("(91) 123.45.67", Recipient("911234567", "sms")),
        ("tel:+34600111222", Recipient("+34600111222", "sms")),
    ],
)
def test_parse_valid_recipients(raw, expected):
    assert parse_recipient(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "juan", "juan@", "juan@example", "@example.com", "12345678", "1234567890123456", "600-111-22a", "10:00"],
)
def test_parse_invalid_recipients(raw):
    assert parse_recipient(raw) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Enviar email a juan@example.com diciendo que su paquete está en camino.",
         [Recipient("juan@example.com", "email")]),
        ("Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.", [Recipient("600111222", "sms")]),
        ("Manda un correo a marta@app.com: Tienes un nuevo mensaje.", [Recipient("marta@app.com", "email")]),
        ("Recordatorio por teléfono al 699888777: cita a las 10:00", [Recipient("699888777", "sms")]),
    ],
)
def test_find_recipients_in_k6_prompts(text, expected):
    assert find_recipients(text) == expected


def test_find_multiple_recipients_in_order_and_deduplicated():
    text = "SMS al +34 600 111 222 y email a ana@x.es; repito: 600111222 y ANA@x.es"
    assert find_recipients(text) == [Recipient("+34600111222", "sms"), Recipient("ana@x.es", "email")]


@pytest.mark.parametrize(
    "text",
    [
        "Cita el 2026-10-26 a las 10:00",
        "Tu código es 123456",
        "Pedido 12345678 confirmado",
        "Escribe a soporte en la web",
        "Mi id es abc699888777xyz",
    ],
)
def test_no_false_positive_recipients(text):
    assert find_recipients(text) == []


def test_digits_inside_an_email_are_not_a_phone():
    assert find_recipients("Escribe a user600111222@x.com") == [Recipient("user600111222@x.com", "email")]


def test_contiguous_phone_does_not_swallow_following_number():
    assert find_recipients("Llama al 699888777 10 veces") == [Recipient("699888777", "sms")]


def test_matches_tolerates_country_code_and_email_case():
    assert Recipient("+34600111222", "sms").matches(Recipient("600111222", "sms"))
    assert Recipient("Ana@X.es", "email").matches(Recipient("ana@x.es", "email"))
    assert not Recipient("600111222", "sms").matches(Recipient("600111223", "sms"))
    assert not Recipient("600111222", "sms").matches(Recipient("600111222@x.es", "email"))
