"""Тесты pipeline.collect_stage (MockTransport + :memory: БД + FakeClock)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from trading_assistant.clock import FakeClock
from trading_assistant.config import Keywords, NewsSource, load_settings
from trading_assistant.db import Database
from trading_assistant.pipeline import CollectDeps, collect_stage
from trading_assistant.repository import Repository

FIXTURES = "tests/fixtures"


DEFAULT_PUB = "Fri, 06 Nov 2026 10:00:00 +0300"
BODY = "<p>" + ("Полный текст новости о ключевой ставке и российском рынке. " * 20) + "</p>"


def _make_rss(
    title: str, link: str, pub_date: str = DEFAULT_PUB, body: str = BODY
) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><item><title>{title}</title><link>{link}</link>"
        f"<summary>Краткое описание новости о рынке и акциях.</summary>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<content:encoded><![CDATA[{body}]]></content:encoded>"
        "</item></channel></rss>"
    ).encode()


async def _make_db() -> Database:
    db = Database(":memory:")
    await db.init()
    return db


def _make_settings(sources: list[NewsSource]) -> object:
    settings = load_settings(Path("config/settings.yaml"))
    settings.news.sources = sources
    return settings


async def test_collect_stage_two_sources_one_cluster() -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)

    db = await _make_db()
    repo = Repository(db)

    settings = _make_settings(
        [
            NewsSource(name="src1", url="http://example1.com/rss", fulltext="rss", enabled=True),
            NewsSource(name="src2", url="http://example2.com/rss", fulltext="rss", enabled=True),
        ]
    )
    keywords = Keywords(include=["ставк"], exclude=[])

    rss1 = _make_rss("ЦБ повысил ставку до 17%", "http://example1.com/news/1")
    rss2 = _make_rss(
        "ЦБ поднимает ставку до 17%",
        "http://example2.com/news/2",
        "Fri, 06 Nov 2026 10:30:00 +0300",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "example1" in url:
            return httpx.Response(200, content=rss1)
        if "example2" in url:
            return httpx.Response(200, content=rss2)
        return httpx.Response(404, content=b"not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    deps = CollectDeps(client=client, repository=repo, settings=settings, keywords=keywords)
    stats = await collect_stage(deps, clock.now())
    await client.aclose()
    await db.close()

    assert stats.fetched == 2
    assert stats.clustered == 2
    assert stats.new_clusters == 1
    assert stats.duplicates == 0
    assert stats.filtered == 0
    assert stats.no_fulltext == 0


async def test_collect_stage_repeat_call_duplicates() -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)

    db = await _make_db()
    repo = Repository(db)

    settings = _make_settings(
        [NewsSource(name="src1", url="http://example1.com/rss", fulltext="rss", enabled=True)]
    )
    keywords = Keywords(include=["ставк"], exclude=[])

    rss = _make_rss("ЦБ повысил ставку до 17%", "http://example1.com/news/1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=rss)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    deps = CollectDeps(client=client, repository=repo, settings=settings, keywords=keywords)
    stats1 = await collect_stage(deps, clock.now())
    assert stats1.clustered == 1
    assert stats1.new_clusters == 1

    stats2 = await collect_stage(deps, clock.now())
    assert stats2.fetched == 1
    assert stats2.duplicates == 1
    assert stats2.clustered == 0
    assert stats2.new_clusters == 0

    await client.aclose()
    await db.close()


async def test_collect_stage_short_text_no_fulltext() -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)

    db = await _make_db()
    repo = Repository(db)

    settings = _make_settings(
        [NewsSource(name="src1", url="http://example1.com/rss", fulltext="rss", enabled=True)]
    )
    settings.news.min_fulltext_chars = 500
    keywords = Keywords(include=["ставк"], exclude=[])

    rss = _make_rss(
        "ЦБ повысил ставку до 17%", "http://example1.com/news/1", body="<p>коротко</p>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=rss)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    deps = CollectDeps(client=client, repository=repo, settings=settings, keywords=keywords)
    stats = await collect_stage(deps, clock.now())
    await client.aclose()
    await db.close()

    assert stats.no_fulltext == 1
    assert stats.clustered == 0


async def test_collect_stage_source_500_other_processed() -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)

    db = await _make_db()
    repo = Repository(db)

    settings = _make_settings(
        [
            NewsSource(
                name="src_bad", url="http://bad.example.com/rss", fulltext="rss", enabled=True
            ),
            NewsSource(
                name="src_good", url="http://good.example.com/rss", fulltext="rss", enabled=True
            ),
        ]
    )
    keywords = Keywords(include=["ставк"], exclude=[])

    rss_good = _make_rss("ЦБ повысил ставку до 17%", "http://good.example.com/news/1")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "bad" in url:
            return httpx.Response(500, content=b"error")
        if "good" in url:
            return httpx.Response(200, content=rss_good)
        return httpx.Response(404, content=b"not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    deps = CollectDeps(client=client, repository=repo, settings=settings, keywords=keywords)
    stats = await collect_stage(deps, clock.now())
    await client.aclose()
    await db.close()

    assert stats.fetched == 1
    assert stats.clustered == 1
    assert stats.new_clusters == 1


