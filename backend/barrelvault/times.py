"""UTC datetimes and the account timezone. Every datetime is stored in UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from barrelvault.errors import Invalid


def now() -> datetime:
    """The current UTC time to the millisecond.

    A browser's Date keeps milliseconds, so an updated_at a client sends back
    compares equal to the stored one.
    """
    return _whole_ms(datetime.now(UTC))


def _whole_ms(moment: datetime) -> datetime:
    """Every stored time is whole milliseconds, the precision a response shows."""
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def as_utc(value: datetime) -> datetime:
    """A stored datetime in UTC. SQLite hands back a naive one, which was stored as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc(value: datetime, timezone: str) -> datetime:
    """A datetime the caller sent, in UTC, to the millisecond.

    One with an offset is converted. One without an offset is read in the account's
    timezone, and zoneinfo settles a daylight-saving gap or overlap.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone))
    return _whole_ms(value.astimezone(UTC))


def check_timezone(name: str) -> str:
    """Return the name when zoneinfo knows it. Refuse it as Invalid otherwise."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid("timezone is not a known name") from None
    return name
