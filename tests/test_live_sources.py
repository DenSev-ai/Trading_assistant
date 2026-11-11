"""Живые тесты источников: реальные RSS-запросы (маркер live, по умолчанию пропускаются)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from trading_assistant.clock import SystemClock
from trading_assistant.config import load_settings
from trading_assistant.news.sources import fetch_feed, parse_feed

pytestmark = pytest.mark.live


@pytest.mark.parametrize(
    "source_name",
    [s.name for s in load_settings(Path("config/settings.yaml")).news.sources if s.enabled],
)
async def test_live_source_returns_items(source_name: str) -> None:
    settings = load_settings(Path("config/settings.yaml"))
    src = next(s for s in settings.news.sources if s.name == source_name)
    async with httpx.AsyncClient(
        timeout=settings.polling.http_timeout_sec,
        headers={"User-Agent": settings.news.user_agent},
        follow_redirects=True,
    ) as client:
        xml = await fetch_feed(client, src)
    assert xml, f"пустой ответ от {source_name}"
    items = parse_feed(src.name, xml, SystemClock().now(), window_hours=72, limit=50)
    assert len(items) > 0, f"нет записей у {source_name}"
