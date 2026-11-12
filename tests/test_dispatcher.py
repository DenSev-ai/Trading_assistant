"""Тесты диспетчера доставки: анти-спам, суточный лимит, дайджест (STEP 5)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_assistant.clock import Phase, to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.repository import Repository
from trading_assistant.stats import Stats
from trading_assistant.telegram.dispatcher import (
    ALERT_KIND,
    DIGEST_KEY,
    DIGEST_KIND,
    SUPPRESSED_KIND,
    Dispatcher,
)

MSK = ZoneInfo("Europe/Moscow")
PAIR_KEY = "anthropic/claude-sonnet-5.5+x-ai/grok-4.7"
PROMPT_VERSION = "v2"
CHAT_IDS = [101, 202]
# 10:00 МСК — активная фаза; 09:45 МСК — первый ACTIVE-цикл дня (дайджест); 05:00 МСК — ночь.
ACTIVE = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
DIGEST_TIME = datetime(2026, 11, 11, 6, 45, tzinfo=UTC)
NIGHT = datetime(2026, 11, 11, 2, 0, tzinfo=UTC)


class FakeNotifier:
    """Поддельная отправка: запоминает сообщения и умеет отказывать части чатов."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.messages: list[tuple[int, str]] = []
        self._fail = fail or set()

    async def send(self, chat_id: int, text: str) -> bool:
        """Запомнить сообщение; для чатов из fail вернуть отказ."""
        self.messages.append((chat_id, text))
        return chat_id not in self._fail

    def texts(self) -> list[str]:
        """Тексты всех отправленных сообщений."""
        return [text for _, text in self.messages]


class FakeSleep:
    """Подмена asyncio.sleep: запоминает паузы и не ждёт."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _settings() -> AppSettings:
    """Настройки из config/settings.yaml (пара, версия промта, лимиты telegram)."""
    return load_settings(Path("config/settings.yaml"))


def _dispatcher(
    db: Database,
    registry: InstrumentRegistry,
    notifier: FakeNotifier,
    sleep: FakeSleep | None = None,
    record: bool = True,
) -> Dispatcher:
    """Диспетчер на :memory:-БД с поддельной отправкой и мгновенной паузой."""
    settings = _settings()
    repo = Repository(db)
    stats = Stats(repo, settings.signal, settings.evaluation, lambda: ACTIVE)
    return Dispatcher(
        repo,
        stats,
        notifier,
        CHAT_IDS,
        settings.telegram,
        registry,
        sleep or FakeSleep(),
        tz=MSK,
        signal=settings.signal,
        pair_key=PAIR_KEY,
        prompt_version=PROMPT_VERSION,
        record_deliveries=record,
    )


async def _add_signal(db: Database, created_at: datetime, ticker: str = "SBER", **overrides) -> int:
    """Вставить кластер с главной статьёй и consensus-прогноз."""
    data: dict[str, object] = {
        "direction": "up",
        "confidence": 88,
        "severity": 8,
        "horizon_days": 90,
        "expected_change_pct": 18.0,
        "is_issued": 1,
        "model": "consensus",
        "prompt_version": PROMPT_VERSION,
        "pair_key": PAIR_KEY,
        "sources_count": 3,
    }
    data.update(overrides)
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, sources_count, status) "
        "VALUES ('Новость', 'новость', ?, ?, ?, 'analyzed')",
        (to_iso(created_at), to_iso(created_at), data["sources_count"]),
    )
    article_id = await db.execute(
        "INSERT INTO articles (url, source, title, title_norm, published_at, fetched_at, status, "
        "cluster_id) VALUES (?, 'interfax', 'Новость', 'новость', ?, ?, 'clustered', ?)",
        (f"https://example.com/{cluster_id}", to_iso(created_at), to_iso(created_at), cluster_id),
    )
    await db.execute(
        "UPDATE clusters SET main_article_id = ? WHERE id = ?", (article_id, cluster_id)
    )
    return await db.execute(
        "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
        "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
        "is_issued) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'причина', ?, ?, ?, ?)",
        (
            cluster_id,
            data["model"],
            ticker,
            data["direction"],
            data["confidence"],
            data["severity"],
            data["horizon_days"],
            data["expected_change_pct"],
            to_iso(created_at),
            data["prompt_version"],
            data["pair_key"],
            data["is_issued"],
        ),
    )


async def _kinds(db: Database) -> list[str]:
    """Виды записанных доставок в порядке вставки."""
    rows = await db.fetchall("SELECT kind FROM deliveries ORDER BY id")
    return [str(row["kind"]) for row in rows]


async def _mark_digest_sent(db: Database, moment: datetime) -> None:
    """Отметить, что дайджест за сутки момента уже отправлен."""
    await Repository(db).kv_set(DIGEST_KEY, moment.astimezone(MSK).date().isoformat())


async def test_active_sends_alert_to_every_chat(db, registry) -> None:
    """1) ACTIVE + новый сигнал: по сообщению каждому chat_id и запись deliveries."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    stats = await _dispatcher(db, registry, notifier).dispatch(Phase.ACTIVE, ACTIVE)
    assert [chat for chat, _ in notifier.messages] == CHAT_IDS
    assert "SBER" in notifier.texts()[0]
    assert stats.alerts == 1
    assert await _kinds(db) == [ALERT_KIND]


