"""Порог выдачи, консенсус двух моделей пары и приоритет алерта (CONTRACTS §5)."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import SignalSettings
from ..models import LLMAnalysis, LLMInstrument

# Погрешность сравнения величин: 15 и 18 дают ровно 20% и должны проходить.
TOLERANCE_EPS = 1e-9


@dataclass(frozen=True)
class ConsensusRow:
    """Строка консенсуса: общий прогноз двух моделей пары по одному тикеру."""
    ticker: str
    direction: str
    confidence: int
    severity: int
    horizon_days: int
    expected_change_pct: float
    reason: str


def is_issued(item: LLMInstrument, severity: int, signal: SignalSettings) -> bool:
    """Прошёл ли прогноз модели ВСЕ пороги выдачи (CONTRACTS §5)."""
    return (
        item.confidence >= signal.min_confidence
        and severity >= signal.min_severity
        and item.expected_change_pct >= signal.min_move_pct
        and 1 <= item.horizon_days <= signal.max_horizon_days
    )


def _find(analysis: LLMAnalysis, ticker: str) -> LLMInstrument | None:
    """Инструмент с нужным тикером (первый в ответе модели)."""
    for item in analysis.instruments:
        if item.ticker == ticker:
            return item
    return None


def _close_enough(lo: float, hi: float, tolerance: float) -> bool:
    """Расхождение величин в пределах допуска (с погрешностью TOLERANCE_EPS)."""
    return (hi - lo) / lo <= tolerance + TOLERANCE_EPS


def build_consensus(a: LLMAnalysis, b: LLMAnalysis, signal: SignalSettings) -> list[ConsensusRow]:
    """Строки консенсуса по тикерам, где обе модели прошли пороги и согласны."""
    rows: list[ConsensusRow] = []
    for x in a.instruments:
        y = _find(b, x.ticker)
        if y is None:
            continue
        if not (is_issued(x, a.severity, signal) and is_issued(y, b.severity, signal)):
            continue
        if x.direction != y.direction:
            continue
        lo = min(x.expected_change_pct, y.expected_change_pct)
        hi = max(x.expected_change_pct, y.expected_change_pct)
        if not _close_enough(lo, hi, signal.magnitude_tolerance):
            continue
        rows.append(
            ConsensusRow(
                ticker=x.ticker,
                direction=x.direction,
                confidence=min(x.confidence, y.confidence),
                severity=min(a.severity, b.severity),
                horizon_days=max(x.horizon_days, y.horizon_days),
                expected_change_pct=lo,
                reason=y.reason if y.confidence < x.confidence else x.reason,
            )
        )
    return rows


def is_priority(row: ConsensusRow, signal: SignalSettings) -> bool:
    """Приоритетный алерт: сильное движение или быстрое движение (CONTRACTS §5)."""
    return row.expected_change_pct > signal.priority_move_pct or (
        row.expected_change_pct >= signal.min_move_pct
        and row.horizon_days < signal.priority_fast_days
    )
