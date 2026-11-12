"""Тесты сторожа здоровья сервиса (STEP 5)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_assistant.clock import to_iso
from trading_assistant.config import load_settings
from trading_assistant.db import Database
from trading_assistant.repository import Repository
from trading_assistant.telegram.health import (
    BUDGET,
    CYCLE_KEY,
    CYCLE_STALE,
    HEALTH_PREFIX,
    LLM_FATAL,
    SOURCES_DOWN,
    SOURCES_DOWN_KEY,
    HealthMonitor,
)

MSK = ZoneInfo("Europe/Moscow")
CHAT_IDS = [101, 202]
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
# cycle_interval_sec=180 из конфига -> сбой, если цикла нет дольше 9 минут.
STALE_AFTER = NOW - timedelta(minutes=10)


class FakeNotifier:
    """Поддельная отправка: запоминает сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str) -> bool:
        """Запомнить сообщение и считать его доставленным."""
        self.messages.append((chat_id, text))
        return True


class FakeSleep:
    """Подмена asyncio.sleep без ожидания."""

    async def __call__(self, seconds: float) -> None:
        """Ничего не делать."""


def _monitor(db: Database) -> tuple[HealthMonitor, FakeNotifier]:
    """Монитор на :memory:-БД с поддельной отправкой."""
    settings = load_settings(Path("config/settings.yaml"))
    notifier = FakeNotifier()
    monitor = HealthMonitor(
        Repository(db), notifier, CHAT_IDS, settings, MSK, FakeSleep()
    )
    return monitor, notifier


async def _cycle_ok(db: Database, moment: datetime) -> None:
    """Отметить успешное завершение цикла."""
    await Repository(db).kv_set(CYCLE_KEY, to_iso(moment))


async def _run(db: Database, status: str = "ok", cost: float = 0.0) -> None:
    """Записать вызов модели с указанным статусом и стоимостью."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, status) "
        "VALUES ('Новость', 'новость', ?, ?, 'analyzed')",
        (to_iso(NOW), to_iso(NOW)),
    )
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, cost_usd, created_at) "
        "VALUES (?, 'anthropic/claude-sonnet-5.5', 'v2', ?, ?, ?)",
        (cluster_id, status, cost, to_iso(NOW)),
    )


async def test_healthy_service_is_silent(db) -> None:
    """1) Всё в порядке — ни одного сообщения."""
    await _cycle_ok(db, NOW - timedelta(minutes=1))
    monitor, notifier = _monitor(db)
    assert await monitor.check(NOW) == []
    assert notifier.messages == []


async def test_stale_cycle_alerts_once_and_resets(db) -> None:
    """2) Нет цикла 10 минут при интервале 3 мин: один алерт, затем тишина и сброс."""
    await _cycle_ok(db, STALE_AFTER)
    monitor, notifier = _monitor(db)
    assert await monitor.check(NOW) == [CYCLE_STALE]
    assert len(notifier.messages) == len(CHAT_IDS)
    assert await monitor.check(NOW) == []
    assert len(notifier.messages) == len(CHAT_IDS)

    # Восстановление: флаг снимается.
    await _cycle_ok(db, NOW)
    assert await monitor.check(NOW) == []
    assert await Repository(db).kv_get(f"{HEALTH_PREFIX}:{CYCLE_STALE}") is None

    # Новый сбой — снова один алерт.
    await _cycle_ok(db, STALE_AFTER)
    assert await monitor.check(NOW) == [CYCLE_STALE]
    assert len(notifier.messages) == 2 * len(CHAT_IDS)


async def test_budget_exhausted_alert(db) -> None:
    """Израсходованный суточный бюджет даёт один алерт."""
    await _cycle_ok(db, NOW)
    await _run(db, cost=0.6)
    monitor, notifier = _monitor(db)
    assert await monitor.check(NOW) == [BUDGET]
    assert "бюджет" in notifier.messages[0][1]
    assert await monitor.check(NOW) == []


async def test_fatal_llm_alert(db) -> None:
    """Фатальный сбой LLM (401/402/403) даёт один алерт."""
    await _cycle_ok(db, NOW)
    await _run(db, status="fatal")
    monitor, notifier = _monitor(db)
    assert await monitor.check(NOW) == [LLM_FATAL]
    assert "401/402/403" in notifier.messages[0][1]


async def test_all_sources_down_alert(db) -> None:
    """Флаг «все RSS недоступны» даёт один алерт и сбрасывается после восстановления."""
    await _cycle_ok(db, NOW)
    await Repository(db).kv_set(SOURCES_DOWN_KEY, to_iso(NOW))
    monitor, _notifier = _monitor(db)
    assert await monitor.check(NOW) == [SOURCES_DOWN]
    assert await monitor.check(NOW) == []

    await Repository(db).kv_delete(SOURCES_DOWN_KEY)
    assert await monitor.check(NOW) == []
    assert await Repository(db).kv_get(f"{HEALTH_PREFIX}:{SOURCES_DOWN}") is None
