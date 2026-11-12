"""Тесты pipeline.base_price_stage и pipeline.backtest_stage (STEP 4B)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from trading_assistant.clock import FakeClock, to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.market.moex import MoexClient
from trading_assistant.pipeline import (
    BacktestStats,
    BasePriceStats,
    PriceDeps,
    backtest_stage,
    base_price_stage,
)
from trading_assistant.repository import Repository

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)  # 12:00 МСК, сегодня 10.03
CREATED = datetime(2026, 3, 2, 7, 0, tzinfo=UTC)  # 10:00 МСК, день создания 02.03
RECENT = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)  # за час до NOW — внутри окна 24 ч
LATER = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)  # срок прогноза (30 дн.) истёк
BASE_URL = "https://iss.example.invalid"
CandleRow = tuple[date, float, float, float]


def _settings() -> AppSettings:
    """Настройки сервиса из config/settings.yaml."""
    return load_settings(Path("config/settings.yaml"))


def _marketdata(price: float | None) -> dict:
    """Ответ ISS с блоком marketdata для SBER."""
    return {
        "marketdata": {
            "columns": ["SECID", "LAST", "CURRENTVALUE"],
            "data": [["SBER", price, None]],
        }
    }


def _candles(rows: list[CandleRow]) -> dict:
    """Ответ ISS с дневными свечами."""
    return {
        "candles": {
            "columns": ["begin", "high", "low", "close"],
            "data": [
                [f"{day.isoformat()} 00:00:00", high, low, close]
                for day, high, low, close in rows
            ],
        }
    }


class _Moex:
    """Поддельный MOEX ISS: ответы по цене и свечам плюс журнал запросов."""

    def __init__(self, price: float | None = None, candles: list[CandleRow] | None = None) -> None:
        self._price = price
        self._candles = candles or []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/candles.json"):
            return httpx.Response(200, json=_candles(self._candles))
        return httpx.Response(200, json=_marketdata(self._price))

    def count(self, suffix: str) -> int:
        """Сколько запросов заканчивается таким путём."""
        return len([request for request in self.requests if request.url.path.endswith(suffix)])


async def _run_base_price(
    db: Database, api: _Moex, registry: InstrumentRegistry, now: datetime = NOW
) -> BasePriceStats:
    """Выполнить base_price_stage против поддельного MOEX."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    try:
        deps = PriceDeps(
            moex=MoexClient(http, BASE_URL, clock=FakeClock(now)),
            repository=Repository(db),
            settings=_settings(),
            registry=registry,
            tz=MSK,
        )
        return await base_price_stage(deps, now)
    finally:
        await http.aclose()


async def _run_backtest(
    db: Database, api: _Moex, registry: InstrumentRegistry, now: datetime = NOW
) -> BacktestStats:
    """Выполнить backtest_stage против поддельного MOEX."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    try:
        deps = PriceDeps(
            moex=MoexClient(http, BASE_URL, clock=FakeClock(now)),
            repository=Repository(db),
            settings=_settings(),
            registry=registry,
            tz=MSK,
        )
        return await backtest_stage(deps, now)
    finally:
        await http.aclose()


async def _add_prediction(
    db: Database,
    *,
    ticker: str = "SBER",
    direction: str = "up",
    horizon_days: int = 30,
    expected_change_pct: float = 20.0,
    base_price: float | None = None,
    issued: int = 1,
    created_at: datetime = CREATED,
) -> int:
    """Вставить кластер и прогноз с нужными полями; вернуть id прогноза."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, sources_count, status) "
        "VALUES ('Новость', 'новость', ?, ?, 1, 'analyzed')",
        (to_iso(created_at), to_iso(created_at)),
    )
    return await db.execute(
        "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
        "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
        "is_issued, run_id, base_price) "
        "VALUES (?, 'anthropic/claude-sonnet-5.5', ?, ?, 90, 8, ?, ?, 'проверка', ?, 'v2', "
        "NULL, ?, NULL, ?)",
        (
            cluster_id,
            ticker,
            direction,
            horizon_days,
            expected_change_pct,
            to_iso(created_at),
            issued,
            base_price,
        ),
    )


async def _prediction(db: Database, prediction_id: int) -> dict:
    """Строка прогноза по id."""
    row = await db.fetchone("SELECT * FROM predictions WHERE id = ?", (prediction_id,))
    assert row is not None
    return row


async def test_base_price_written_for_prediction_without_price(db, registry) -> None:
    prediction_id = await _add_prediction(db, created_at=RECENT)
    api = _Moex(price=250.5)

    stats = await _run_base_price(db, api, registry)

    assert stats == BasePriceStats(checked=1, priced=1, missing=0, unknown=0)
    assert (await _prediction(db, prediction_id))["base_price"] == 250.5
    assert api.count("/securities/SBER.json") == 1


async def test_missing_price_keeps_base_price_null(db, registry) -> None:
    prediction_id = await _add_prediction(db, created_at=RECENT)
    api = _Moex(price=None, candles=[])

    stats = await _run_base_price(db, api, registry)

    assert stats.missing == 1
    assert stats.priced == 0
    assert (await _prediction(db, prediction_id))["base_price"] is None


async def test_old_prediction_gets_no_base_price(db, registry) -> None:
    prediction_id = await _add_prediction(db, created_at=NOW - timedelta(hours=25))
    api = _Moex(price=250.5)

    stats = await _run_base_price(db, api, registry)

    assert stats.checked == 0
    assert (await _prediction(db, prediction_id))["base_price"] is None


