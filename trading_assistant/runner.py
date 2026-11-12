"""Главный цикл сервиса и корректное завершение фоновых задач."""
from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .clock import Clock, Phase, get_phase, is_working_day, to_iso
from .config import AppSettings
from .db import Database
from .platform_win import allow_sleep, prevent_sleep
from .repository import Repository

logger = logging.getLogger(__name__)
Stage = Callable[[datetime], Awaitable[object]]
Sleep = Callable[[float], Awaitable[None]]


class BackgroundBot(Protocol):
    """Интерфейс фонового опроса и закрытия Telegram-бота."""

    async def run_polling(self) -> None: ...
    async def close(self) -> None: ...


@dataclass
class Deps:
    """Зависимости цикла и ресурсы, которыми он владеет."""

    db: Database
    repository: Repository
    settings: AppSettings
    tz: ZoneInfo
    data_dir: Path
    collect: Stage
    analyze: Stage
    base_price: Stage
    backtest: Stage
    dispatch: Stage
    reports: Stage
    health: Stage
    bot: BackgroundBot | None = None
    http: httpx.AsyncClient | None = None
    close_notifier: Callable[[], Awaitable[None]] | None = None


@dataclass
class CycleReport:
    """Результаты этапов и имена этапов с ошибками."""

    started_at: datetime
    results: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Runner:
    """Выполнить циклы по расписанию и освободить ресурсы при остановке."""

    def __init__(
        self, deps: Deps, clock: Clock, sleep: Sleep = asyncio.sleep,
        once: bool = False, force: bool = False,
    ) -> None:
        self.deps = deps
        self.clock = clock
        self.sleep = sleep
        self.once = once
        self.force = force
        self.last_report: CycleReport | None = None
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def request_stop(self) -> None:
        """Запросить остановку после текущего этапа цикла или во время ожидания."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        else:
            self._stop.set()

    async def run_cycle(self, now: datetime) -> CycleReport:
        """Выполнить все этапы по порядку, независимо от ошибок отдельных этапов."""
        report = CycleReport(now)
        stages = ("collect", "analyze", "base_price", "backtest", "dispatch", "reports", "health")
        for name in stages:
            try:
                report.results[name] = await getattr(self.deps, name)(now)
            except Exception:
                logger.exception("Ошибка этапа %s", name)
                report.errors.append(name)
        await self.deps.repository.kv_set("last_cycle_ok", to_iso(self.clock.now()))
        return report

    def _stop_flag(self) -> bool:
        """Удалить найденный флаг остановки и сообщить о нём."""
        try:
            (self.deps.data_dir / "stop.flag").unlink()
            return True
        except FileNotFoundError:
            return False

    async def _wait(self) -> None:
        """Ожидать интервал или немедленно проснуться по запросу остановки."""
        sleeper = asyncio.ensure_future(self.sleep(self.deps.settings.polling.cycle_interval_sec))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait((sleeper, stopper), return_when=asyncio.FIRST_COMPLETED)
            if sleeper.done():
                await sleeper
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    async def run(self) -> int:
        """Запустить сервис и закрыть ресурсы даже при ошибках или остановке."""
        self._loop = asyncio.get_running_loop()
        handlers: dict[signal.Signals, object] = {}
        bot_task: asyncio.Task | None = None
        try:
            prevent_sleep()
            now = self.clock.now()
            schedule = self.deps.settings.schedule
            phase = get_phase(now, schedule, self.deps.tz)
            if not self.once and not self.force and not is_working_day(now, schedule, self.deps.tz):
                logger.info("Нерабочий день: сервис не запускается")
                return 0
            if not self.once and not self.force and phase is Phase.STOPPED:
                logger.info("Время работы сервиса закончилось")
                return 0
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
                    if sig is not None:
                        handlers[sig] = signal.getsignal(sig)
                        signal.signal(sig, lambda *_: self.request_stop())
            await self.deps.db.init()
            await self.deps.db.cleanup(now)
            if self.deps.bot is not None:
                bot_task = asyncio.create_task(self.deps.bot.run_polling())
            while not self._stop.is_set():
                now = self.clock.now()
                if not self.once and get_phase(now, schedule, self.deps.tz) is Phase.STOPPED:
                    logger.info("Время работы сервиса закончилось")
                    break
                self.last_report = await self.run_cycle(now)
                if self._stop_flag() or self.once or self._stop.is_set():
                    break
                await self._wait()
                if self._stop_flag():
                    break
            return 0
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            if bot_task is not None:
                if not bot_task.done():
                    bot_task.cancel()
                result = await asyncio.gather(bot_task, return_exceptions=True)
                if isinstance(result[0], Exception):
                    logger.error("Ошибка фонового Telegram-бота: %s", type(result[0]).__name__)
            for close in (
                self.deps.bot.close if self.deps.bot else None,
                self.deps.close_notifier,
                self.deps.http.aclose if self.deps.http else None,
                self.deps.db.close,
            ):
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        logger.exception("Ошибка закрытия ресурса")
            allow_sleep()
            self._loop = None
