"""Конвейер сбора новостей (шаг 2)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .config import AppSettings, Keywords, NewsSource
from .news.clustering import find_cluster
from .news.fulltext import RobotsCache, get_fulltext
from .news.prefilter import normalize_title, passes_prefilter
from .news.sources import RawItem, fetch_feed, parse_feed
from .repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class CollectDeps:
    client: httpx.AsyncClient
    repository: Repository
    settings: AppSettings
    keywords: Keywords


@dataclass
class CollectStats:
    fetched: int = 0
    duplicates: int = 0
    filtered: int = 0
    no_fulltext: int = 0
    clustered: int = 0
    new_clusters: int = 0


async def collect_stage(deps: CollectDeps, now: datetime) -> CollectStats:
    """Один этап сбора новостей для всех включённых источников.

    Загрузка лент — параллельно (asyncio.gather), обработка записей —
    последовательно, чтобы кластеризация была детерминирована.
    """
    stats = CollectStats()
    enabled = [s for s in deps.settings.news.sources if s.enabled]

    # Этап 1: параллельно загружаем и парсим все ленты.
    async def fetch_and_parse(src: NewsSource) -> list[RawItem]:
        xml = await fetch_feed(deps.client, src)
        if xml is None:
            return []
        return await asyncio.to_thread(
            parse_feed,
            src.name,
            xml,
            now,
            deps.settings.news.window_hours,
            deps.settings.news.max_items_per_feed,
        )

    results = await asyncio.gather(
        *(fetch_and_parse(s) for s in enabled), return_exceptions=True
    )
    source_items: list[tuple[NewsSource, list[RawItem]]] = []
    for src, result in zip(enabled, results, strict=False):
        if isinstance(result, Exception):
            logger.warning("источник %s упал: %s", src.name, result)
            continue
        stats.fetched += len(result)
        source_items.append((src, result))

    # Этап 2: обрабатываем записи последовательно (детерминированная кластеризация).
    robots = RobotsCache(deps.client, deps.settings.news.user_agent)
    for src, items in source_items:
        for item in items:
            if await deps.repository.article_exists(item.url):
                stats.duplicates += 1
                continue
            if not passes_prefilter(item.title, item.summary, deps.keywords):
                await deps.repository.add_article(
                    item, normalize_title(item.title), "filtered", None, now
                )
                stats.filtered += 1
                continue
            fulltext = await get_fulltext(
                deps.client, robots, item, src, deps.settings.news.max_fulltext_bytes
            )
            if fulltext is None or len(fulltext) < deps.settings.news.min_fulltext_chars:
                await deps.repository.add_article(
                    item, normalize_title(item.title), "no_fulltext", fulltext, now
                )
                stats.no_fulltext += 1
                continue
            title_norm = normalize_title(item.title)
            window_start = now - timedelta(hours=deps.settings.news.cluster_window_hours)
            candidates = await deps.repository.recent_clusters(window_start)
            cluster_id = find_cluster(
                title_norm,
                [(r["id"], r["title_norm"]) for r in candidates],
                deps.settings.news.cluster_similarity,
            )
            article_id = await deps.repository.add_article(
                item, title_norm, "clustered", fulltext, now
            )
            if cluster_id is None:
                await deps.repository.create_cluster(article_id, item.title, title_norm, now)
                stats.new_clusters += 1
            else:
                await deps.repository.attach_to_cluster(article_id, cluster_id, src.name, now)
            stats.clustered += 1

    return stats

