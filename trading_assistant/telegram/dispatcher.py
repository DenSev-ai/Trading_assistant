"""Доставка сигналов в Telegram: анти-спам, суточный лимит, дайджест (STEP 5)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import LinkPreviewOptions

from ..clock import Phase
from ..config import SignalSettings, TelegramSettings
from ..instruments import InstrumentRegistry
from ..llm.consensus import ConsensusRow, is_priority
from ..repository import Repository
from ..stats import CONSENSUS_MODEL, Stats
from .formatter import SignalView, format_alert, format_digest

logger = logging.getLogger(__name__)

ALERT_KIND = "alert"
DIGEST_KIND = "digest"
SUPPRESSED_KIND = "suppressed"
# Виды доставок, из-за которых работает анти-спам по тикеру.
ANTISPAM_KINDS = (ALERT_KIND, DIGEST_KIND)
DIGEST_KEY = "last_digest_date"
DIGEST_WINDOW = timedelta(hours=24)
# Ссылки в сообщениях не разворачиваем: сообщения короткие.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

Sleep = Callable[[float], Awaitable[None]]


class Notifier(Protocol):
    """Отправка текста одному получателю; False — доставить не удалось."""

    async def send(self, chat_id: int, text: str) -> bool:
        """Отправить текст в чат chat_id, вернуть признак успеха."""
        ...


class LogNotifier:
    """Без Telegram-переменных и в режиме --dry-run сообщения идут в лог."""

    async def send(self, chat_id: int, text: str) -> bool:
        """Записать сообщение в лог и считать его доставленным."""
        logger.info("telegram -> %s:\n%s", chat_id, text)
        return True


class BotNotifier:
    """Отправка через aiogram Bot; ошибка одного получателя не прерывает остальных."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> bool:
        """Отправить HTML-сообщение; ошибки Telegram логируются, возвращается False."""
        try:
            await self._bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                link_preview_options=NO_PREVIEW,
            )
        except TelegramForbiddenError:
            logger.warning("получатель %s заблокировал бота", chat_id)
            return False
        except (TelegramAPIError, OSError) as exc:
            logger.warning("не отправить сообщение в %s: %s", chat_id, exc)
            return False
        return True

    async def close(self) -> None:
        """Закрыть HTTP-сессию бота при остановке сервиса."""
        await self._bot.session.close()


@dataclass
class DispatchStats:
    """Счётчики этапа доставки: алерты, подавленные анти-спамом, отложенные в дайджест."""

    alerts: int = 0
    suppressed: int = 0
    queued: int = 0
    digest_messages: int = 0
    errors: int = 0


def _consensus_row(row: dict) -> ConsensusRow:
    """Строка БД -> значение для проверки приоритета алерта (CONTRACTS §5)."""
    return ConsensusRow(
        ticker=str(row["ticker"]),
        direction=str(row["direction"]),
        confidence=int(row["confidence"]),
        severity=int(row["severity"]),
        horizon_days=int(row["horizon_days"]),
        expected_change_pct=float(row["expected_change_pct"]),
        reason=str(row["reason"]),
    )


