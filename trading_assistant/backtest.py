"""Проверка прогноза по дневным свечам (CONTRACTS §6)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .market.moex import Candle


@dataclass
class EvalResult:
    """Результат проверки прогноза: дата и цена проверки, движение, флаги."""
    check_date: date | None
    check_price: float | None
    max_move_pct: float | None
    target_hit: int | None
    is_successful: int | None


def _move_pct(direction: str, base: float, candle: Candle) -> float:
    """Движение свечи в сторону прогноза в процентах (умножение до деления)."""
    if direction == "up":
        return (candle.high - base) * 100.0 / base
    if direction == "down":
        return (base - candle.low) * 100.0 / base
    raise ValueError(f"неизвестное направление прогноза: {direction}")


def evaluate(
    direction: str,
    horizon_days: int,
    expected_pct: float,
    created_msk: date,
    base_price: float | None,
    candles: list[Candle],
    today_msk: date,
    min_move_pct: float,
) -> EvalResult | None:
    """Проверить прогноз по свечам окна (created_msk, created_msk + horizon_days].

    Успех — движение >= min_move_pct хотя бы на одном закрытии внутри срока.
    None означает «ещё ждём»: ничего записывать не нужно.
    """
    end_date = created_msk + timedelta(days=horizon_days)

    base = base_price
    if base is None:
        history = [candle for candle in candles if candle.date <= created_msk]
        if not history:
            return None
        base = max(history, key=lambda candle: candle.date).close
    if base <= 0:
        return None

    window = sorted(
        (candle for candle in candles if created_msk < candle.date <= end_date),
        key=lambda candle: candle.date,
    )
    if not window:
        no_data_gap = horizon_days + 14
        if today_msk > end_date and (today_msk - created_msk).days > no_data_gap:
            return EvalResult(
                check_date=None,
                check_price=None,
                max_move_pct=None,
                target_hit=None,
                is_successful=None,
            )
        return None

    moves = [_move_pct(direction, base, candle) for candle in window]
    max_move_pct = max(moves)
    target_hit = int(max_move_pct >= expected_pct)

    for candle, move in zip(window, moves, strict=True):
        if move >= min_move_pct:
            return EvalResult(
                check_date=candle.date,
                check_price=candle.high if direction == "up" else candle.low,
                max_move_pct=max_move_pct,
                target_hit=target_hit,
                is_successful=1,
            )

    if today_msk > end_date:
        last = window[-1]
        return EvalResult(
            check_date=last.date,
            check_price=last.close,
            max_move_pct=max_move_pct,
            target_hit=target_hit,
            is_successful=0,
        )
    return None
