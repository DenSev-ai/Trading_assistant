"""Сторожевые проверки сервиса: молчит в норме, шлёт один алерт на сбой (STEP 5)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..clock import to_iso
from ..config import AppSettings
from ..llm.budget import Budget
from ..repository import Repository
from .dispatcher import Notifier

logger = logging.getLogger(__name__)

HEALTH_PREFIX = "health"
CYCLE_KEY = "last_cycle_ok"
COLLECT_OK_KEY = "last_collect_ok"
SOURCES_DOWN_KEY = "collect_all_failed"
CYCLE_FACTOR = 3
FATAL_WINDOW_HOURS = 24

CYCLE_STALE = "cycle_stale"
BUDGET = "budget"
LLM_FATAL = "llm_fatal"
SOURCES_DOWN = "sources_down"
HEALTH_KINDS = (CYCLE_STALE, BUDGET, LLM_FATAL, SOURCES_DOWN)

CYCLE_TEXT = (
    "Trading assistant: нет успешного цикла дольше {seconds} с. "
    "Проверьте сервис и data/errors.log."
)
BUDGET_TEXT = (
    "Trading assistant: суточный бюджет LLM исчерпан "
    "(${spent:.4f} из ${limit:.4f}). Анализ остановлен до следующих суток."
)
LLM_FATAL_TEXT = (
    "Trading assistant: сбой LLM (401/402/403). Проверьте ключ и баланс провайдера."
)
SOURCES_DOWN_TEXT = (
    "Trading assistant: недоступны все RSS-источники. Проверьте сеть и data/errors.log."
)

Sleep = Callable[[float], Awaitable[None]]


class HealthMonitor:
    """Проверки «нет цикла / бюджет / фатальный LLM / все RSS недоступны»."""

    def __init__(
        self,
        repo: Repository,
        notifier: Notifier,
        chat_ids: list[int],
        settings: AppSettings,
        tz: ZoneInfo,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Запомнить зависимости: БД, отправку, получателей, настройки и пояс."""
        self._repo = repo
        self._notifier = notifier
        self._chat_ids = list(chat_ids)
        self._settings = settings
        self._tz = tz
        self._sleep = sleep

    async def check(self, now: datetime) -> list[str]:
        """Проверить состояние сервиса; вернуть виды сбоев, о которых сообщили сейчас."""
        problems = await self._problems(now)
        sent: list[str] = []
        for kind in HEALTH_KINDS:
            key = f"{HEALTH_PREFIX}:{kind}"
            flagged = await self._repo.kv_get(key) is not None
            if kind in problems:
                if flagged:
                    continue
                await self._notify(kind, now)
                await self._repo.kv_set(key, to_iso(now))
                sent.append(kind)
            elif flagged:
                await self._repo.kv_delete(key)
        return sent

    async def _problems(self, now: datetime) -> set[str]:
        """Множество текущих сбоев (по одному на вид)."""
        problems: set[str] = set()
        if await self._cycle_stale(now):
            problems.add(CYCLE_STALE)
        spent = await Budget(
            self._repo, self._settings.llm.daily_budget_usd, self._tz
        ).spent_today(now)
        if spent >= self._settings.llm.daily_budget_usd:
            problems.add(BUDGET)
        since = now - timedelta(hours=FATAL_WINDOW_HOURS)
        if await self._repo.recent_fatal_run(since) is not None:
            problems.add(LLM_FATAL)
        if await self._repo.kv_get(SOURCES_DOWN_KEY) is not None:
            problems.add(SOURCES_DOWN)
        return problems

    async def _cycle_stale(self, now: datetime) -> bool:
        """True, если успешного цикла не было дольше CYCLE_FACTOR интервалов (или не было вовсе)."""
        last = await self._repo.kv_get(CYCLE_KEY)
        if last is None:
            return True
        allowed = timedelta(seconds=CYCLE_FACTOR * self._settings.polling.cycle_interval_sec)
        return datetime.fromisoformat(last) < now - allowed

    async def _notify(self, kind: str, now: datetime) -> None:
        """Отправить алерт о сбое всем получателям с паузой между сообщениями."""
        text = await self._text(kind, now)
        for index, chat_id in enumerate(self._chat_ids):
            if index:
                await self._sleep(self._settings.telegram.send_pause_sec)
            await self._notifier.send(chat_id, text)

    async def _text(self, kind: str, now: datetime) -> str:
        """Текст алерта по виду сбоя с текущими числами."""
        if kind == BUDGET:
            spent = await Budget(
                self._repo, self._settings.llm.daily_budget_usd, self._tz
            ).spent_today(now)
            return BUDGET_TEXT.format(
                spent=spent, limit=self._settings.llm.daily_budget_usd
            )
        if kind == CYCLE_STALE:
            seconds = CYCLE_FACTOR * self._settings.polling.cycle_interval_sec
            return CYCLE_TEXT.format(seconds=seconds)
        if kind == LLM_FATAL:
            return LLM_FATAL_TEXT
        return SOURCES_DOWN_TEXT
