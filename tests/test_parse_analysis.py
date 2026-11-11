"""Тесты parse_analysis: разбор и валидация ответа модели (CONTRACTS §3)."""
from __future__ import annotations

import pytest

from trading_assistant.llm.analyzer import parse_analysis

GOOD = (
    '{"severity":7,"instruments":[{"ticker":"SBER","direction":"UP","confidence":90,'
    '"horizon_days":5,"expected_change_pct":3,"reason":"x"}]}'
)


def _two_tickers(first: str, second: str) -> str:
    return (
        '{"severity":7,"instruments":['
        f'{{"ticker":"{first}","direction":"up","confidence":90,"horizon_days":5,'
        '"expected_change_pct":20,"reason":"x"},'
        f'{{"ticker":"{second}","direction":"up","confidence":90,"horizon_days":5,'
        '"expected_change_pct":20,"reason":"x"}]}'
    )


def test_plain_json(registry) -> None:
    analysis = parse_analysis(GOOD, registry)
    assert analysis.severity == 7
    assert len(analysis.instruments) == 1
    assert analysis.instruments[0].direction == "up"


def test_json_in_fence(registry) -> None:
    analysis = parse_analysis(f"```json\n{GOOD}\n```", registry)
    assert [item.ticker for item in analysis.instruments] == ["SBER"]


def test_text_around_json(registry) -> None:
    analysis = parse_analysis(f"Вот ответ: {GOOD}", registry)
    assert [item.ticker for item in analysis.instruments] == ["SBER"]


def test_not_json(registry) -> None:
    with pytest.raises(ValueError):
        parse_analysis("not json", registry)


def test_unknown_ticker_dropped(registry) -> None:
    analysis = parse_analysis(_two_tickers("XXXX", "SBER"), registry)
    assert [item.ticker for item in analysis.instruments] == ["SBER"]


def test_severity_out_of_range(registry) -> None:
    with pytest.raises(ValueError):
        parse_analysis('{"severity":11,"instruments":[]}', registry)


def test_empty_instruments_is_ok(registry) -> None:
    analysis = parse_analysis('{"severity":0,"instruments":[]}', registry)
    assert analysis.severity == 0
    assert analysis.instruments == []


def test_small_move_and_long_horizon_kept(registry) -> None:
    raw = (
        '{"severity":9,"instruments":[{"ticker":"SBER","direction":"up","confidence":10,'
        '"horizon_days":181,"expected_change_pct":10,"reason":"x"}]}'
    )
    analysis = parse_analysis(raw, registry)
    assert len(analysis.instruments) == 1
    assert analysis.instruments[0].horizon_days == 181
    assert analysis.instruments[0].expected_change_pct == 10
