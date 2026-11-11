"""Тесты instruments.py: реестр тикеров из config/instruments.json."""
from __future__ import annotations

from trading_assistant.instruments import InstrumentRegistry


def test_get_sber(registry: InstrumentRegistry) -> None:
    assert registry.get("SBER").name == "Сбербанк"


def test_get_unknown_ticker(registry: InstrumentRegistry) -> None:
    assert registry.get("XXXX") is None


def test_get_disabled_ticker(registry: InstrumentRegistry) -> None:
    assert registry.get("IMOEX") is None


def test_enabled_list(registry: InstrumentRegistry) -> None:
    tickers = {item.ticker for item in registry.enabled()}
    assert tickers == {"SBER", "LKOH"}


def test_prompt_block(registry: InstrumentRegistry) -> None:
    block = registry.prompt_block()
    assert "SBER — Сбербанк (синонимы: Сбер, Сбербанк, Sberbank)" in block
    assert "LKOH — ЛУКОЙЛ (синонимы: Лукойл, LUKOIL)" in block
    assert "IMOEX" not in block
