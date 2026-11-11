"""Точка входа: `python -m trading_assistant --check-config` / `--once`."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .clock import SystemClock
from .config import EnvSettings, env_checklist, load_keywords, load_settings
from .db import Database
from .pipeline import CollectDeps, collect_stage


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
        help="один цикл сбора новостей и выход (печатает счётчики этапов)",
    )
    args = parser.parse_args(argv)

    if args.check_config:
        env = EnvSettings()  # читает .env в текущей папке
        lines, ok = env_checklist(env)
        for line in lines:
            print(line)
        return 0 if ok else 1

    if args.once:
        return _run_once()

    parser.print_help()
    return 0


def _run_once() -> int:
    """Один цикл сбора новостей с печатью счётчиков."""
    settings = load_settings()
    keywords = load_keywords(Path("config/keywords.yaml"))
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir / "trading.db"))

    async def run() -> int:
        await db.init()
        from .repository import Repository

        repo = Repository(db)
        import httpx

        async with httpx.AsyncClient(
            timeout=settings.polling.http_timeout_sec,
            headers={"User-Agent": settings.news.user_agent},
            follow_redirects=True,
        ) as client:
            deps = CollectDeps(client=client, repository=repo, settings=settings, keywords=keywords)
            stats = await collect_stage(deps, SystemClock().now())
        await db.close()
        print(
            f"собрано={stats.fetched}, дубли={stats.duplicates}, отфильтровано={stats.filtered}, "
            f"без полного текста={stats.no_fulltext}, в кластеры={stats.clustered}, "
            f"новые кластеры={stats.new_clusters}"
        )
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
