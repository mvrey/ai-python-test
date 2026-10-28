from dataclasses import fields

import pytest

from service.config import Settings, _parse


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate tests from whatever is set in the developer's shell."""
    for f in fields(Settings):
        monkeypatch.delenv(f.name.upper(), raising=False)


def test_defaults_without_env():
    settings = Settings.from_env()
    assert settings.provider_base_url == "http://localhost:3001"
    assert settings.notify_rate_per_sec < 5  # provider limit is 50 req / 10 s


def test_env_overrides_are_cast_to_field_type(monkeypatch):
    monkeypatch.setenv("NOTIFY_RATE_PER_SEC", "3.5")
    monkeypatch.setenv("EXTRACTION_WORKERS", "20")
    monkeypatch.setenv("PROVIDER_BASE_URL", "http://provider:3001")

    settings = Settings.from_env()

    assert settings.notify_rate_per_sec == 3.5
    assert settings.extraction_workers == 20
    assert settings.provider_base_url == "http://provider:3001"


def test_invalid_env_value_names_the_variable(monkeypatch):
    monkeypatch.setenv("NOTIFY_WORKERS", "five")
    with pytest.raises(ValueError, match="NOTIFY_WORKERS"):
        Settings.from_env()


@pytest.mark.parametrize("name", ["NOTIFY_WORKERS", "NOTIFY_RATE_PER_SEC", "AI_TIMEOUT_SEC"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_numbers_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"{name} must be positive"):
        Settings.from_env()


@pytest.mark.parametrize(
    "raw, expected",
    [("true", True), ("1", True), ("ON", True), ("false", False), ("0", False), (" no ", False)],
)
def test_bool_parsing_is_explicit(raw, expected):
    assert _parse(raw, bool) is expected


def test_bool_setting_from_env(monkeypatch):
    monkeypatch.setenv("ACCESS_LOG", "true")
    assert Settings.from_env().access_log is True


def test_public_dict_masks_the_api_key():
    public = Settings(provider_api_key="secret").public_dict()
    assert public["provider_api_key"] == "***"
    assert "secret" not in str(public)
    assert public["notify_rate_per_sec"] == 4.5


def test_bool_parsing_rejects_unknown_values():
    with pytest.raises(ValueError, match="expected one of"):
        _parse("maybe", bool)