async def test_unknown_ticker_is_skipped(db, registry) -> None:
    prediction_id = await _add_prediction(db, ticker="GAZP", created_at=RECENT)
    api = _Moex(price=250.5, candles=[])

    base_stats = await _run_base_price(db, api, registry)
    backtest_stats = await _run_backtest(db, api, registry)

    assert base_stats == BasePriceStats(checked=1, priced=0, missing=0, unknown=1)
    assert backtest_stats == BacktestStats(checked=0, evaluated=0, waiting=0, unknown=1)
    assert api.requests == []
    assert (await _prediction(db, prediction_id))["evaluated_at"] is None


async def test_unissued_prediction_gets_no_price_and_no_evaluation(db, registry) -> None:
    prediction_id = await _add_prediction(db, issued=0)
    api = _Moex(price=250.5, candles=[(date(2026, 3, 5), 116.0, 99.0, 101.0)])

    base_stats = await _run_base_price(db, api, registry)
    backtest_stats = await _run_backtest(db, api, registry)

    assert base_stats.checked == 0
    assert backtest_stats.checked == 0
    assert api.requests == []
    row = await _prediction(db, prediction_id)
    assert row["base_price"] is None
    assert row["evaluated_at"] is None


async def test_backtest_marks_success_and_writes_evaluated_at(db, registry) -> None:
    prediction_id = await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 116.0, 99.0, 101.0)])

    stats = await _run_backtest(db, api, registry)

    assert stats == BacktestStats(checked=1, evaluated=1, waiting=0, unknown=0, skipped=False)
    row = await _prediction(db, prediction_id)
    assert row["is_successful"] == 1
    assert row["check_price"] == 116.0
    assert row["check_date"] == "2026-03-05"
    assert row["evaluated_at"] == to_iso(NOW)


async def test_backtest_waits_while_term_running(db, registry) -> None:
    prediction_id = await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 110.0, 99.0, 105.0)])

    stats = await _run_backtest(db, api, registry)

    assert stats.waiting == 1
    assert stats.evaluated == 0
    row = await _prediction(db, prediction_id)
    assert row["is_successful"] is None
    assert row["evaluated_at"] is None


async def test_backtest_marks_failure_after_term(db, registry) -> None:
    prediction_id = await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 110.0, 99.0, 105.0)])

    stats = await _run_backtest(db, api, registry, now=LATER)

    assert stats.evaluated == 1
    row = await _prediction(db, prediction_id)
    assert row["is_successful"] == 0
    assert row["check_date"] == "2026-03-05"
    assert row["check_price"] == 105.0
    assert row["max_move_pct"] == 10.0


async def test_backtest_not_repeated_before_interval(db, registry) -> None:
    await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 110.0, 99.0, 105.0)])
    interval = _settings().polling.backtest_interval_sec
    clock = FakeClock(NOW)

    first = await _run_backtest(db, api, registry, now=clock.now())
    clock.advance(seconds=interval - 1)
    second = await _run_backtest(db, api, registry, now=clock.now())

    assert first.skipped is False
    assert second.skipped is True
    assert api.count("/candles.json") == 1


async def test_two_predictions_of_one_ticker_share_candles_request(db, registry) -> None:
    first = await _add_prediction(db, base_price=100.0)
    second = await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 116.0, 99.0, 101.0)])

    stats = await _run_backtest(db, api, registry)

    assert stats.checked == 2
    assert stats.evaluated == 2
    assert api.count("/candles.json") == 1
    assert (await _prediction(db, first))["is_successful"] == 1
    assert (await _prediction(db, second))["is_successful"] == 1
    params = api.requests[0].url.params
    assert params["from"] == "2026-02-23"  # день создания 02.03 минус 7 дней
    assert params["till"] == "2026-03-10"


async def test_no_market_data_after_grace_period_is_unknown(db, registry) -> None:
    prediction_id = await _add_prediction(db, base_price=100.0, horizon_days=10)
    api = _Moex(candles=[])
    moment = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)  # срок 12.03 + 14 дней уже прошли

    stats = await _run_backtest(db, api, registry, now=moment)

    assert stats.evaluated == 1
    row = await _prediction(db, prediction_id)
    assert row["is_successful"] is None
    assert row["check_date"] is None
    assert row["check_price"] is None
    assert row["evaluated_at"] == to_iso(moment)


async def test_last_backtest_time_is_stored(db, registry) -> None:
    api = _Moex(candles=[])

    await _run_backtest(db, api, registry)

    assert await Repository(db).kv_get("last_backtest") == to_iso(NOW)


async def test_backtest_creates_pair_and_pair_model_tests(db, registry) -> None:
    """После проверки прогнозов backtest_stage создаёт записи тестов пары и её моделей (4C)."""
    await _add_prediction(db, base_price=100.0)
    api = _Moex(candles=[(date(2026, 3, 5), 116.0, 99.0, 101.0)])

    stats = await _run_backtest(db, api, registry)

    assert stats.proposals == []
    rows = await db.fetchall("SELECT subject, role, status FROM model_tests ORDER BY id")
    assert len(rows) == 3
    assert {row["role"] for row in rows} == {"pair", "pair_model"}
    assert {row["status"] for row in rows} == {"running"}
