"""The only reader of Barrelvault environment variables."""

import os
from dataclasses import dataclass

DEFAULT_DB_PATH = "backend/barrelvault.db"
DEFAULT_SESSION_LENGTH = 604800


class SettingsError(ValueError):
    """An environment variable is present and not usable."""


@dataclass(frozen=True)
class Settings:
    db_path: str
    session_length: int


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    return Settings(
        db_path=_db_path(env.get("BARRELVAULT_DB")),
        session_length=_session_length(env.get("BARRELVAULT_SESSION_LENGTH")),
    )


def _db_path(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return DEFAULT_DB_PATH
    return raw


def _session_length(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_SESSION_LENGTH
    try:
        length = int(raw)
    except ValueError:
        raise SettingsError(
            "BARRELVAULT_SESSION_LENGTH must be a positive integer"
        ) from None
    if length <= 0:
        raise SettingsError(
            "BARRELVAULT_SESSION_LENGTH must be a positive integer"
        )
    return length
