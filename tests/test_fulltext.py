"""Тесты fulltext.py: html_to_text, RobotsCache, get_fulltext."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_assistant.config import NewsSource
from trading_assistant.news.fulltext import RobotsCache, get_fulltext, html_to_text
from trading_assistant.news.sources import RawItem


def test_html_to_text() -> None:
    html = open("tests/fixtures/article.html", encoding="utf-8").read()
    assert html_to_text(html) == "a\nb & c"


def test_html_to_text_no_paragraphs() -> None:
    assert html_to_text("<div>нет тегов p</div>") == ""


@pytest.mark.asyncio
async def test_robots_cache_disallow() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /doc/\n")
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    robots = RobotsCache(client, "test-agent")
    assert await robots.allowed("http://example.com/doc/1") is False
    assert await robots.allowed("http://example.com/news/1") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_robots_cache_404() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    robots = RobotsCache(client, "test-agent")
    assert await robots.allowed("http://example.com/anything") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_get_fulltext_rss_mode() -> None:
    import httpx

    client = httpx.AsyncClient()
    robots = RobotsCache(client, "test-agent")
    item = RawItem(
        source="test",
        url="http://example.com/1",
        title="Заголовок",
        summary="Аннотация",
        published_at=datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
        content_html="<p>текст статьи</p>",
    )
    src = NewsSource(name="test", url="http://example.com/rss", fulltext="rss", enabled=True)
    text = await get_fulltext(client, robots, item, src, max_bytes=10000)
    assert text == "текст статьи"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_fulltext_page_robots_disallowed() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text="<p>не должен загружаться</p>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    robots = RobotsCache(client, "test-agent")
    item = RawItem(
        source="test",
        url="http://example.com/page/1",
        title="Заголовок",
        summary="Аннотация",
        published_at=datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
        content_html=None,
    )
    src = NewsSource(name="test", url="http://example.com/rss", fulltext="page", enabled=True)
    text = await get_fulltext(client, robots, item, src, max_bytes=10000)
    assert text is None
    await client.aclose()
