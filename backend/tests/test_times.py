from datetime import UTC, datetime, timedelta, timezone

import pytest

from barrelvault.errors import Invalid
from barrelvault.times import as_utc, check_timezone, to_utc


def test_a_stored_naive_datetime_is_utc():
    assert as_utc(datetime(2000, 1, 1)) == datetime(2000, 1, 1, tzinfo=UTC)
    eastern = datetime(2000, 1, 1, 7, tzinfo=timezone(timedelta(hours=-5)))
    assert as_utc(eastern) == datetime(2000, 1, 1, 12, tzinfo=UTC)


def test_a_sent_datetime_with_an_offset_is_converted():
    sent = datetime(2026, 11, 6, 17, tzinfo=timezone(timedelta(hours=-6)))
    assert to_utc(sent, "Asia/Tokyo") == datetime(2026, 11, 6, 23, tzinfo=UTC)


def test_a_sent_datetime_without_an_offset_is_read_in_the_account_timezone():
    assert to_utc(datetime(2026, 7, 1, 17), "America/Chicago") == datetime(
        2026, 7, 1, 22, tzinfo=UTC
    )
    assert to_utc(datetime(2026, 12, 1, 17), "America/Chicago") == datetime(
        2026, 12, 1, 23, tzinfo=UTC
    )


def test_zoneinfo_settles_a_daylight_saving_gap_and_overlap():
    # 02:30 on 2026-03-08 does not exist in Chicago, and 01:30 on 2026-11-01 happens twice.
    # zoneinfo reads both with the offset in force before the change.
    assert to_utc(datetime(2026, 3, 8, 2, 30), "America/Chicago") == datetime(
        2026, 3, 8, 8, 30, tzinfo=UTC
    )
    assert to_utc(datetime(2026, 11, 1, 1, 30), "America/Chicago") == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )


def test_only_a_known_timezone_name_is_accepted():
    assert check_timezone("America/Chicago") == "America/Chicago"
    for name in ("Mars/Olympus", "../etc/passwd", "/etc/localtime", "not a zone"):
        with pytest.raises(Invalid) as raised:
            check_timezone(name)
        assert raised.value.message == "timezone is not a known name"
