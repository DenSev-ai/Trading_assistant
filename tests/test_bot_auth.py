"""Тесты авторизации бота и команд (SPEC §7, STEP 5)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_assistant.config import load_settings
from trading_assistant.db import Database
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.repository import Repository
from trading_assistant.stats import Stats
from trading_assistant.telegram.bot import TradingBot, is_allowed
from trading_assistant.telegram.dispatcher import Dispatcher

MSK = ZoneInfo("Europe/Moscow")
PAIR_KEY = "anthropic/claude-sonnet-5.5+x-ai/grok-4.7"
PROMPT_VERSION = "v2"
CHAT_IDS = [101, 202]
FOREIGN_CHAT = 999
TOKEN = "42:TEST"
# 10:00 МСК — активная фаза расписания.
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)


@dataclass
class FakeChat:
    """Двойник aiogram Chat: нужен только id."""

    id: int


class FakeMessage:
    """Двойник aiogram Message: помнит ответы бота."""

    def __init__(self, chat_id: int, text: str) -> None:
        self.chat = FakeChat(chat_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        """Запомнить ответ бота."""
        self.answers.append(text)


class FakeNotifier:
    """Отправка-заглушка: диспетчеру нужен любой Notifier."""

    async def send(self, chat_id: int, text: str) -> bool:
        """Считать сообщение доставленным."""
        return True


class FakeSleep:
    """Подмена asyncio.sleep без ожидания."""

    async def __call__(self, seconds: float) -> None:
        """Ничего не делать."""


def _bot(db: Database, registry: InstrumentRegistry) -> TradingBot:
    """Бот с разрешёнными чатами 101 и 202 на пустой БД."""
    settings = load_settings(Path("config/settings.yaml"))
    repo = Repository(db)
    stats = Stats(repo, settings.signal, settings.evaluation, lambda: NOW)
    dispatcher = Dispatcher(
        repo,
        stats,
        FakeNotifier(),
        CHAT_IDS,
        settings.telegram,
        registry,
        FakeSleep(),
        tz=MSK,
        signal=settings.signal,
        pair_key=PAIR_KEY,
        prompt_version=PROMPT_VERSION,
    )
    return TradingBot(
        dispatcher,
        repo,
        stats,
        CHAT_IDS,
        settings=settings,
        tz=MSK,
        now_fn=lambda: NOW,
        token=TOKEN,
    )


def test_is_allowed_filter() -> None:
    """Разрешены только chat_id из списка получателей."""
    assert is_allowed(101, CHAT_IDS)
    assert not is_allowed(FOREIGN_CHAT, CHAT_IDS)


async def test_foreign_chat_gets_no_answer(db, registry) -> None:
    """Сообщение из чужого чата игнорируется без ответа."""
    bot = _bot(db, registry)
    message = FakeMessage(FOREIGN_CHAT, "/help")
    await bot.cmd_help(message)
    await bot.cmd_status(message)
    assert message.answers == []


async def test_allowed_chat_gets_help(db, registry) -> None:
    """Из разрешённого чата /help отвечает списком команд."""
    bot = _bot(db, registry)
    message = FakeMessage(101, "/help")
    await bot.cmd_help(message)
    assert len(message.answers) == 1
    assert "/stats" in message.answers[0]
    assert "/status" in message.answers[0]


async def test_router_registers_all_commands(db, registry) -> None:
    """Все пять команд зарегистрированы в роутере aiogram."""
    bot = _bot(db, registry)
    names = {handler.callback.__name__ for handler in bot.router.message.handlers}
    assert names == {"cmd_start", "cmd_help", "cmd_stats", "cmd_last", "cmd_status"}


async def test_start_command_replies(db, registry) -> None:
    """Разрешённый чат на /start получает приветствие."""
    bot = _bot(db, registry)
    message = FakeMessage(202, "/start")
    await bot.cmd_start(message)
    assert message.answers and "Trading assistant" in message.answers[0]


async def test_stats_without_data(db, registry) -> None:
    """Без проверенных прогнозов /stats честно пишет, что статистики мало."""
    bot = _bot(db, registry)
    message = FakeMessage(101, "/stats SBER")
    await bot.cmd_stats(message)
    assert "Статистики пока мало" in message.answers[0]

    common = FakeMessage(101, "/stats")
    await bot.cmd_stats(common)
    assert "Статистики пока мало" in common.answers[0]


async def test_last_without_signals(db, registry) -> None:
    """Без отправленных прогнозов /last так и сообщает."""
    bot = _bot(db, registry)
    message = FakeMessage(101, "/last")
    await bot.cmd_last(message)
    assert message.answers == ["Пока нет отправленных прогнозов."]


async def test_status_reports_phase_and_queue(db, registry) -> None:
    """В 10:00 МСК /status показывает активную фазу и пустую очередь."""
    bot = _bot(db, registry)
    message = FakeMessage(101, "/status")
    await bot.cmd_status(message)
    assert "Фаза: active" in message.answers[0]
    assert "Кластеров в очереди: 0" in message.answers[0]
    assert "Расход за сутки" in message.answers[0]