async def test_repeat_dispatch_sends_nothing(db, registry) -> None:
    """2) Повторный вызов на том же сигнале не отправляет ничего."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier)
    await dispatcher.dispatch(Phase.ACTIVE, ACTIVE)
    sent_before = len(notifier.messages)
    stats = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE)
    assert len(notifier.messages) == sent_before
    assert stats.alerts == 0


async def test_passive_sends_nothing(db, registry) -> None:
    """3) В пассивной фазе сигналы копятся и не отправляются."""
    await _add_signal(db, NIGHT)
    notifier = FakeNotifier()
    stats = await _dispatcher(db, registry, notifier).dispatch(Phase.PASSIVE, NIGHT)
    assert notifier.messages == []
    assert stats.alerts == 0
    assert await _kinds(db) == []


async def test_antispam_blocks_and_expires(db, registry) -> None:
    """4) Второй сигнал по тикеру через 2 ч подавляется, через 13 ч — отправляется."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier)
    await dispatcher.dispatch(Phase.ACTIVE, ACTIVE)

    soon = ACTIVE + timedelta(hours=2)
    await _add_signal(db, soon, expected_change_pct=19.0)
    blocked = await dispatcher.dispatch(Phase.ACTIVE, soon)
    assert blocked.suppressed == 1
    assert len(notifier.messages) == len(CHAT_IDS)
    assert await _kinds(db) == [ALERT_KIND, SUPPRESSED_KIND]

    # 13 ч спустя окно анти-спама (12 ч) уже прошло; фаза передаётся явно.
    later = ACTIVE + timedelta(hours=13)
    await _add_signal(db, later, expected_change_pct=20.0)
    allowed = await dispatcher.dispatch(Phase.ACTIVE, later)
    assert allowed.alerts == 1
    assert await _kinds(db) == [ALERT_KIND, SUPPRESSED_KIND, ALERT_KIND]


async def test_unissued_forecast_not_sent(db, registry) -> None:
    """5) Невыданный прогноз (is_issued=0) не отправляется."""
    await _add_signal(db, ACTIVE, is_issued=0)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    await _dispatcher(db, registry, notifier).dispatch(Phase.ACTIVE, ACTIVE)
    assert notifier.messages == []
    assert await _kinds(db) == []


async def test_daily_limit_and_priority(db, registry) -> None:
    """5а) Шестой обычный сигнал за сутки ждёт дайджеста, приоритетный — уходит."""
    tickers = ["SBER", "LKOH", "GAZP", "ROSN", "VTBR"]
    for index, ticker in enumerate(tickers):
        # Срок 150 дн. — обычный (не приоритетный) алерт: 150 >= priority_fast_days.
        await _add_signal(db, ACTIVE + timedelta(minutes=index), ticker, horizon_days=150)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier)
    first = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE + timedelta(minutes=10))
    assert first.alerts == len(tickers)

    await _add_signal(db, ACTIVE + timedelta(minutes=11), "YDEX", horizon_days=150)
    queued = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE + timedelta(minutes=12))
    assert queued.queued == 1
    assert queued.alerts == 0

    await _add_signal(db, ACTIVE + timedelta(minutes=13), "AFLT", expected_change_pct=28.0)
    priority = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE + timedelta(minutes=14))
    assert priority.alerts == 1
    assert "Приоритетный прогноз" in notifier.texts()[-1]


