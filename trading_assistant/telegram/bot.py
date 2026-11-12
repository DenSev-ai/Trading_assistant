"""Команды Telegram-бота и фильтр получателей (SPEC §7, STEP 5)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram import Dispatcher as AiogramDispatcher
from aiogram.filters import Command

from ..clock import get_phase
from ..config import AppSettings
from ..llm.budget import Budget
from ..repository import Repository
from ..stats import CONSENSUS_MODEL, Stats
from .dispatcher import Dispatcher

logger = logging.getLogger(__name__)

LAST_SIGNALS = 5
TOP_TICKERS = 5
STATUS_KEYS = ("last_cycle_ok", "last_backtest", "last_digest_date")
COMMANDS = ("start", "help", "stats", "last", "status")
UP_WORD = "рост"
DOWN_WORD = "падение"
LOW_STATS_TEXT = "Статистики пока мало: пара или модели недавно менялись."
NO_SIGNALS_TEXT = "Пока нет отправленных прогнозов."
NO_TICKERS_TEXT = "Проверенных прогнозов по тикерам пока нет."
START_TEXT = (
    "Trading assistant — прогнозы по российскому рынку.\n"
    "Сигналы приходят сами; команды: /help"
)
HELP_TEXT = (
    "Доступные команды:\n"
    "/help — список команд\n"
    "/stats [ТИКЕР] — точность прогнозов пары, без тикера — общая и топ-5 тикеров\n"
    "/last — последние 5 отправленных прогнозов\n"
    "/status — фаза, очередь кластеров, расход за сутки и метки циклов"
)


class ChatLike(Protocol):
    """Минимальный интерфейс чата aiogram, нужный обработчикам."""

    id: int


class MessageLike(Protocol):
    """Минимальный интерфейс сообщения aiogram, нужный обработчикам."""

    chat: ChatLike
    text: str | None

    async def answer(self, text: str, **kwargs: object) -> object:
        """Ответить в тот же чат."""
        ...


def is_allowed(chat_id: int, chat_ids: list[int]) -> bool:
    """True, если чат входит в список разрешённых получателей."""
    return chat_id in chat_ids



class TradingBot:
    """Бот на aiogram 3: пять команд, чужие чаты игнорируются без ответа."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        repo: Repository,
        stats: Stats,
        chat_ids: list[int],
        *,
        settings: AppSettings,
        tz: ZoneInfo,
        now_fn: Callable[[], datetime],
        token: str,
    ) -> None:
        """Собрать бота и зарегистрировать команды; token — из TELEGRAM_BOT_TOKEN."""
        self._dispatcher = dispatcher
        self._repo = repo
        self._stats = stats
        self._chat_ids = list(chat_ids)
        self._settings = settings
        self._tz = tz
        self._now_fn = now_fn
        self._bot = Bot(token=token)
        self._router = Router(name="trading_assistant")
        self._register()

    @property
    def bot(self) -> Bot:
        """Экземпляр aiogram Bot (нужен планировщику шага 6)."""
        return self._bot

    @property
    def router(self) -> Router:
        """Роутер с зарегистрированными командами."""
        return self._router

    def _register(self) -> None:
        """Зарегистрировать обработчики команд в роутере."""
        self._router.message.register(self.cmd_start, Command("start"))
        self._router.message.register(self.cmd_help, Command("help"))
        self._router.message.register(self.cmd_stats, Command("stats"))
        self._router.message.register(self.cmd_last, Command("last"))
        self._router.message.register(self.cmd_status, Command("status"))

    async def cmd_start(self, message: MessageLike) -> None:
        """Ответ на /start: приветствие и подсказка про /help."""
        if not self._allowed(message):
            return
        await message.answer(START_TEXT)

    async def cmd_help(self, message: MessageLike) -> None:
        """Ответ на /help: список команд."""
        if not self._allowed(message):
            return
        await message.answer(HELP_TEXT)

    async def cmd_stats(self, message: MessageLike) -> None:
        """Ответ на /stats [ТИКЕР]: точность по тикеру или общая с топ-5 тикеров."""
        if not self._allowed(message):
            return
        ticker = self._argument(message).upper()
        if ticker:
            await message.answer(await self._ticker_stats(ticker))
            return
        await message.answer(await self._common_stats())

    async def cmd_last(self, message: MessageLike) -> None:
        """Ответ на /last: пять последних сигналов consensus текущей пары."""
        if not self._allowed(message):
            return
        rows = await self._repo.last_signals(
            LAST_SIGNALS, self._dispatcher.pair_key, self._dispatcher.prompt_version
        )
        if not rows:
            await message.answer(NO_SIGNALS_TEXT)
            return
        lines = ["Последние прогнозы:", *(self._signal_line(row) for row in rows)]
        await message.answer("\n".join(lines))

    async def cmd_status(self, message: MessageLike) -> None:
        """Ответ на /status: фаза, очередь кластеров, расход за сутки и метки циклов."""
        if not self._allowed(message):
            return
        now = self._now_fn()
        phase = get_phase(now, self._settings.schedule, self._tz)
        pending = await self._repo.pending_cluster_count()
        budget = Budget(self._repo, self._settings.llm.daily_budget_usd, self._tz)
        spent = await budget.spent_today(now)
        lines = [
            f"Фаза: {phase.value}",
            f"Кластеров в очереди: {pending}",
            f"Расход за сутки: ${spent:.4f} из ${self._settings.llm.daily_budget_usd:.4f}",
            "Метки циклов:",
        ]
        for key in STATUS_KEYS:
            value = await self._repo.kv_get(key)
            lines.append(f"{key}: {value or '—'}")
        await message.answer("\n".join(lines))

    async def run_polling(self) -> None:
        """Снять вебхук с drop_pending_updates и начать long-polling без перехвата сигналов."""
        dispatcher = AiogramDispatcher()
        dispatcher.include_router(self._router)
        await self._bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(self._bot, handle_signals=False)

    async def close(self) -> None:
        """Закрыть HTTP-сессию бота при остановке сервиса."""
        await self._bot.session.close()

    def _allowed(self, message: MessageLike) -> bool:
        """Пропустить только сообщения из разрешённых чатов (остальные — без ответа)."""
        if is_allowed(int(message.chat.id), self._chat_ids):
            return True
        logger.debug("сообщение из чужого чата %s проигнорировано", message.chat.id)
        return False

    @staticmethod
    def _argument(message: MessageLike) -> str:
        """Аргумент команды: второе слово в тексте сообщения."""
        parts = (message.text or "").split()
        return parts[1] if len(parts) > 1 else ""

    async def _ticker_stats(self, ticker: str) -> str:
        """Текст статистики по одному тикеру с оговоркой о размере выборки."""
        percent, n = await self._accuracy(ticker)
        if n < self._settings.telegram.stat_min_n:
            return f"Тикер {ticker}\n{LOW_STATS_TEXT}"
        return f"Тикер {ticker}\nСбылось прогнозов: {(percent or 0.0):.0f}% (n={n})."

    async def _common_stats(self) -> str:
        """Общая точность пары и топ-5 тикеров по числу проверенных прогнозов."""
        percent, n = await self._accuracy(None)
        key = self._dispatcher.pair_key
        if n < self._settings.telegram.stat_min_n:
            head = f"Пара {key}\n{LOW_STATS_TEXT}"
        else:
            head = f"Пара {key}\nСбылось прогнозов: {(percent or 0.0):.0f}% (n={n})."
        rows = await self._repo.ticker_success_counts(
            key, self._dispatcher.prompt_version, TOP_TICKERS
        )
        if not rows:
            return f"{head}\n{NO_TICKERS_TEXT}"
        lines = [head, "Топ тикеров:"]
        for row in rows:
            share = 100.0 * int(row["ok"]) / int(row["n"])
            lines.append(f"{row['ticker']}: {share:.0f}% (n={row['n']})")
        return "\n".join(lines)

    async def _accuracy(self, ticker: str | None) -> tuple[float | None, int]:
        """Точность consensus текущей пары и версии промта."""
        return await self._stats.accuracy(
            CONSENSUS_MODEL,
            ticker,
            pair_key=self._dispatcher.pair_key,
            prompt_version=self._dispatcher.prompt_version,
        )

    def _signal_line(self, row: dict) -> str:
        """Строка списка /last: тикер, движение, срок и дата сигнала."""
        word = UP_WORD if row["direction"] == "up" else DOWN_WORD
        moment = datetime.fromisoformat(str(row["created_at"])).astimezone(self._tz)
        return (
            f"{row['ticker']}: {word} не менее {float(row['expected_change_pct']):g}% "
            f"за срок до {int(row['horizon_days'])} дн. ({moment:%d.%m.%Y})"
        )

