"""Тесты market/moex.py: разбор ответов ISS и клиент на httpx.MockTransport."""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from trading_assistant.clock import FakeClock
from trading_assistant.instruments import Instrument
from trading_assistant.market.moex import Candle, MoexClient, parse_candles, parse_last_price

FIXTURES = Path("tests/fixtures")
BASE_URL = "https://iss.example.invalid"
TODAY = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def fixture(name: str) -> dict:
    """JSON-фикстура из tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def share() -> Instrument:
    """Акция SBER из реестра инструментов."""
    return Instrument(
        ticker="SBER",
        name="Сбербанк",
        type="share",
        secid="SBER",
        board="TQBR",
        market="shares",
        aliases=["Сбер"],
        enabled=True,
    )


def index() -> Instrument:
    """Индекс IMOEX из реестра инструментов."""
    return Instrument(
        ticker="IMOEX",
        name="Индекс МосБиржи",
        type="index",
        secid="IMOEX",
        board="SNDX",
        market="index",
        aliases=["IMOEX"],
        enabled=True,
    )


def client_with(handler) -> httpx.AsyncClient:
    """AsyncClient на MockTransport: реальной сети нет."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def router(captured: list[httpx.Request], *, marketdata: dict, candles: dict):
    """Обработчик: свечи для .../candles.json, marketdata для остального."""
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/candles.json"):
            return httpx.Response(200, json=candles)
        return httpx.Response(200, json=marketdata)

    return handler


def test_parse_candles_uses_column_names_and_sorts() -> None:
    candles = parse_candles(fixture("moex_candles.json"))
    assert candles == [
        Candle(date(2026, 3, 3), 268.5, 262.0, 265.0),
        Candle(date(2026, 3, 4), 272.0, 268.0, 270.5),
    ]


def test_parse_candles_skips_row_with_none_high() -> None:
    payload = fixture("moex_candles.json")
    assert len(payload["candles"]["data"]) == 3
    assert all(candle.date != date(2026, 3, 5) for candle in parse_candles(payload))


def test_parse_candles_empty_or_broken_payload() -> None:
    assert parse_candles({}) == []
    assert parse_candles({"candles": {"columns": ["begin"], "data": []}}) == []


def test_parse_last_price_share_uses_last() -> None:
    assert parse_last_price(fixture("moex_marketdata.json"), is_index=False) == 250.5


def test_parse_last_price_index_uses_currentvalue() -> None:
    assert parse_last_price(fixture("moex_marketdata.json"), is_index=True) == 3100.0


def test_parse_last_price_null_values_give_none() -> None:
    payload = {
        "marketdata": {
            "columns": ["SECID", "LAST", "CURRENTVALUE"],
            "data": [["SBER", None, None]],
        }
    }
    assert parse_last_price(payload, is_index=False) is None
    assert parse_last_price(payload, is_index=True) is None
    assert parse_last_price({}, is_index=False) is None


async def test_daily_candles_request_and_result() -> None:
    captured: list[httpx.Request] = []
    handler = router(captured, marketdata={}, candles=fixture("moex_candles.json"))
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        candles = await moex.daily_candles(share(), date(2026, 3, 1), date(2026, 3, 5))
    finally:
        await client.aclose()
    assert [candle.date for candle in candles] == [date(2026, 3, 3), date(2026, 3, 4)]
    url = str(captured[0].url)
    assert "/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER/candles.json" in url
    assert "iss.meta=off" in url
    assert "interval=24" in url
    assert "from=2026-03-01" in url
    assert "till=2026-03-05" in url


async def test_index_uses_market_and_board_from_registry() -> None:
    captured: list[httpx.Request] = []
    handler = router(captured, marketdata={}, candles=fixture("moex_candles.json"))
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        await moex.daily_candles(index(), date(2026, 3, 1), date(2026, 3, 5))
    finally:
        await client.aclose()
    url = str(captured[0].url)
    assert "/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json" in url


async def test_http_500_gives_empty_list_and_none(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "x"})

    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        with caplog.at_level(logging.WARNING):
            assert await moex.daily_candles(share(), date(2026, 3, 1), date(2026, 3, 5)) == []
            assert await moex.last_price(share()) is None
    finally:
        await client.aclose()
    assert "500" in caplog.text


async def test_network_error_gives_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        assert await moex.daily_candles(share(), date(2026, 3, 1), date(2026, 3, 5)) == []
        assert await moex.last_price(index()) is None
    finally:
        await client.aclose()


async def test_last_price_share_takes_last_column() -> None:
    captured: list[httpx.Request] = []
    handler = router(captured, marketdata=fixture("moex_marketdata.json"), candles={})
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        price = await moex.last_price(share())
    finally:
        await client.aclose()
    assert price == 250.5
    assert len(captured) == 1
    url = str(captured[0].url)
    assert "/securities/SBER.json" in url
    assert "iss.meta=off" in url
    assert "iss.only=marketdata" in url


async def test_last_price_index_takes_currentvalue() -> None:
    captured: list[httpx.Request] = []
    handler = router(captured, marketdata=fixture("moex_marketdata.json"), candles={})
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        price = await moex.last_price(index())
    finally:
        await client.aclose()
    assert price == 3100.0
    assert "/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json" in str(
        captured[0].url
    )


async def test_last_price_empty_last_falls_back_to_last_close() -> None:
    captured: list[httpx.Request] = []
    marketdata = {
        "marketdata": {
            "columns": ["SECID", "LAST", "CURRENTVALUE"],
            "data": [["SBER", None, None]],
        }
    }
    handler = router(captured, marketdata=marketdata, candles=fixture("moex_candles.json"))
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        price = await moex.last_price(share())
    finally:
        await client.aclose()
    assert price == 270.5
    assert len(captured) == 2
    candles_url = str(captured[1].url)
    assert "from=2026-03-03" in candles_url
    assert "till=2026-03-10" in candles_url


async def test_last_price_without_any_data_is_none() -> None:
    captured: list[httpx.Request] = []
    marketdata = {"marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", None]]}}
    handler = router(captured, marketdata=marketdata, candles={"candles": {"columns": []}})
    client = client_with(handler)
    try:
        moex = MoexClient(client, BASE_URL, clock=FakeClock(TODAY))
        assert await moex.last_price(share()) is None
    finally:
        await client.aclose()
