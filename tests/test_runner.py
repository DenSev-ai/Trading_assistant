"""Тесты главного цикла Runner и режимов __main__ (STEP 6)."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trading_assistant import __main__ as main_module
from trading_assistant import runner as runner_module
from trading_assistant.clock import FakeClock, to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.repository import Repository
from trading_assistant.runner import Deps, Runner

MSK = ZoneInfo("Europe/Moscow")
FRIDAY_NOON = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)  # 12:00 МСК, рабочий день
FRIDAY_EVENING = datetime(2026, 11, 6, 19, 41, tzinfo=UTC)  # 22:41 МСК, после stop_at
FRIDAY_BEFORE_STOP = datetime(2026, 11, 6, 19, 35, tzinfo=UTC)  # 22:35 МСК
SATURDAY = datetime(2026, 11, 7, 7, 0, tzinfo=UTC)  # 10:00 МСК, выходной
STAGES = ["collect", "analyze", "base_price", "backtest", "dispatch", "reports", "health"]
CYCLE_INTERVAL_SEC = 1
STOP_MINUTES_LATER = 6


def _settings() -> AppSettings:
    """Настройки из config/settings.yaml с коротким интервалом цикла."""
    cfg = load_settings(Path("config/settings.yaml"))
    cfg.polling.cycle_interval_sec = CYCLE_INTERVAL_SEC
    return cfg


async def _stage(name: str, calls: list[str], fail: str | None, now: datetime) -> str:
    """Заглушка этапа: пишет своё имя в журнал и может бросить исключение."""
    calls.append(name)
    if name == fail:
        raise RuntimeError(f"сбой этапа {name}")
    return name


def _deps(
    tmp_path: Path,
    calls: list[str],
    *,
    fail: str | None = None,
    bot: object | None = None,
    db: Database | None = None,
) -> Deps:
    """Deps на :memory: БД с заглушками этапов; БД инициализирует сам Runner."""
    database = db if db is not None else Database(":memory:")
    return Deps(
        db=database,
        repository=Repository(database),
        settings=_settings(),
        tz=MSK,
        data_dir=tmp_path,
        collect=partial(_stage, "collect", calls, fail),
        analyze=partial(_stage, "analyze", calls, fail),
        base_price=partial(_stage, "base_price", calls, fail),
        backtest=partial(_stage, "backtest", calls, fail),
        dispatch=partial(_stage, "dispatch", calls, fail),
        reports=partial(_stage, "reports", calls, fail),
        health=partial(_stage, "health", calls, fail),
        bot=bot,
    )


async def _kv(path: Path, key: str) -> str | None:
    """Прочитать ключ kv из файловой БД после завершения цикла."""
    db = Database(str(path))
    await db.init()
    try:
        return await Repository(db).kv_get(key)
    finally:
        await db.close()


class _Bot:
    """Заглушка фонового бота: вечный опрос и учёт закрытий."""

    def __init__(self) -> None:
        self.closed = 0

    async def run_polling(self) -> None:
        """Ожидать отмены задачи."""
        await asyncio.Event().wait()

    async def close(self) -> None:
        """Запомнить закрытие сессии."""
        self.closed += 1


async def test_weekend_exits_without_stages(tmp_path: Path) -> None:
    """В субботу без --force сервис завершается, этапы не вызываются."""
    calls: list[str] = []
    code = await Runner(_deps(tmp_path, calls), FakeClock(SATURDAY)).run()
    assert code == 0
    assert calls == []


async def test_after_stop_time_exits_without_stages(tmp_path: Path) -> None:
    """В 22:41 МСК без --force сервис завершается, этапы не вызываются."""
    calls: list[str] = []
    code = await Runner(_deps(tmp_path, calls), FakeClock(FRIDAY_EVENING)).run()
    assert code == 0
    assert calls == []


async def test_once_runs_single_cycle_in_order(tmp_path: Path) -> None:
    """--once в рабочий день: ровно один цикл в порядке конвейера."""
    calls: list[str] = []
    db_path = tmp_path / "trading.db"
    deps = _deps(tmp_path, calls, db=Database(str(db_path)))
    code = await Runner(deps, FakeClock(FRIDAY_NOON), once=True).run()
    assert code == 0
    assert calls == STAGES
    assert await _kv(db_path, "last_cycle_ok") == to_iso(FRIDAY_NOON)


async def test_stage_error_does_not_stop_cycle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка этапа analyze не мешает остальным этапам и записи last_cycle_ok."""
    calls: list[str] = []
    db_path = tmp_path / "trading.db"
    deps = _deps(tmp_path, calls, fail="analyze", db=Database(str(db_path)))
    with caplog.at_level(logging.ERROR):
        code = await Runner(deps, FakeClock(FRIDAY_NOON), once=True).run()
    assert code == 0
    assert calls == STAGES
    assert "analyze" in caplog.text
    assert await _kv(db_path, "last_cycle_ok") == to_iso(FRIDAY_NOON)