async def test_priority_does_not_bypass_antispam(db, registry) -> None:
    """5б) Приоритетный сигнал по «свежему» тикеру всё равно подавляется."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier)
    await dispatcher.dispatch(Phase.ACTIVE, ACTIVE)
    await _add_signal(db, ACTIVE + timedelta(hours=2), expected_change_pct=28.0)
    stats = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE + timedelta(hours=2))
    assert stats.suppressed == 1
    assert stats.alerts == 0
    assert await _kinds(db) == [ALERT_KIND, SUPPRESSED_KIND]



async def test_failed_chat_does_not_stop_others(db, registry) -> None:
    """6) Отказ одному получателю: остальные получают сообщение, доставка записана."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier(fail={202})
    stats = await _dispatcher(db, registry, notifier).dispatch(Phase.ACTIVE, ACTIVE)
    assert [chat for chat, _ in notifier.messages] == CHAT_IDS
    assert stats.alerts == 1
    rows = await db.fetchall("SELECT kind, chat_id, error FROM deliveries")
    assert len(rows) == 1
    assert rows[0]["kind"] == ALERT_KIND
    assert rows[0]["chat_id"] == "101"
    assert "202" in str(rows[0]["error"])


async def test_pause_between_recipients(db, registry) -> None:
    """Пауза send_pause_sec соблюдается между сообщениями двум получателям."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    sleep = FakeSleep()
    await _dispatcher(db, registry, FakeNotifier(), sleep).dispatch(Phase.ACTIVE, ACTIVE)
    assert sleep.calls == [_settings().telegram.send_pause_sec]


async def test_digest_at_0945_once_per_day(db, registry) -> None:
    """7) В 09:45 уходит дайджест ночных сигналов; повторно в тот же день — нет."""
    await _add_signal(db, NIGHT, "SBER")
    await _add_signal(db, NIGHT + timedelta(minutes=30), "LKOH")
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier)
    stats = await dispatcher.dispatch(Phase.ACTIVE, DIGEST_TIME)
    assert stats.digest_messages == 1
    assert notifier.texts()[0].startswith("Утренний дайджест")
    assert await _kinds(db) == [DIGEST_KIND, DIGEST_KIND]

    sent_before = len(notifier.messages)
    again = await dispatcher.dispatch(Phase.ACTIVE, DIGEST_TIME + timedelta(minutes=3))
    assert len(notifier.messages) == sent_before
    assert again.digest_messages == 0


async def test_old_signal_not_in_digest(db, registry) -> None:
    """8) Сигнал старше 24 ч в дайджест не попадает (уходит обычным алертом)."""
    await _add_signal(db, DIGEST_TIME - timedelta(hours=30))
    notifier = FakeNotifier()
    await _dispatcher(db, registry, notifier).dispatch(Phase.ACTIVE, DIGEST_TIME)
    assert notifier.texts()
    assert all("Утренний дайджест" not in text for text in notifier.texts())
    assert await _kinds(db) == [ALERT_KIND]


async def test_no_signals_no_digest(db, registry) -> None:
    """9) Нет сигналов — дайджеста нет и метка дня не ставится."""
    notifier = FakeNotifier()
    await _dispatcher(db, registry, notifier).dispatch(Phase.ACTIVE, DIGEST_TIME)
    assert notifier.messages == []
    assert await Repository(db).kv_get(DIGEST_KEY) is None


async def test_dry_run_skips_deliveries(db, registry) -> None:
    """В режиме --dry-run сообщения формируются, но deliveries не пишутся."""
    await _add_signal(db, ACTIVE)
    await _mark_digest_sent(db, ACTIVE)
    notifier = FakeNotifier()
    dispatcher = _dispatcher(db, registry, notifier, record=False)
    stats = await dispatcher.dispatch(Phase.ACTIVE, ACTIVE)
    assert stats.alerts == 1
    assert len(notifier.messages) == len(CHAT_IDS)
    assert await _kinds(db) == []

