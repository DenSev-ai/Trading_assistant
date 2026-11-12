"""Сквозной сценарий шагов 3–4: анализ новостей, проверка цен и вердикт проекта (STEP 4E).

Сеть не используется: OpenRouter и MOEX подменены `httpx.MockTransport`, БД — `:memory:`,
время — `FakeClock`. Новости приходят двумя волнами: у второй волны окно проверки сдвинуто,
поэтому один и тот же набор свечей даёт разные исходы (прогнозы первой волны сбылись,
прогноз второй — нет), а `evaluation.sample_n` = 3 закрывает тест пары сразу после проверки.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from trading_assistant.clock import FakeClock, to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.evaluation import Evaluator, Proposal
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.llm.client import OpenRouterClient
from trading_assistant.llm.consensus import make_pair_key
from trading_assistant.market.moex import MoexClient
from trading_assistant.pipeline import (
    AnalyzeDeps,
    AnalyzeStats,
    BacktestStats,
    BasePriceStats,
    PriceDeps,
    analyze_stage,
    backtest_stage,
    base_price_stage,
)
from trading_assistant.repository import Repository
from trading_assistant.stats import CONSENSUS_MODEL, Stats

MSK = ZoneInfo("Europe/Moscow")
START = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)  # 21:00 МСК, первая волна новостей
GAP_DAYS = 10  # вторая волна новостей приходит позже: сроки прогнозов не совпадают
WAIT_DAYS = 61  # после последней волны все сроки прогнозов истекли
MODEL_HORIZON_DAYS = 60
BASE_PRICE = 100.0
FIRST_HIGH = 116.0  # 16% за 3 дня: первые два прогноза сбываются
FAILING_HIGH = 110.0  # 10% за весь срок: третий прогноз не сбывается
SUCCESS_HIGH = 116.0  # 16% за весь срок: третий прогноз сбывается
SEVERITY = 8
HIGH_CONFIDENCE = 90
LOW_CONFIDENCE = 80  # ниже signal.min_confidence: прогноз не выдаётся
TOKENS_IN = 1000
TOKENS_OUT = 500
SAMPLE_N = 3
MIN_SUCCESS_PCT = 75
ROWS_PER_CLUSTER = 3  # две модели пары и строка консенсуса
PAIR_A = "anthropic/claude-sonnet-5.5"
PAIR_B = "x-ai/grok-4.7"
PAIR_KEY = make_pair_key([PAIR_A, PAIR_B])
TITLES = (
    "ЦБ повысил ключевую ставку",
    "Сбербанк отчитался о прибыли",
    "Сбербанк объявил дивиденды",
    "Сбербанк сменил руководство",
)
TEMPLATE = Path("prompts/analyze_news.txt").read_text(encoding="utf-8")
CandleRow = tuple[date, float, float, float]


def _settings() -> AppSettings:
    """Настройки сервиса из config/settings.yaml с уменьшенной выборкой теста."""
    cfg = load_settings(Path("config/settings.yaml"))
    cfg.evaluation.sample_n = SAMPLE_N
    cfg.evaluation.min_success_pct = MIN_SUCCESS_PCT
    return cfg


def _content(confidence: int, expected_change_pct: float) -> str:
    """JSON-ответ модели: один инструмент SBER, движение вверх."""
    return json.dumps(
        {
            "severity": SEVERITY,
            "instruments": [
                {
                    "ticker": "SBER",
                    "direction": "up",
                    "confidence": confidence,
                    "horizon_days": MODEL_HORIZON_DAYS,
                    "expected_change_pct": expected_change_pct,
                    "reason": "сквозная проверка шагов 3-4",
                }
            ],
        },
        ensure_ascii=False,
    )


def _marketdata(price: float) -> dict:
    """Ответ ISS с последней ценой SBER."""
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


def _candle_rows(third_high: float) -> list[CandleRow]:
    """Свечи вокруг сроков: успех первой волны и потолок третьего прогноза."""
    first_day = START.astimezone(MSK).date()
    return [
        (first_day + timedelta(days=3), FIRST_HIGH, 99.0, 101.0),
        (first_day + timedelta(days=GAP_DAYS + 10), third_high, 99.0, 102.0),
        (first_day + timedelta(days=MODEL_HORIZON_DAYS + 5), 105.0, 99.0, 100.0),
    ]


class _OpenRouter:
    """Поддельный OpenRouter: каждая модель пары отвечает своим прогнозом по SBER."""

    def __init__(self) -> None:
        self.expected = {PAIR_A: 18.0, PAIR_B: 17.0}
        self.confidence = {PAIR_A: HIGH_CONFIDENCE, PAIR_B: HIGH_CONFIDENCE}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model not in self.expected:
            raise AssertionError(f"неожиданный запрос к модели {model}")
        self.calls.append(model)
        content = _content(self.confidence[model], self.expected[model])
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": TOKENS_IN, "completion_tokens": TOKENS_OUT},
            },
        )


class _Moex:
    """Поддельный MOEX ISS: последняя цена и подставные дневные свечи."""

    def __init__(self, price: float = BASE_PRICE) -> None:
        self._price = price
        self.candles: list[CandleRow] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/candles.json"):
            return httpx.Response(200, json=_candles(self.candles))
        return httpx.Response(200, json=_marketdata(self._price))


def _handler(api: _OpenRouter, moex: _Moex) -> Callable[[httpx.Request], httpx.Response]:
    """Направить запрос в мок OpenRouter или MOEX по пути запроса."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return api(request)
        return moex(request)

    return handle


