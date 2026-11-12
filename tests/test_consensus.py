"""Тесты консенсуса: пороги выдачи, согласие двух моделей, приоритет (CONTRACTS §5)."""
from __future__ import annotations

import pytest

from trading_assistant.config import SignalSettings
from trading_assistant.llm.consensus import (
    ConsensusRow,
    build_consensus,
    is_issued,
    is_priority,
)
from trading_assistant.models import LLMAnalysis, LLMInstrument


def signal(**kw) -> SignalSettings:
    """Пороги сигнала как в config/settings.yaml, при необходимости переопределённые."""
    base = dict(
        min_confidence=85,
        min_severity=6,
        min_move_pct=15,
        max_horizon_days=180,
        magnitude_tolerance=0.20,
        priority_move_pct=25,
        priority_fast_days=120,
    )
    base.update(kw)
    return SignalSettings(**base)


def item(
    ticker: str = "SBER",
    direction: str = "up",
    conf: int = 90,
    h: int = 60,
    pct: float = 18.0,
    reason: str = "x",
) -> LLMInstrument:
    """Инструмент в ответе модели с нужными полями."""
    return LLMInstrument(
        ticker=ticker,
        direction=direction,
        confidence=conf,
        horizon_days=h,
        expected_change_pct=pct,
        reason=reason,
    )


def analysis(*items: LLMInstrument, severity: int = 8) -> LLMAnalysis:
    """Ответ модели: важность новости и её инструменты."""
    return LLMAnalysis(severity=severity, instruments=list(items))


@pytest.mark.parametrize(
    "kw,ok",
    [
        (dict(conf=85), True),
        (dict(conf=84), False),
        (dict(pct=15.0), True),
        (dict(pct=14.9), False),
        (dict(h=180), True),
        (dict(h=181), False),
        (dict(h=1), True),
    ],
)
def test_is_issued_boundaries(kw, ok) -> None:
    """Границы порогов одной модели: confidence, величина, срок."""
    assert is_issued(item(**kw), 8, signal()) is ok


@pytest.mark.parametrize("severity,ok", [(6, True), (5, False)])
def test_is_issued_severity(severity, ok) -> None:
    """Порог важности новости: 6 проходит, 5 нет."""
    assert is_issued(item(), severity, signal()) is ok


@pytest.mark.parametrize(
    "a_kw,b_kw,expected",
    [
        # CONTRACTS §5: A (dir, conf, m, h) | B | результат
        (dict(conf=90, pct=15, h=60), dict(conf=88, pct=18, h=90), ("up", 88, 15, 90)),
        (dict(conf=90, pct=15, h=60), dict(conf=90, pct=19, h=60), None),
        (dict(direction="up"), dict(direction="down"), None),
        (dict(conf=90, pct=15, h=30), dict(conf=90, pct=15, h=170), ("up", 90, 15, 170)),
        (dict(conf=84, pct=20, h=60), dict(conf=90, pct=20, h=60), None),
        (dict(pct=14.9), dict(pct=15), None),
        (dict(pct=20, h=181), dict(pct=20, h=60), None),
    ],
)
def test_contracts_table(a_kw, b_kw, expected) -> None:
    """Семь строк таблицы CONTRACTS §5: согласие, величины, направления, пороги."""
    rows = build_consensus(analysis(item(**a_kw)), analysis(item(**b_kw)), signal())
    if expected is None:
        assert rows == []
        return
    assert len(rows) == 1
    row = rows[0]
    assert (row.direction, row.confidence, row.expected_change_pct, row.horizon_days) == expected


@pytest.mark.parametrize("pct_b,rows_expected", [(18.0, 1), (18.01, 0)])
def test_magnitude_tolerance_boundary(pct_b, rows_expected) -> None:
    """Ровно 20% расхождения проходят, чуть больше — нет."""
    rows = build_consensus(analysis(item(pct=15)), analysis(item(pct=pct_b)), signal())
    assert len(rows) == rows_expected


def test_consensus_only_common_tickers() -> None:
    """Тикер есть только у одной модели — строки нет; оба совпали — одна строка."""
    rows = build_consensus(
        analysis(item("SBER"), item("LKOH")), analysis(item("SBER"), item("TATN")), signal()
    )
    assert [row.ticker for row in rows] == ["SBER"]


def test_consensus_severity_is_minimum_and_checked() -> None:
    """Severity в строке — минимум двух ответов; ниже порога консенсуса нет."""
    assert build_consensus(
        analysis(item(), severity=5), analysis(item(), severity=9), signal()
    ) == []
    rows = build_consensus(analysis(item(), severity=7), analysis(item(), severity=9), signal())
    assert rows[0].severity == 7


def test_consensus_reason_from_lower_confidence() -> None:
    """Обоснование берётся у модели с меньшей уверенностью, при равенстве — у первой."""
    rows = build_consensus(
        analysis(item(conf=95, reason="от A")),
        analysis(item(conf=88, reason="от B")),
        signal(),
    )
    assert rows[0].reason == "от B"
    rows = build_consensus(
        analysis(item(conf=90, reason="от A")),
        analysis(item(conf=90, reason="от B")),
        signal(),
    )
    assert rows[0].reason == "от A"


def test_consensus_row_is_frozen() -> None:
    """Строка консенсуса неизменяема."""
    row = ConsensusRow("SBER", "up", 90, 8, 60, 15, "x")
    with pytest.raises(AttributeError):
        row.ticker = "LKOH"


@pytest.mark.parametrize(
    "pct,h,expected",
    [
        (25.0, 150, False),
        (25.1, 150, True),
        (15.0, 119, True),
        (15.0, 120, False),
        (14.9, 10, False),
    ],
)
def test_is_priority(pct, h, expected) -> None:
    """Приоритет: величина > 25% или >= 15% за срок < 120 дней."""
    row = ConsensusRow("SBER", "up", 90, 8, h, pct, "x")
    assert is_priority(row, signal()) is expected
