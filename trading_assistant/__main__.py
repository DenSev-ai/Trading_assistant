"""Точка входа сервиса: длительная работа, `--once`, `--check-config`, `--report`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .clock import SystemClock
from .config import AppSettings, EnvSettings, env_checklist, load_keywords, load_settings
from .db import Database
from .evaluation import Evaluator
from .instruments import InstrumentRegistry
from .llm.client import OpenRouterClient
from .llm.consensus import make_pair_key
from .logging_setup import setup_logging
from .market.moex import MoexClient
from .pipeline import (
    AnalyzeDeps,
    CollectDeps,
    DispatchDeps,
    PriceDeps,
    ReportsDeps,
    analyze_stage,
    backtest_stage,
    base_price_stage,
    collect_stage,
    dispatch_stage,
    reports_stage,
)
from .platform_win import AlreadyRunning, SingleInstance
from .reports.mailer import file_mailer, make_mailer, reports_dir
from .reports.reports import DAILY, WEEKLY, ReportService
from .repository import Repository
from .runner import CycleReport, Deps, Runner
from .stats import Stats
from .telegram.bot import TradingBot
from .telegram.dispatcher import BotNotifier, Dispatcher, LogNotifier, Notifier
from .telegram.health import HealthMonitor

PROMPT_PATH = Path("prompts/analyze_news.txt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trading_assistant",
        description="Trading assistant — сервис прогнозов российского фондового рынка",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="проверить .env и настройки, распечатать чек-лист",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="один цикл (сбор, анализ, цены, доставка, отчёты) и выход без проверок расписания",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="игнорировать проверку рабочего дня и фазы расписания при старте",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="не отправлять в Telegram: сообщения пишутся в лог, deliveries не ведутся",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="напечатать статистику проекта и моделей (состояние, таблица тестов, вердикт)",
    )
    parser.add_argument(
        "--send-report",
        choices=[DAILY, WEEKLY],
        help="собрать и отправить отчёт daily|weekly прямо сейчас (файл пишется всегда)",
    )
    args = parser.parse_args(argv)

    if args.check_config:
        env = EnvSettings()  # читает .env в текущей папке
        lines, ok = env_checklist(env)
        for line in lines:
            print(line)
        return 0 if ok else 1

    if args.report:
        return _print_report()

    if args.send_report:
        return _send_report(args.send_report)

    return _run_service(once=args.once, force=args.force, dry_run=args.dry_run)


def _print_report() -> int:
    """Напечатать статистику проекта и моделей (--report); пустая БД не ошибка."""
    settings = load_settings()
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir / "trading.db"))
    clock = SystemClock()

    async def run() -> int:
        await db.init()
        try:
            stats = Stats(Repository(db), settings.signal, settings.evaluation, clock.now)
            print(await stats.report(clock.now()))
        finally:
            await db.close()
        return 0

    return asyncio.run(run())


def _chat_ids(raw: str) -> list[int]:
    """Список chat_id из TELEGRAM_CHAT_IDS (через запятую); мусор пропускается с пометкой."""
    ids: list[int] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            print(f"[!] TELEGRAM_CHAT_IDS: значение «{piece}» пропущено (нужен chat_id)")
    return ids


def _notifier(env: EnvSettings, chat_ids: list[int], dry_run: bool) -> Notifier:
    """BotNotifier при настроенном Telegram; без токена, chat_id или в --dry-run — лог."""
    if dry_run or not env.TELEGRAM_BOT_TOKEN or not chat_ids:
        return LogNotifier()
    from aiogram import Bot  # локально: aiogram нужен только в режиме бота

    try:
        return BotNotifier(Bot(token=env.TELEGRAM_BOT_TOKEN))
    except Exception as exc:  # некорректный токен не должен останавливать сервис
        print(f"[!] Telegram-бот не создан ({type(exc).__name__}); сообщения идут в лог")
        return LogNotifier()


def _report_service(
    settings: AppSettings,
    env: EnvSettings,
    repo: Repository,
    stats: Stats,
    evaluator: Evaluator,
    now_fn: Callable[[], datetime],
) -> ReportService:
    """Собрать сервис отчётов: файл в data/reports всегда, письмо — если настроен SMTP."""
    return ReportService(
        repo,
        stats,
        evaluator,
        settings,
        make_mailer(env, settings, now_fn),
        file_mailer(settings, now_fn),
        now_fn,
    )


def _send_report(kind: str) -> int:
    """Собрать и отправить отчёт указанного вида прямо сейчас (--send-report daily|weekly)."""
    settings = load_settings()
    env = EnvSettings()
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir / "trading.db"))
    clock = SystemClock()

    async def run() -> int:
        await db.init()
        try:
            repo = Repository(db)
            stats = Stats(repo, settings.signal, settings.evaluation, clock.now)
            evaluator = Evaluator(repo, stats, settings, clock.now)
            service = _report_service(settings, env, repo, stats, evaluator, clock.now)
            sent = await service.run(clock.now(), force=kind)
        finally:
            await db.close()
        if not sent:
            print("отчёт не отправлен: проверьте reports.enabled в config/settings.yaml")
            return 1
        for name in sent:
            print(f"отчёт {name} сохранён в {reports_dir(settings)}")
        return 0

    return asyncio.run(run())


def _log_level(name: str) -> int:
    """Уровень логирования по имени из конфига; незнакомое имя — INFO."""
    return getattr(logging, name.upper(), logging.INFO)


def _run_service(once: bool, force: bool, dry_run: bool) -> int:
    """Работа сервиса по расписанию; --once — один цикл с печатью счётчиков."""
    settings = load_settings()
    keywords = load_keywords(Path("config/keywords.yaml"))
    registry = InstrumentRegistry.load(Path("config/instruments.json"))
    env = EnvSettings()
    template = PROMPT_PATH.read_text(encoding="utf-8")
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir, _log_level(settings.log_level))
    tz = ZoneInfo(settings.timezone)
    db = Database(str(data_dir / "trading.db"))
    chat_ids = _chat_ids(env.TELEGRAM_CHAT_IDS)
    clock = SystemClock()

    async def run() -> int:
        with SingleInstance(data_dir / "service.lock"):
            repo = Repository(db)
            stats = Stats(repo, settings.signal, settings.evaluation, clock.now)
            evaluator = Evaluator(repo, stats, settings, clock.now)
            service = _report_service(settings, env, repo, stats, evaluator, clock.now)
            notifier = _notifier(env, chat_ids, dry_run)
            dispatcher = Dispatcher(
                repo,
                stats,
                notifier,
                chat_ids,
                settings.telegram,
                registry,
                tz=tz,
                signal=settings.signal,
                pair_key=make_pair_key([model.id for model in settings.llm.pair]),
                prompt_version=settings.llm.prompt_version,
                record_deliveries=not dry_run,
            )
            http = httpx.AsyncClient(
                timeout=settings.polling.http_timeout_sec,
                headers={"User-Agent": settings.news.user_agent},
                follow_redirects=True,
            )
            collect_deps = CollectDeps(
                client=http, repository=repo, settings=settings, keywords=keywords
            )
            llm = OpenRouterClient(
                http, env.OPENROUTER_API_KEY, settings.llm.base_url, settings.llm.timeout_sec
            )
            analyze_deps = AnalyzeDeps(
                client=llm,
                repository=repo,
                settings=settings,
                registry=registry,
                prompt_template=template,
                tz=tz,
                reports=service,
            )
            price_deps = PriceDeps(
                moex=MoexClient(http),
                repository=repo,
                settings=settings,
                registry=registry,
                tz=tz,
                reports=service,
            )
            health = HealthMonitor(repo, notifier, chat_ids, settings, tz)
            bot = _trading_bot(
                dispatcher, repo, stats, settings, tz, chat_ids, env, clock.now, dry_run
            )
            deps = Deps(
                db=db,
                repository=repo,
                settings=settings,
                tz=tz,
                data_dir=data_dir,
                collect=lambda now: collect_stage(collect_deps, now),
                analyze=lambda now: analyze_stage(analyze_deps, now),
                base_price=lambda now: base_price_stage(price_deps, now),
                backtest=lambda now: backtest_stage(price_deps, now),
                dispatch=lambda now: dispatch_stage(DispatchDeps(dispatcher, settings, tz), now),
                reports=lambda now: reports_stage(ReportsDeps(service=service), now),
                health=health.check,
                bot=bot,
                http=http,
                close_notifier=notifier.close if isinstance(notifier, BotNotifier) else None,
            )
            runner = Runner(deps, clock, once=once, force=force)
            code = await runner.run()
            if once:
                _print_counts(runner.last_report)
            return code

    try:
        return asyncio.run(run())
    except AlreadyRunning as exc:
        print(f"[!] {exc}")
        return 1


def _trading_bot(
    dispatcher: Dispatcher,
    repo: Repository,
    stats: Stats,
    settings: AppSettings,
    tz: ZoneInfo,
    chat_ids: list[int],
    env: EnvSettings,
    now_fn: Callable[[], datetime],
    dry_run: bool,
) -> TradingBot | None:
    """Бот команд при настроенном Telegram; без токена, chat_id или в --dry-run — None."""
    if dry_run or not env.TELEGRAM_BOT_TOKEN or not chat_ids:
        return None
    try:
        return TradingBot(
            dispatcher,
            repo,
            stats,
            chat_ids,
            settings=settings,
            tz=tz,
            now_fn=now_fn,
            token=env.TELEGRAM_BOT_TOKEN,
        )
    except Exception as exc:  # некорректный токен не должен останавливать сервис
        print(f"[!] Telegram-бот команд не создан ({type(exc).__name__}); команды недоступны")
        return None


def _value(report: CycleReport, stage: str, name: str) -> object:
    """Поле счётчиков этапа из отчёта цикла; пропущенный этап даёт 0."""
    result = report.results.get(stage)
    return getattr(result, name, 0) if result is not None else 0


def _print_counts(report: CycleReport | None) -> None:
    """Напечатать счётчики одного цикла (--once) и этапы, завершившиеся ошибкой."""
    if report is None:
        print("цикл не выполнялся")
        return
    print(
        f"собрано={_value(report, 'collect', 'fetched')}, "
        f"дубли={_value(report, 'collect', 'duplicates')}, "
        f"отфильтровано={_value(report, 'collect', 'filtered')}, "
        f"без полного текста={_value(report, 'collect', 'no_fulltext')}, "
        f"в кластеры={_value(report, 'collect', 'clustered')}, "
        f"новые кластеры={_value(report, 'collect', 'new_clusters')}"
    )
    print(
        f"проанализировано={_value(report, 'analyze', 'analyzed')}, "
        f"сбоев={_value(report, 'analyze', 'failed')}, "
        f"выданных прогнозов={_value(report, 'analyze', 'issued_rows')}, "
        f"консенсус={_value(report, 'analyze', 'consensus_rows')}, "
        f"отложено из-за бюджета={_value(report, 'analyze', 'skipped_budget')}, "
        f"расход=${float(_value(report, 'analyze', 'cost_usd')):.4f}"
    )
    print(
        f"базовые цены={_value(report, 'base_price', 'priced')} "
        f"из {_value(report, 'base_price', 'checked')}, "
        f"проверено прогнозов={_value(report, 'backtest', 'evaluated')} "
        f"из {_value(report, 'backtest', 'checked')}, "
        f"ждут срока={_value(report, 'backtest', 'waiting')}, "
        f"пропуск по интервалу={_value(report, 'backtest', 'skipped')}"
    )
    print(
        f"доставка: алертов={_value(report, 'dispatch', 'alerts')}, "
        f"подавлено анти-спамом={_value(report, 'dispatch', 'suppressed')}, "
        f"в дайджест={_value(report, 'dispatch', 'queued')}, "
        f"сообщений дайджеста={_value(report, 'dispatch', 'digest_messages')}, "
        f"ошибок доставки={_value(report, 'dispatch', 'errors')}"
    )
    sent = report.results.get("reports")
    names = ", ".join(sent) if isinstance(sent, list) and sent else "нет"
    print(f"отчёты: {names}")
    print(f"этапы с ошибками: {', '.join(report.errors) if report.errors else 'нет'}")


if __name__ == "__main__":
    sys.exit(main())