class Dispatcher:
    """Рассылка сигналов: новые алерты в активной фазе и утренний дайджест."""

    def __init__(
        self,
        repo: Repository,
        stats: Stats,
        notifier: Notifier,
        chat_ids: list[int],
        cfg: TelegramSettings,
        registry: InstrumentRegistry,
        sleep: Sleep = asyncio.sleep,
        *,
        tz: ZoneInfo,
        signal: SignalSettings,
        pair_key: str,
        prompt_version: str,
        record_deliveries: bool = True,
    ) -> None:
        """Запомнить зависимости; record_deliveries=False — режим --dry-run."""
        self._repo = repo
        self._stats = stats
        self._notifier = notifier
        self._chat_ids = list(chat_ids)
        self._cfg = cfg
        self._registry = registry
        self._sleep = sleep
        self._tz = tz
        self._signal = signal
        self._pair_key = pair_key
        self._prompt_version = prompt_version
        self._record_deliveries = record_deliveries

    @property
    def pair_key(self) -> str:
        """Ключ боевой пары, по которому считаются сигналы и статистика."""
        return self._pair_key

    @property
    def prompt_version(self) -> str:
        """Версия промта, к которой привязана статистика."""
        return self._prompt_version

    async def dispatch(self, phase: Phase, now: datetime) -> DispatchStats:
        """Разослать новые сигналы и дайджест; в PASSIVE и STOPPED не отправляется ничего."""
        result = DispatchStats()
        if phase is not Phase.ACTIVE:
            return result
        # Дайджест идёт первым: накопленные за ночь сигналы должны попасть в него,
        # а не уйти отдельными алертами на первом цикле активной фазы.
        await self._send_digest(now, result)
        await self._send_alerts(now, result)
        return result

    async def _send_alerts(self, now: datetime, result: DispatchStats) -> None:
        """Новые сигналы: анти-спам, суточный лимит, приоритетные сверх лимита."""
        day_start, day_end = self._day_bounds(now)
        sent_today = await self._repo.delivery_count(ALERT_KIND, day_start, day_end)
        rows = await self._repo.undelivered_signals(self._pair_key, self._prompt_version)
        for row in rows:
            view = self._view(row)
            if await self._is_antispam(view.ticker, now):
                await self._record(view, SUPPRESSED_KIND, now)
                result.suppressed += 1
                continue
            priority = is_priority(_consensus_row(row), self._signal)
            if not priority and sent_today >= self._cfg.daily_alert_limit:
                # Сверх лимита уходят только приоритетные; остальные ждут дайджеста.
                result.queued += 1
                continue
            accuracy = await self._accuracy(view.ticker)
            text = format_alert(view, accuracy, self._cfg, priority=priority)
            sent, errors = await self._broadcast(text)
            if not sent:
                result.errors += 1
                continue
            await self._record(view, ALERT_KIND, now, sent=sent, errors=errors)
            result.alerts += 1
            if not priority:
                sent_today += 1

    async def _send_digest(self, now: datetime, result: DispatchStats) -> None:
        """Утренний дайджест: первый цикл ACTIVE за сутки МСК, сигналы не старше 24 ч."""
        today = now.astimezone(self._tz).date().isoformat()
        if await self._repo.kv_get(DIGEST_KEY) == today:
            return
        since = now - DIGEST_WINDOW
        rows = await self._repo.undelivered_signals(self._pair_key, self._prompt_version, since)
        if not rows:
            return
        views = [self._view(row) for row in rows]
        counts: dict[str, int] = {}
        accuracies: dict[str, tuple[float | None, int]] = {}
        for view in views:
            counts[view.ticker] = counts.get(view.ticker, 0) + 1
            if view.ticker not in accuracies:
                accuracies[view.ticker] = await self._accuracy(view.ticker)
        delivered = 0
        for index, text in enumerate(format_digest(views, accuracies, counts, self._cfg)):
            if index:
                await self._sleep(self._cfg.send_pause_sec)
            sent, _errors = await self._broadcast(text)
            if sent:
                delivered += 1
            else:
                result.errors += 1
        if not delivered:
            return
        for view in views:
            await self._record(view, DIGEST_KIND, now)
        await self._repo.kv_set(DIGEST_KEY, today)
        result.digest_messages += delivered

    async def _broadcast(self, text: str) -> tuple[list[int], list[str]]:
        """Отправить сообщение всем получателям с паузой; вернуть успешных и ошибки."""
        sent: list[int] = []
        errors: list[str] = []
        for index, chat_id in enumerate(self._chat_ids):
            if index:
                await self._sleep(self._cfg.send_pause_sec)
            if await self._notifier.send(chat_id, text):
                sent.append(chat_id)
            else:
                errors.append(f"{chat_id}: доставка не удалась")
        return sent, errors

    async def _is_antispam(self, ticker: str, now: datetime) -> bool:
        """True, если по тикеру уже был алерт или дайджест внутри окна анти-спама."""
        last = await self._repo.last_delivery_at(ticker, ANTISPAM_KINDS)
        if last is None:
            return False
        return datetime.fromisoformat(last) > now - timedelta(hours=self._cfg.antispam_hours)

    async def _record(
        self,
        view: SignalView,
        kind: str,
        now: datetime,
        sent: list[int] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Записать доставку сигнала (кроме --dry-run, где deliveries не пишутся)."""
        if not self._record_deliveries:
            return
        await self._repo.add_delivery(
            view.prediction_id,
            view.ticker,
            kind,
            now,
            chat_id=", ".join(str(item) for item in sent) if sent else None,
            error="; ".join(errors) if errors else None,
        )

    async def _accuracy(self, ticker: str) -> tuple[float | None, int]:
        """Точность consensus по тикеру для текущей пары и версии промта (CONTRACTS §8)."""
        return await self._stats.accuracy(
            CONSENSUS_MODEL,
            ticker,
            pair_key=self._pair_key,
            prompt_version=self._prompt_version,
        )

    def _view(self, row: dict) -> SignalView:
        """Строка БД -> сигнал для форматирования (имя инструмента из реестра)."""
        ticker = str(row["ticker"])
        instrument = self._registry.get(ticker)
        return SignalView(
            prediction_id=int(row["id"]),
            ticker=ticker,
            name=instrument.name if instrument is not None else ticker,
            direction=str(row["direction"]),
            confidence=int(row["confidence"]),
            horizon_days=int(row["horizon_days"]),
            expected_change_pct=float(row["expected_change_pct"]),
            reason=str(row["reason"]),
            sources_count=int(row["sources_count"] or 1),
            severity=int(row["severity"]),
            url=row["url"],
            title=row["title"],
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _day_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        """Границы текущих суток МСК (для суточного лимита алертов)."""
        local_day = now.astimezone(self._tz).date()
        start = datetime.combine(local_day, time.min, tzinfo=self._tz)
        return start, start + timedelta(days=1)

