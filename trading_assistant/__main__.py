"""Точка входа: `python -m trading_assistant --check-config` / `--once`."""
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
from .reports.mailer import file_mailer, make_mailer, reports_dir
from .reports.reports import DAILY, WEEKLY, ReportService
from .repository import Repository
from .stats import Stats
from .telegram.dispatcher import BotNotifier, Dispatcher, LogNotifier, Notifier

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
        help="один цикл (сбор, анализ, базовые цены, проверка) и выход (печатает счётчики)",
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

    if args.once:
        return _run_once(dry_run=args.dry_run)

    parser.print_help()
    return 0


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


def _run_once(dry_run: bool) -> int:
    """Один цикл сбора, анализа, проверки и доставки с печатью счётчиков."""
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
    notifier = _notifier(env, chat_ids, dry_run)

    async def run() -> int:
        await db.init()
        repo = Repository(db)
        now = SystemClock().now()
        stats = Stats(repo, settings.signal, settings.evaluation, lambda: now)
        evaluator = Evaluator(repo, stats, settings, lambda: now)
        service = _report_service(settings, env, repo, stats, evaluator, lambda: now)
        async with httpx.AsyncClient(
            timeout=settings.polling.http_timeout_sec,
            headers={"User-Agent": settings.news.user_agent},
            follow_redirects=True,
        ) as http:
            collect_deps = CollectDeps(
                client=http, repository=repo, settings=settings, keywords=keywords
            )
            collected = await collect_stage(collect_deps, now)
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
            analyzed = await analyze_stage(analyze_deps, now)
            price_deps = PriceDeps(
                moex=MoexClient(http),
                repository=repo,
                settings=settings,
                registry=registry,
                tz=tz,
                reports=service,
            )
            priced = await base_price_stage(price_deps, now)
            checked = await backtest_stage(price_deps, now)
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
            dispatched = await dispatch_stage(DispatchDeps(dispatcher, settings, tz), now)
            sent = await reports_stage(ReportsDeps(service=service), now)
        if isinstance(notifier, BotNotifier):
            await notifier.close()
        await db.close()
        print(
            f"собрано={collected.fetched}, дубли={collected.duplicates}, "
            f"отфильтровано={collected.filtered}, без полного текста={collected.no_fulltext}, "
            f"в кластеры={collected.clustered}, новые кластеры={collected.new_clusters}"
        )
        print(
            f"проанализировано={analyzed.analyzed}, сбоев={analyzed.failed}, "
            f"выданных прогнозов={analyzed.issued_rows}, консенсус={analyzed.consensus_rows}, "
            f"отложено из-за бюджета={analyzed.skipped_budget}, "
            f"расход=${analyzed.cost_usd:.4f}"
        )
        print(
            f"базовые цены={priced.priced} из {priced.checked}, "
            f"проверено прогнозов={checked.evaluated} из {checked.checked}, "
            f"ждут срока={checked.waiting}, пропуск по интервалу={checked.skipped}"
        )
        print(
            f"доставка: алертов={dispatched.alerts}, "
            f"подавлено анти-спамом={dispatched.suppressed}, "
            f"в дайджест={dispatched.queued}, сообщений дайджеста={dispatched.digest_messages}, "
            f"ошибок доставки={dispatched.errors}"
        )
        print(f"отчёты: {', '.join(sent) if sent else 'нет'}")
        if dry_run:
            print("режим --dry-run: сообщения в лог, deliveries не пишутся")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
