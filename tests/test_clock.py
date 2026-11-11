"""Тесты clock.py: фазы расписания, рабочие дни, форматирование времени."""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from trading_assistant.clock import (
    FakeClock,
    Phase,
    SystemClock,
    get_phase,
    is_working_day,
    msk_date,
    to_iso,
)
from trading_assistant.config import ScheduleSettings

MSK = ZoneInfo("Europe/Moscow")


def sched(
    active_from: str = "09:45",
    stop_at: str = "22:40",
    skip_weekends: bool = True,
    non_working_dates: list[str] | None = None,
) -> ScheduleSettings:
    return ScheduleSettings(
        active_from=active_from,
        stop_at=stop_at,
        non_working_dates=non_working_dates or [],
        skip_weekends=skip_weekends,
    )


def msk_local(hour: int, minute: int, second: int = 0) -> datetime:
    """Пятница 2026-11-06 по МСК, переданная в get_phase в UTC."""
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=MSK).astimezone(UTC)


@pytest.mark.parametrize(
    "hour,minute,second,expected",
    [
        (0, 0, 0, Phase.PASSIVE),
        (8, 0, 0, Phase.PASSIVE),
        (9, 44, 59, Phase.PASSIVE),
        (9, 45, 0, Phase.ACTIVE),
        (22, 39, 59, Phase.ACTIVE),
        (22, 40, 0, Phase.STOPPED),
        (23, 59, 0, Phase.STOPPED),
    ],
)
def test_get_phase_table(hour: int, minute: int, second: int, expected: Phase) -> None:
    assert get_phase(msk_local(hour, minute, second), sched(), MSK) == expected


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_fake_clock() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now().hour == 0
    clock.advance(hours=2, minutes=30)
    assert clock.now().hour == 2
    clock.set(datetime(2026, 6, 1, 12, tzinfo=UTC))
    assert clock.now().month == 6


@pytest.mark.parametrize(
    "weekday_name,skip,expected",
    [
        ("Saturday", True, False),
        ("Sunday", True, False),
        ("Friday", True, True),
        ("Saturday", False, True),
    ],
)
def test_is_working_day(weekday_name: str, skip: bool, expected: bool) -> None:
    # 2026-11-06 Friday, 2026-11-07 Saturday, 2026-11-08 Sunday
    day_map = {"Friday": 6, "Saturday": 7, "Sunday": 8}
    now = datetime(2026, 11, day_map[weekday_name], 10, 0, tzinfo=MSK)
    assert is_working_day(now, sched(skip_weekends=skip), MSK) is expected


def test_is_working_day_friday_holiday(msk: ZoneInfo) -> None:
    friday = datetime(2026, 11, 6, 10, 0, tzinfo=MSK)
    assert not is_working_day(
        friday, sched(non_working_dates=["2026-11-06"]), MSK
    )


def test_to_iso() -> None:
    dt = datetime(2026, 11, 6, 18, 30, 0, tzinfo=UTC)
    assert to_iso(dt) == "2026-11-06T18:30:00+00:00"


def test_msk_date() -> None:
    assert msk_date("2026-11-06T21:30:00+00:00", MSK) == date(2026, 11, 7)
