"""Тесты backtest.evaluate: каждая строка таблицы CONTRACTS §6 и граничные случаи."""
from __future__ import annotations

from datetime import date

import pytest

from trading_assistant.backtest import EvalResult, evaluate
from trading_assistant.market.moex import Candle

D = date(2026, 3, 2)  # день создания прогноза


def candle(day: int, high: float, low: float = 99.0, close: float = 100.0) -> Candle:
    """Свеча марта 2026 (day — число месяца)."""
    return Candle(date(2026, 3, day), high, low, close)


def run(
    direction: str,
    candles: list[Candle],
    today: date,
    horizon: int = 30,
    expected: float = 20.0,
    base: float | None = 100.0,
) -> EvalResult | None:
    """Вызвать evaluate с порогом min_move_pct = 15."""
    return evaluate(
        direction=direction,
        horizon_days=horizon,
        expected_pct=expected,
        created_msk=D,
        base_price=base,
        candles=candles,
        today_msk=today,
        min_move_pct=15.0,
    )


def test_up_reached_within_day() -> None:
    result = run("up", [candle(3, 116.0, 99.0, 101.0)], today=date(2026, 3, 5))
    assert result == EvalResult(
        check_date=date(2026, 3, 3),
        check_price=116.0,
        max_move_pct=16.0,
        target_hit=0,
        is_successful=1,
    )


def test_up_returned_after_hit_is_still_success() -> None:
    result = run("up", [candle(3, 116.0), candle(4, 90.0, 80.0, 85.0)], today=date(2026, 3, 6))
    assert result is not None
    assert result.is_successful == 1
    assert result.check_date == date(2026, 3, 3)


def test_exactly_15_percent_is_success() -> None:
    result = run("up", [candle(3, 115.0)], today=date(2026, 3, 5))
    assert result is not None
    assert result.is_successful == 1
    assert result.max_move_pct == 15.0


def test_below_threshold_while_term_running_waits() -> None:
    assert run("up", [candle(3, 114.9)], today=date(2026, 3, 5)) is None


def test_term_expired_without_hit_is_failure() -> None:
    candles = [candle(3, 110.0, 99.0, 105.0), candle(5, 112.0, 99.0, 108.0)]
    result = run("up", candles, today=date(2026, 4, 5))
    assert result == EvalResult(
        check_date=date(2026, 3, 5),
        check_price=108.0,
        max_move_pct=12.0,
        target_hit=0,
        is_successful=0,
    )


def test_down_uses_low_not_high() -> None:
    assert run("down", [candle(3, 130.0, 99.0)], today=date(2026, 3, 5)) is None
    result = run("down", [candle(3, 101.0, 84.0)], today=date(2026, 3, 5))
    assert result is not None
    assert result.is_successful == 1
    assert result.check_price == 84.0
    assert result.max_move_pct == 16.0


def test_down_high_ignored_and_failure_after_term() -> None:
    result = run("down", [candle(3, 130.0, 99.0)], today=date(2026, 4, 5))
    assert result is not None
    assert result.is_successful == 0
    assert result.max_move_pct == 1.0


def test_creation_day_candle_is_ignored() -> None:
    candles = [Candle(D, 130.0, 99.0, 100.0)]
    assert run("up", candles, today=date(2026, 3, 5)) is None


def test_candle_after_end_date_is_ignored() -> None:
    candles = [candle(3, 105.0), Candle(date(2026, 5, 1), 130.0, 99.0, 100.0)]
    result = run("up", candles, today=date(2026, 5, 10), horizon=30)
    assert result is not None
    assert result.is_successful == 0
    assert result.check_date == date(2026, 3, 3)


def test_candle_on_last_day_of_term_counts() -> None:
    candles = [Candle(date(2026, 4, 1), 116.0, 99.0, 100.0)]
    result = run("up", candles, today=date(2026, 4, 2), horizon=30)
    assert result is not None
    assert result.is_successful == 1
    assert result.check_date == date(2026, 4, 1)


def test_candle_after_last_day_of_term_ignored() -> None:
    candles = [Candle(date(2026, 4, 2), 116.0, 99.0, 100.0)]
    assert run("up", candles, today=date(2026, 4, 5), horizon=30) is None


def test_target_hit_reflects_expected_move() -> None:
    small = run("up", [candle(3, 116.0)], today=date(2026, 3, 5), expected=20.0)
    assert small is not None
    assert small.target_hit == 0
    big = run("up", [candle(3, 121.0)], today=date(2026, 3, 5), expected=20.0)
    assert big is not None
    assert big.target_hit == 1


def test_max_move_pct_is_max_over_whole_window() -> None:
    result = run("up", [candle(3, 116.0), candle(4, 130.0)], today=date(2026, 3, 6))
    assert result is not None
    assert result.check_date == date(2026, 3, 3)
    assert result.max_move_pct == 30.0
    assert result.target_hit == 1


def test_base_price_none_takes_last_close_on_or_before_creation() -> None:
    candles = [Candle(date(2026, 2, 27), 100.0, 99.0, 100.0), candle(3, 116.0)]
    result = run("up", candles, base=None, today=date(2026, 3, 5))
    assert result is not None
    assert result.is_successful == 1


def test_base_price_none_uses_close_as_reference() -> None:
    candles = [Candle(date(2026, 2, 27), 101.0, 99.0, 100.0), candle(3, 110.0, 99.0, 105.0)]
    result = run("up", candles, base=None, today=date(2026, 4, 5))
    assert result is not None
    assert result.is_successful == 0
    assert result.max_move_pct == 10.0


def test_base_price_none_without_history_returns_none() -> None:
    assert run("up", [candle(3, 116.0)], base=None, today=date(2026, 3, 5)) is None


def test_non_positive_base_price_returns_none() -> None:
    assert run("up", [candle(3, 116.0)], base=0.0, today=date(2026, 3, 5)) is None
    assert run("up", [candle(3, 116.0)], base=-5.0, today=date(2026, 3, 5)) is None


def test_no_candles_after_long_wait_is_unknown() -> None:
    result = run("up", [], today=date(2026, 5, 1), horizon=10)
    assert result == EvalResult(None, None, None, None, None)


def test_no_candles_while_term_running_waits() -> None:
    assert run("up", [], today=date(2026, 3, 5), horizon=10) is None


def test_empty_window_within_grace_period_waits() -> None:
    candles = [Candle(D, 130.0, 99.0, 100.0)]
    assert run("up", candles, today=date(2026, 3, 20), horizon=10) is None


def test_unsorted_candles_are_ordered_by_date() -> None:
    candles = [candle(5, 116.0), candle(3, 110.0), candle(4, 111.0)]
    result = run("up", candles, today=date(2026, 3, 6))
    assert result is not None
    assert result.check_date == date(2026, 3, 5)


def test_unknown_direction_raises() -> None:
    with pytest.raises(ValueError):
        run("sideways", [candle(3, 116.0)], today=date(2026, 3, 5))
