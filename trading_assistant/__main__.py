"""Точка входа: `python -m trading_assistant --check-config` / `--once`."""
from __future__ import annotations

import argparse
import asyncio
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
from .market.moex import MoexClient
from .pipeline import (
    AnalyzeDeps,
    CollectDeps,
    PriceDeps,
    ReportsDeps,
    analyze_stage,
    backtest_stage,
    base_price_stage,
    collect_stage,
    reports_stage,
)
from .reports.mailer import file_mailer, make_mailer, reports_dir
from .reports.reports import DAILY, WEEKLY, ReportService
from .repository import Repository
from .stats import Stats

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
        help="не отправлять сообщения в Telegram (на этом шаге отправки нет)",
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


def _run_once(dry_run: bool) -> int:
    """Один цикл сбора новостей и анализа кластеров с печатью счётчиков."""
    settings = load_settings()
    keywords = load_keywords(Path("config/keywords.yaml"))
    registry = InstrumentRegistry.load(Path("config/instruments.json"))
    env = EnvSettings()
    template = PROMPT_PATH.read_text(encoding="utf-8")
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(settings.timezone)
    db = Database(str(data_dir / "trading.db"))

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
            sent = await reports_stage(ReportsDeps(service=service), now)
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
        print(f"отчёты: {', '.join(sent) if sent else 'нет'}")
        if dry_run:
            print("режим --dry-run: отправка сообщений отключена")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