@dataclass
class _Result:
    """Итог сквозного прогона: настройки, статистика этапов, тест пары и предложения."""

    cfg: AppSettings
    pair_before: list[str]
    analyses: list[AnalyzeStats]
    prices: list[BasePriceStats]
    backtest: BacktestStats
    proposals: list[Proposal]
    repeated: list[Proposal]
    pair_test: dict
    calls: list[str]


async def _add_cluster(db: Database, title: str, now: datetime) -> int:
    """Вставить pending-кластер с главной статьёй и вернуть его id."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, sources_count, status) "
        "VALUES (?, ?, ?, ?, 1, 'pending')",
        (title, title.lower(), to_iso(now), to_iso(now)),
    )
    article_id = await db.execute(
        "INSERT INTO articles (url, source, title, title_norm, summary, published_at, fetched_at, "
        "fulltext, status, cluster_id) "
        "VALUES (?, 'interfax', ?, ?, NULL, ?, ?, ?, 'clustered', ?)",
        (
            f"https://example.com/{cluster_id}",
            title,
            title.lower(),
            to_iso(now),
            to_iso(now),
            "Полный текст новости о рынке и ключевой ставке. " * 10,
            cluster_id,
        ),
    )
    await db.execute(
        "UPDATE clusters SET main_article_id = ? WHERE id = ?", (article_id, cluster_id)
    )
    return cluster_id


async def _pair_test_row(db: Database) -> dict:
    """Запись model_tests боевой пары."""
    row = await db.fetchone("SELECT * FROM model_tests WHERE role = 'pair'")
    assert row is not None
    return row


async def _consensus(db: Database) -> list[dict]:
    """Строки консенсуса пары в порядке создания."""
    return await db.fetchall(
        "SELECT id, cluster_id, pair_key, is_issued, is_successful, max_move_pct "
        "FROM predictions WHERE model = ? ORDER BY created_at, id",
        (CONSENSUS_MODEL,),
    )


def _stats(db: Database, cfg: AppSettings) -> Stats:
    """Статистика поверх репозитория с часами, зафиксированными на старте сценария."""
    return Stats(Repository(db), cfg.signal, cfg.evaluation, lambda: START)


async def _issued(db: Database, model: str, prompt_version: str) -> list[dict]:
    """Выданные прогнозы модели для версии промта из конфига."""
    return await Repository(db).issued_predictions(model, prompt_version=prompt_version)


async def _ok_runs_cost(db: Database) -> float:
    """Сумма cost_usd успешных запросов обеих моделей пары."""
    row = await db.fetchone(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs "
        "WHERE status = 'ok' AND model IN (?, ?)",
        (PAIR_A, PAIR_B),
    )
    return float(row["total"]) if row is not None else 0.0


async def _run(
    db: Database,
    registry: InstrumentRegistry,
    *,
    third_high: float,
    unissued_cluster: bool = False,
) -> _Result:
    """Прогнать сценарий: волны новостей, базовые цены, проверка сроков и вердикт."""
    cfg = _settings()
    pair_before = [model.id for model in cfg.llm.pair]
    clock = FakeClock(START)
    repo = Repository(db)
    api = _OpenRouter()
    moex = _Moex()
    analyses: list[AnalyzeStats] = []
    prices: list[BasePriceStats] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler(api, moex)))
    try:
        llm = OpenRouterClient(http, "secret-key", cfg.llm.base_url, cfg.llm.timeout_sec)
        analyze_deps = AnalyzeDeps(
            client=llm,
            repository=repo,
            settings=cfg,
            registry=registry,
            prompt_template=TEMPLATE,
            tz=MSK,
        )
        price_deps = PriceDeps(
            moex=MoexClient(http, clock=clock),
            repository=repo,
            settings=cfg,
            registry=registry,
            tz=MSK,
        )

        for title in TITLES[:2]:
            await _add_cluster(db, title, clock.now())
        analyses.append(await analyze_stage(analyze_deps, clock.now()))
        prices.append(await base_price_stage(price_deps, clock.now()))

        clock.advance(days=GAP_DAYS)
        await _add_cluster(db, TITLES[2], clock.now())
        analyses.append(await analyze_stage(analyze_deps, clock.now()))
        prices.append(await base_price_stage(price_deps, clock.now()))

        if unissued_cluster:
            api.confidence[PAIR_B] = LOW_CONFIDENCE
            await _add_cluster(db, TITLES[3], clock.now())
            analyses.append(await analyze_stage(analyze_deps, clock.now()))
            prices.append(await base_price_stage(price_deps, clock.now()))

        moex.candles = _candle_rows(third_high)
        clock.advance(days=WAIT_DAYS)
        backtest = await backtest_stage(price_deps, clock.now())
        evaluator = Evaluator(
            repo, Stats(repo, cfg.signal, cfg.evaluation, clock.now), cfg, clock.now
        )
        repeated = await evaluator.update(clock.now())
        pair_test = await _pair_test_row(db)
    finally:
        await http.aclose()

    return _Result(
        cfg=cfg,
        pair_before=pair_before,
        analyses=analyses,
        prices=prices,
        backtest=backtest,
        proposals=backtest.proposals,
        repeated=repeated,
        pair_test=pair_test,
        calls=api.calls,
    )


async def test_end_to_end_pair_unsuitable(db, registry) -> None:
    """Два сбывшихся прогноза из трёх дают вердикт unsuitable и project_failed."""
    result = await _run(db, registry, third_high=FAILING_HIGH)

    # Шаг 3: обе модели пары вызваны по каждому кластеру.
    assert result.calls.count(PAIR_A) == len(TITLES[:3])
    assert result.calls.count(PAIR_B) == len(TITLES[:3])

    # Шаг 4: три кластера проанализированы, у каждого свой консенсус пары.
    assert sum(stats.analyzed for stats in result.analyses) == len(TITLES[:3])
    consensus = await _consensus(db)
    assert len(consensus) == len(TITLES[:3])
    assert {row["is_issued"] for row in consensus} == {1}
    assert {row["pair_key"] for row in consensus} == {PAIR_KEY}

    # Шаги 5-6: базовые цены записаны, два прогноза сбылись, третий нет.
    assert result.prices[0].priced == 2 * ROWS_PER_CLUSTER
    prices = await db.fetchall("SELECT DISTINCT base_price FROM predictions")
    assert {row["base_price"] for row in prices} == {BASE_PRICE}
    assert [row["is_successful"] for row in consensus] == [1, 1, 0]
    assert consensus[0]["max_move_pct"] == pytest.approx(16.0)
    assert consensus[2]["max_move_pct"] == pytest.approx(10.0)
    assert result.backtest.waiting == 0
    assert result.backtest.unknown == 0

    # Шаг 7: тест пары закрыт, вердикт unsuitable, проект не пригоден.
    assert result.pair_test["status"] == "done"
    assert result.pair_test["n"] == SAMPLE_N
    assert result.pair_test["success_pct"] == pytest.approx(66.67, abs=0.01)
    assert result.pair_test["verdict"] == "unsuitable"
    assert await Repository(db).kv_get("project_state") == "project_failed"
    assert [proposal.kind for proposal in result.proposals] == ["project_result"]
    assert result.repeated == []  # вердикт фиксируется один раз

    # Шаг 9: боевая пара в конфиге не менялась.
    assert [model.id for model in result.cfg.llm.pair] == result.pair_before


async def test_end_to_end_pair_suitable(db, registry) -> None:
    """Три сбывшихся прогноза дают вердикт suitable, эффективность и candidates_test."""
    result = await _run(db, registry, third_high=SUCCESS_HIGH)

    consensus = await _consensus(db)
    assert [row["is_successful"] for row in consensus] == [1, 1, 1]

    assert result.pair_test["status"] == "done"
    assert result.pair_test["n"] == SAMPLE_N
    assert result.pair_test["success_pct"] == pytest.approx(100.0)
    assert result.pair_test["verdict"] == "suitable"
    cost = await _ok_runs_cost(db)
    assert result.pair_test["cost_issued_usd"] == pytest.approx(cost)
    assert result.pair_test["efficiency"] == pytest.approx(cost / 100.0)
    assert await Repository(db).kv_get("project_state") == "candidates_test"
    assert [proposal.kind for proposal in result.proposals] == ["project_result"]

    # Шаг 9: боевая пара в конфиге не менялась.
    assert [model.id for model in result.cfg.llm.pair] == result.pair_before


async def test_unissued_prediction_not_in_accuracy_or_sample(db, registry) -> None:
    """Прогноз ниже порога confidence не идёт ни в точность, ни в выборку теста."""
    result = await _run(db, registry, third_high=FAILING_HIGH, unissued_cluster=True)

    unissued = await db.fetchall(
        "SELECT id FROM predictions WHERE model = ? AND is_issued = 0 ORDER BY id",
        (PAIR_B,),
    )
    assert len(unissued) == 1
    issued = await _issued(db, PAIR_B, result.cfg.llm.prompt_version)
    assert len(issued) == SAMPLE_N
    assert unissued[0]["id"] not in {int(row["id"]) for row in issued}

    percent, n = await _stats(db, result.cfg).accuracy(PAIR_B)
    assert n == SAMPLE_N
    assert percent == pytest.approx(66.67, abs=0.01)

    model_test = await db.fetchone(
        "SELECT n FROM model_tests WHERE subject = ? AND role = 'pair_model'",
        (PAIR_B,),
    )
    assert model_test is not None
    assert model_test["n"] == SAMPLE_N
    assert result.pair_test["n"] == SAMPLE_N

    # Консенсуса по этой новости нет: вторая модель пары прогноз не выдала.
    assert len(await _consensus(db)) == len(TITLES[:3])

