"""Общие фикстуры для тестов шага 1."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from trading_assistant.clock import FakeClock
from trading_assistant.db import Database
from trading_assistant.instruments import Instrument, InstrumentRegistry

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
def msk() -> ZoneInfo:
    return MSK


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 6, 18, 0, tzinfo=UTC))


@pytest.fixture
async def db() -> Database:
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def registry() -> InstrumentRegistry:
    items = [
        Instrument(
            ticker="SBER",
            name="Сбербанк",
            type="share",
            secid="SBER",
            board="TQBR",
            market="shares",
            aliases=["Сбер", "Сбербанк", "Sberbank"],
            enabled=True,
        ),
        Instrument(
            ticker="LKOH",
            name="ЛУКОЙЛ",
            type="share",
            secid="LKOH",
            board="TQBR",
            market="shares",
            aliases=["Лукойл", "LUKOIL"],
            enabled=True,
        ),
        Instrument(
            ticker="IMOEX",
            name="Индекс МосБиржи",
            type="index",
            secid="IMOEX",
            board="SNDX",
            market="index",
            aliases=["IMOEX"],
            enabled=False,
        ),
    ]
    return InstrumentRegistry(items)
