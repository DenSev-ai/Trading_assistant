"""Тесты Budget и Repository.spent_since: суточный лимит расхода по МСК (CONTRACTS §1, §4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading_assistant.clock import to_iso
from trading_assistant.db import Database
from trading_assistant.llm.budget import Budget
from trading_assistant.repository import Repository

LIMIT = 0.50
NOW_UTC = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)  # 21:00 МСК 6 ноября


async def _add_run(db: Database, moment: datetime, cost: float) -> None:
    """Записать в model_runs вызов модели стоимостью cost в момент moment."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at) "
        "VALUES ('t', 'т', ?, ?)",
        (to_iso(moment), to_iso(moment)),
    )
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, cost_usd, created_at) "
        "VALUES (?, 'test/model', 'v2', 'ok', ?, ?)",
        (cluster_id, cost, to_iso(moment)),
    )


def _budget(db: Database, tz: ZoneInfo, limit: float = LIMIT) -> Budget:
    """Бюджет на репозитории с лимитом за сутки МСК."""
    return Budget(Repository(db), limit, tz)


async def test_spent_since_empty(db) -> None:
    """Без запросов к моделям расход нулевой."""
    assert await Repository(db).spent_since(NOW_UTC) == 0.0


async def test_spent_since_sums_not_older(db) -> None:
    """Суммируются только записи не старше since (граница включается)."""
    first = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    second = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    await _add_run(db, first, 0.1)
    await _add_run(db, second, 0.2)
    repo = Repository(db)
    assert await repo.spent_since(first) == pytest.approx(0.3)
    assert await repo.spent_since(second) == pytest.approx(0.2)
    assert await repo.spent_since(second + timedelta(seconds=1)) == 0.0


async def test_spent_today_sums_runs_of_today(db, msk) -> None:
    """Расход за сутки — сумма всех запросов с начала суток МСК."""
    await _add_run(db, NOW_UTC, 0.2)
    await _add_run(db, NOW_UTC - timedelta(hours=2), 0.1)
    assert await _budget(db, msk).spent_today(NOW_UTC) == pytest.approx(0.3)


async def test_spent_today_ignores_yesterday_msk(db, msk) -> None:
    """Запрос вчерашних суток МСК в сегодняшний расход не входит."""
    # 00:30 МСК 7 ноября: начало суток — 21:00 UTC 6 ноября.
    now = datetime(2026, 11, 6, 21, 30, tzinfo=UTC)
    await _add_run(db, datetime(2026, 11, 6, 20, 0, tzinfo=UTC), 0.3)  # 23:00 МСК 6 ноября
    await _add_run(db, datetime(2026, 11, 6, 21, 10, tzinfo=UTC), 0.1)  # 00:10 МСК 7 ноября
    assert await _budget(db, msk).spent_today(now) == pytest.approx(0.1)


async def test_spent_today_counts_midnight_msk(db, msk) -> None:
    """Запрос ровно в полночь МСК входит в сутки, секундой раньше — уже нет."""
    day_start = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)  # 00:00 МСК 6 ноября
    await _add_run(db, day_start, 0.2)
    await _add_run(db, day_start - timedelta(seconds=1), 0.3)
    assert await _budget(db, msk).spent_today(NOW_UTC) == pytest.approx(0.2)


@pytest.mark.parametrize("cost,expected", [(0.0, True), (0.49, True), (0.50, False)])
async def test_allowed_by_daily_limit(db, msk, cost, expected) -> None:
    """Лимит 0.50: 0.49 — можно, 0.50 — уже нет."""
    if cost:
        await _add_run(db, NOW_UTC, cost)
    assert await _budget(db, msk).allowed(NOW_UTC) is expected


@pytest.mark.parametrize("cost,expected", [(0.34, True), (0.35, False)])
async def test_allowed_by_reserve_share(db, msk, cost, expected) -> None:
    """Доля резерва 0.7 от 0.50 = 0.35: 0.34 — можно, 0.35 — нет."""
    await _add_run(db, NOW_UTC, cost)
    assert await _budget(db, msk).allowed(NOW_UTC, reserve_share=0.7) is expected
