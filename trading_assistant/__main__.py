"""Точка входа: `python -m trading_assistant --check-config` / `--once`."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .clock import SystemClock
from .config import EnvSettings, env_checklist, load_keywords, load_settings
from .db import Database
from .instruments import InstrumentRegistry
from .llm.client import OpenRouterClient
from .market.moex import MoexClient
from .pipeline import (
    AnalyzeDeps,
    CollectDeps,
    PriceDeps,
    analyze_stage,
    backtest_stage,
    base_price_stage,
    collect_stage,
)
from .repository import Repository

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
    args = parser.parse_args(argv)

    if args.check_config:
        env = EnvSettings()  # читает .env в текущей папке
        lines, ok = env_checklist(env)
        for line in lines:
            print(line)
        return 0 if ok else 1

    if args.once:
        return _run_once(dry_run=args.dry_run)

    parser.print_help()
    return 0


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
            )
            analyzed = await analyze_stage(analyze_deps, now)
            price_deps = PriceDeps(
                moex=MoexClient(http),
                repository=repo,
                settings=settings,
                registry=registry,
                tz=tz,
            )
            priced = await base_price_stage(price_deps, now)
            checked = await backtest_stage(price_deps, now)
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
        if dry_run:
            print("режим --dry-run: отправка сообщений отключена")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
