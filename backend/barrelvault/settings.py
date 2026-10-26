"""The only reader of Barrelvault environment variables."""

import os
from dataclasses import dataclass

DEFAULT_DB_PATH = "backend/barrelvault.db"
DEFAULT_SESSION_LENGTH = 604800
DEFAULT_KEY_LENGTH = 31536000


class SettingsError(ValueError):
    """An environment variable is present and not usable."""


@dataclass(frozen=True)
class Settings:
    db_path: str
    session_length: int
    key_length: int


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    return Settings(
        db_path=_db_path(env.get("BARRELVAULT_DB")),
        session_length=_positive_int(
            env.get("BARRELVAULT_SESSION_LENGTH"),
            "BARRELVAULT_SESSION_LENGTH",
            DEFAULT_SESSION_LENGTH,
        ),
        key_length=_positive_int(
            env.get("BARRELVAULT_KEY_LENGTH"),
            "BARRELVAULT_KEY_LENGTH",
            DEFAULT_KEY_LENGTH,
        ),
    )


def _db_path(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return DEFAULT_DB_PATH
    return raw


def _positive_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        length = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a positive integer") from None
    if length <= 0:
        raise SettingsError(f"{name} must be a positive integer")
    return length
