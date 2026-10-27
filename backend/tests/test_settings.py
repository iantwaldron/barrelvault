from pathlib import Path

import pytest

from barrelvault.settings import (
    DEFAULT_DB_PATH,
    DEFAULT_KEY_LENGTH,
    DEFAULT_SESSION_LENGTH,
    SettingsError,
    load_settings,
)


def test_defaults_when_unset():
    settings = load_settings({})
    assert settings.db_path == DEFAULT_DB_PATH
    assert settings.session_length == DEFAULT_SESSION_LENGTH
    assert DEFAULT_SESSION_LENGTH == 604800
    assert settings.key_length == DEFAULT_KEY_LENGTH
    assert DEFAULT_KEY_LENGTH == 31536000


def test_blank_values_use_defaults():
    settings = load_settings(
        {
            "BARRELVAULT_DB": "  ",
            "BARRELVAULT_SESSION_LENGTH": " ",
            "BARRELVAULT_KEY_LENGTH": " ",
        }
    )
    assert settings.db_path == DEFAULT_DB_PATH
    assert settings.session_length == DEFAULT_SESSION_LENGTH
    assert settings.key_length == DEFAULT_KEY_LENGTH


def test_overrides():
    settings = load_settings(
        {
            "BARRELVAULT_DB": "/var/lib/barrelvault.db",
            "BARRELVAULT_SESSION_LENGTH": "3600",
            "BARRELVAULT_KEY_LENGTH": "86400",
        }
    )
    assert settings.db_path == "/var/lib/barrelvault.db"
    assert settings.session_length == 3600
    assert settings.key_length == 86400


def test_reads_process_environment(monkeypatch):
    monkeypatch.delenv("BARRELVAULT_DB", raising=False)
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    monkeypatch.setenv("BARRELVAULT_SESSION_LENGTH", "90")
    monkeypatch.setenv("BARRELVAULT_KEY_LENGTH", "120")
    settings = load_settings()
    assert settings.session_length == 90
    assert settings.key_length == 120


@pytest.mark.parametrize(
    "name",
    ["BARRELVAULT_SESSION_LENGTH", "BARRELVAULT_KEY_LENGTH"],
)
@pytest.mark.parametrize("raw", ["0", "-5", "nope", "1.5"])
def test_bad_length_is_refused(name, raw):
    with pytest.raises(SettingsError):
        load_settings({name: raw})


def test_the_default_database_is_in_the_backend_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = load_settings({})
    assert Path(settings.db_path) == Path(__file__).resolve().parents[1] / "barrelvault.db"
