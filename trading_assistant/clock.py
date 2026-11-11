"""Часы, фаза расписания и вспомогательные функции времени."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from .config import ScheduleSettings


class Clock:
    """Протокол часов: now() возвращает aware UTC время."""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    """Реальные часы. Единственное место, где вызывается настоящее время."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock(Clock):
    """Часы для тестов: можно явно задать время и продвигать."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt

    def advance(self, **kw) -> None:
        self._now += timedelta(**kw)


class Phase(StrEnum):
    PASSIVE = "passive"
    ACTIVE = "active"
    STOPPED = "stopped"


def get_phase(now: datetime, sched: ScheduleSettings, tz: ZoneInfo) -> Phase:
    """Фаза расписания для момента now (вход обычно в UTC)."""
    local = now.astimezone(tz)
    start = time.fromisoformat(sched.active_from)
    stop = time.fromisoformat(sched.stop_at)
    if start <= local.time() < stop:
        return Phase.ACTIVE
    if local.time() >= stop:
        return Phase.STOPPED
    return Phase.PASSIVE


def is_working_day(now: datetime, sched: ScheduleSettings, tz: ZoneInfo) -> bool:
    """True, если по МСК это рабочий день (не выходной и не нерабочий праздник)."""
    local = now.astimezone(tz)
    if local.date().isoformat() in sched.non_working_dates:
        return False
    if sched.skip_weekends and local.weekday() >= 5:
        return False
    return True


def to_iso(dt: datetime) -> str:
    """aware datetime -> 'YYYY-MM-DDTHH:MM:SS+00:00' (UTC)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def msk_date(iso: str, tz: ZoneInfo) -> date:
    """Дата по tz для ISO-строки с utc-смещением."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).date()
