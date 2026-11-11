"""Получение и парсинг RSS-источников новостей (CONTRACTS §2)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import feedparser
import httpx

from ..config import NewsSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawItem:
    source: str
    url: str
    title: str
    summary: str
    published_at: datetime  # aware, UTC
    content_html: str | None


def _entry_published(entry) -> datetime | None:
    """Время публикации записи в aware UTC, или None если даты нет."""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def parse_feed(
    source: str, xml_bytes: bytes, now: datetime, window_hours: int, limit: int
) -> list[RawItem]:
    """Распарсить RSS-ленту: отфильтровать по окну, отсортировать по дате, ограничить limit."""
    parsed = feedparser.parse(xml_bytes)
    cutoff = now - timedelta(hours=window_hours)
    items: list[RawItem] = []
    for entry in parsed.entries:
        pub = _entry_published(entry)
        if pub is None:
            continue
        if pub < cutoff:
            continue
        content_html: str | None = None
        content = getattr(entry, "content", None)
        if isinstance(content, list) and content:
            content_html = content[0].get("value")
        items.append(
            RawItem(
                source=source,
                url=getattr(entry, "link", None) or getattr(entry, "id", "") or "",
                title=getattr(entry, "title", "") or "",
                summary=getattr(entry, "summary", "") or "",
                published_at=pub,
                content_html=content_html,
            )
        )
    items.sort(key=lambda item: item.published_at, reverse=True)
    return items[:limit]


async def fetch_feed(client: httpx.AsyncClient, src: NewsSource) -> bytes | None:
    """Скачать RSS-ленту. Ошибка или не 200 -> лог и None."""
    try:
        resp = await client.get(src.url)
    except httpx.RequestError as exc:
        logger.warning("не удалось загрузить %s: %s", src.url, exc)
        return None
    if resp.status_code != 200:
        logger.warning("%s вернул %s", src.url, resp.status_code)
        return None
    return resp.content
