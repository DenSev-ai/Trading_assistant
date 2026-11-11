"""Тесты sources.py: parse_feed и fetch_feed."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from trading_assistant.news.sources import fetch_feed, parse_feed

FIXTURES = "tests/fixtures"


def _now() -> datetime:
    return datetime(2026, 11, 6, 15, 0, 0, tzinfo=UTC)


def test_parse_feed_filters_old_and_missing_dates() -> None:
    xml = open(f"{FIXTURES}/rss_sample.xml", "rb").read()
    items = parse_feed("test", xml, _now(), window_hours=24, limit=10)
    # 1 недавняя (06.11 12:00 МСК = 09:00 UTC, внутри 24ч окна),
    # 1 старая (30.10), 1 без даты -> только 1
    assert len(items) == 1
    assert items[0].title == "ЦБ повысил ключевую ставку до 17%"


def test_parse_feed_limit() -> None:
    xml = open(f"{FIXTURES}/rss_sample.xml", "rb").read()
    items = parse_feed("test", xml, _now(), window_hours=24, limit=1)
    assert len(items) <= 1


def test_parse_feed_moex_content_html() -> None:
    xml = open(f"{FIXTURES}/rss_moex.xml", "rb").read()
    items = parse_feed("moex", xml, _now(), window_hours=24, limit=10)
    assert len(items) == 1
    assert items[0].content_html is not None
    assert "Московская биржа" in items[0].content_html


class _Src:
    def __init__(self, url: str) -> None:
        self.url = url
        self.name = "test"
        self.fulltext = "rss"
        self.enabled = True


@pytest.mark.asyncio
async def test_fetch_feed_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = b"<rss><channel><item><title>x</title></item></channel></rss>"
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    src = _Src("https://example.com/rss")

    result = await fetch_feed(client, src)  # type: ignore[arg-type]
    assert result is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_feed_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    src = _Src("https://example.com/rss")

    result = await fetch_feed(client, src)  # type: ignore[arg-type]
    assert result is None
    await client.aclose()

