"""Эталонные тесты CONTRACTS §6 (evaluate). Файл НЕ править в шагах: он задаёт правила."""
from datetime import date

import pytest

backtest = pytest.importorskip("trading_assistant.backtest")
moex = pytest.importorskip("trading_assistant.market.moex")

Candle = moex.Candle
evaluate = backtest.evaluate

D = date(2026, 3, 2)  # день создания прогноза


def c(day: int, high: float, low: float = 99.0, close: float = 100.0) -> "Candle":
    """Свеча в марте 2026 (day — число месяца)."""
    return Candle(date(2026, 3, day), high, low, close)


def run(direction, candles, today, horizon=30, expected=20.0, base=100.0, minmove=15.0):
    return evaluate(
        direction=direction,
        horizon_days=horizon,
        expected_pct=expected,
        created_msk=D,
        base_price=base,
        candles=candles,
        today_msk=today,
        min_move_pct=minmove,
    )


def test_up_reached_inside_day():
    r = run("up", [c(3, 116.0, 99.0, 101.0)], today=date(2026, 3, 5))
    assert r.is_successful == 1
    assert r.check_date == date(2026, 3, 3)
    assert r.check_price == 116.0


def test_up_reached_then_returned_still_success():
    r = run("up", [c(3, 116.0), c(4, 90.0, 80.0, 85.0)], today=date(2026, 3, 6))
    assert r.is_successful == 1


def test_exactly_15_percent_counts():
    r = run("up", [c(3, 115.0)], today=date(2026, 3, 5))
    assert r.is_successful == 1


def test_just_below_15_percent_waits_when_term_running():
    assert run("up", [c(3, 114.9)], today=date(2026, 3, 5)) is None


def test_term_expired_without_reaching_is_failure():
    r = run("up", [c(3, 110.0, 99.0, 105.0), c(5, 112.0, 99.0, 108.0)],
            today=date(2026, 4, 5), horizon=30)
    assert r.is_successful == 0
    assert r.check_date == date(2026, 3, 5)
    assert r.check_price == 108.0


def test_down_uses_low_not_high():
    assert run("down", [c(3, 130.0, 99.0)], today=date(2026, 3, 5)) is None
    r = run("down", [c(3, 101.0, 84.0)], today=date(2026, 3, 5))
    assert r.is_successful == 1
    assert r.check_price == 84.0


def test_creation_day_candle_is_ignored():
    candles = [Candle(D, 130.0, 99.0, 100.0)]
    assert run("up", candles, today=date(2026, 3, 5)) is None


def test_candle_after_term_is_ignored():
    candles = [c(3, 105.0), Candle(date(2026, 5, 1), 130.0, 99.0, 100.0)]
    r = run("up", candles, today=date(2026, 5, 10), horizon=30)
    assert r.is_successful == 0


def test_target_hit_flag():
    r = run("up", [c(3, 116.0)], today=date(2026, 3, 5), expected=20.0)
    assert r.is_successful == 1
    assert r.target_hit == 0
    r2 = run("up", [c(3, 121.0)], today=date(2026, 3, 5), expected=20.0)
    assert r2.target_hit == 1


def test_base_price_none_uses_last_close_on_or_before_creation():
    candles = [Candle(date(2026, 2, 27), 100.0, 99.0, 100.0), c(3, 116.0)]
    r = evaluate("up", 30, 20.0, D, None, candles, date(2026, 3, 5), 15.0)
    assert r.is_successful == 1


def test_base_price_none_and_no_history_returns_none():
    assert evaluate("up", 30, 20.0, D, None, [c(3, 116.0)], date(2026, 3, 5), 15.0) is None


def test_no_data_at_all_after_long_wait_is_unknown():
    r = run("up", [], today=date(2026, 5, 1), horizon=10)
    assert r is not None
    assert r.is_successful is None


def test_no_data_but_term_running_waits():
    assert run("up", [], today=date(2026, 3, 5), horizon=10) is None
