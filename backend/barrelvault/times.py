"""UTC datetimes and the account timezone. Every datetime is stored in UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from barrelvault.errors import Invalid


def as_utc(value: datetime) -> datetime:
    """A stored datetime in UTC. SQLite hands back a naive one, which was stored as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc(value: datetime, timezone: str) -> datetime:
    """A datetime the caller sent, in UTC.

    One with an offset is converted. One without an offset is read in the account's
    timezone, and zoneinfo settles a daylight-saving gap or overlap.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone))
    return value.astimezone(UTC)


def check_timezone(name: str) -> str:
    """Return the name when zoneinfo knows it. Refuse it as Invalid otherwise."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid("timezone is not a known name") from None
    return name
