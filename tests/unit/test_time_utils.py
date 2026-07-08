from __future__ import annotations

from datetime import UTC, datetime

from altlink.utils.time import format_msk_date, format_msk_datetime


def test_format_msk_datetime_converts_utc_and_adds_timezone_label():
    value = datetime(2026, 7, 8, 12, 30, tzinfo=UTC)

    assert format_msk_datetime(value) == "08.07.2026 15:30 МСК"


def test_format_msk_datetime_treats_naive_datetime_as_utc():
    value = datetime(2026, 7, 8, 12, 30)

    assert format_msk_datetime(value) == "08.07.2026 15:30 МСК"


def test_format_msk_datetime_accepts_iso_string():
    assert format_msk_datetime("2026-07-08T12:30:00+00:00") == "08.07.2026 15:30 МСК"


def test_format_msk_date_adds_timezone_label():
    value = datetime(2026, 7, 8, 21, 30, tzinfo=UTC)

    assert format_msk_date(value) == "09.07.2026 МСК"