async def test_stop_time_during_sleep_ends_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переход через 22:40 во время паузы завершает сервис и снимает запрет сна."""
    calls: list[str] = []
    events: list[str] = []
    monkeypatch.setattr(runner_module, "prevent_sleep", lambda: events.append("prevent_sleep"))
    monkeypatch.setattr(runner_module, "allow_sleep", lambda: events.append("allow_sleep"))
    clock = FakeClock(FRIDAY_BEFORE_STOP)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(minutes=STOP_MINUTES_LATER)

    code = await Runner(_deps(tmp_path, calls), clock, sleep=sleep).run()
    assert code == 0
    assert calls == STAGES
    assert sleeps == [CYCLE_INTERVAL_SEC]
    assert events == ["prevent_sleep", "allow_sleep"]


async def test_stop_flag_ends_after_cycle(tmp_path: Path) -> None:
    """Файл data/stop.flag даёт завершиться после текущего цикла и удаляется."""
    flag = tmp_path / "stop.flag"
    flag.write_text("", encoding="utf-8")
    calls: list[str] = []
    code = await Runner(_deps(tmp_path, calls), FakeClock(FRIDAY_NOON)).run()
    assert code == 0
    assert calls == STAGES
    assert not flag.exists()


async def test_request_stop_during_sleep(tmp_path: Path) -> None:
    """request_stop() во время паузы завершает сервис без следующего цикла."""
    calls: list[str] = []

    async def sleep(seconds: float) -> None:
        runner.request_stop()

    runner = Runner(_deps(tmp_path, calls), FakeClock(FRIDAY_NOON), sleep=sleep)
    code = await runner.run()
    assert code == 0
    assert calls == STAGES


async def test_forced_once_ignores_weekend(tmp_path: Path) -> None:
    """--force в выходной день всё равно выполняет один цикл."""
    calls: list[str] = []
    code = await Runner(_deps(tmp_path, calls), FakeClock(SATURDAY), once=True, force=True).run()
    assert code == 0
    assert calls == STAGES


async def test_bot_task_is_cancelled_and_closed(tmp_path: Path) -> None:
    """Фоновый бот запускается, отменяется при остановке и закрывается."""
    calls: list[str] = []
    bot = _Bot()
    code = await Runner(_deps(tmp_path, calls, bot=bot), FakeClock(FRIDAY_NOON), once=True).run()
    assert code == 0
    assert bot.closed == 1
    assert calls == STAGES


def test_check_config_without_key_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--check-config возвращает 1, если OPENROUTER_API_KEY отсутствует."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert main_module.main(["--check-config"]) == 1


def test_report_does_not_need_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--report читает только БД и печатает отчёт без сетевых запросов."""
    cfg = load_settings(Path("config/settings.yaml"))
    cfg.data_dir = str(tmp_path)
    monkeypatch.setattr(main_module, "load_settings", lambda: cfg)
    assert main_module.main(["--report"]) == 0
    assert "Состояние проекта" in capsys.readouterr().out
