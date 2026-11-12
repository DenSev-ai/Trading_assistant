"""Стоимость вызова модели и суточный лимит расхода (CONTRACTS §4)."""
from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ..config import ModelSpec
from ..repository import Repository

# Цены в конфиге заданы за 1 млн токенов.
TOKENS_PER_PRICE_UNIT = 1e6


def cost_usd(tokens_in: int, tokens_out: int, model: ModelSpec) -> float:
    """Стоимость запроса: вход/1e6*price_in + выход/1e6*price_out."""
    return (
        tokens_in / TOKENS_PER_PRICE_UNIT * model.price_in
        + tokens_out / TOKENS_PER_PRICE_UNIT * model.price_out
    )


def _day_start_utc(now: datetime, tz: ZoneInfo) -> datetime:
    """Полночь текущих суток по tz, переведённая в UTC."""
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time.min, tzinfo=tz).astimezone(UTC)


class Budget:
    """Суточный лимит расхода на вызовы моделей: сумма model_runs.cost_usd с полуночи МСК."""

    def __init__(self, repo: Repository, daily_limit: float, tz: ZoneInfo) -> None:
        self._repo = repo
        self._daily_limit = daily_limit
        self._tz = tz

    async def spent_today(self, now: datetime) -> float:
        """Израсходовано за текущие сутки МСК (начало суток переведено в UTC)."""
        return await self._repo.spent_since(_day_start_utc(now, self._tz))

    async def allowed(self, now: datetime, reserve_share: float = 1.0) -> bool:
        """True, если расход за сутки меньше лимита с учётом доли резерва."""
        return await self.spent_today(now) < self._daily_limit * reserve_share
