"""Тесты stats.py: точность, стоимость выданных прогнозов и отчёт --report (STEP 4C)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_assistant.clock import to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.repository import Repository
from trading_assistant.stats import Stats, efficiency, verdict

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PAIR_A = "anthropic/claude-sonnet-5.5"
PAIR_B = "x-ai/grok-4.7"
PAIR_KEY = f"{PAIR_A}+{PAIR_B}"
MODEL = "openai/gpt-5.4-mini"


def _settings() -> AppSettings:
    """Настройки сервиса из config/settings.yaml."""
    return load_settings(Path("config/settings.yaml"))


def _stats(db: Database) -> Stats:
    """Stats с настройками из конфига и часами, зафиксированными на NOW."""
    cfg = _settings()
    return Stats(Repository(db), cfg.signal, cfg.evaluation, lambda: NOW)


async def _cluster(db: Database, cluster_id: int) -> None:
    """Кластер-заглушка: нужен для внешних ключей прогнозов и запросов."""
    await db.execute(
        "INSERT OR IGNORE INTO clusters (id, title, title_norm, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (cluster_id, f"Кластер {cluster_id}", f"кластер {cluster_id}", to_iso(NOW), to_iso(NOW)),
    )


async def _run(
    db: Database, cluster_id: int, model: str, cost: float = 0.0, status: str = "ok"
) -> int:
    """Один HTTP-запрос к модели по кластеру; вернуть id записи model_runs."""
    await _cluster(db, cluster_id)
    return await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, cost_usd, created_at) "
        "VALUES (?, ?, 'v2', ?, ?, ?)",
        (cluster_id, model, status, cost, to_iso(NOW)),
    )


async def _prediction(
    db: Database,
    cluster_id: int,
    *,
    model: str = "consensus",
    ticker: str = "SBER",
    ok: int | None = 1,
    evaluated: bool = True,
    is_issued: int = 1,
    prompt_version: str = "v2",
    pair_key: str | None = PAIR_KEY,
    run_id: int | None = None,
) -> int:
    """Прогноз с результатом проверки; вернуть его id."""
    await _cluster(db, cluster_id)
    return await db.execute(
        "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
        "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
        "is_issued, run_id, is_successful, evaluated_at) "
        "VALUES (?, ?, ?, 'up', 90, 8, 30, 20.0, 'причина', ?, ?, ?, ?, ?, ?, ?)",
        (
            cluster_id,
            model,
            ticker,
            to_iso(NOW),
            prompt_version,
            pair_key,
            is_issued,
            run_id,
            ok,
            to_iso(NOW) if evaluated else None,
        ),
    )


def test_efficiency_and_verdict_texts() -> None:
    assert efficiency(0.5, 80.0) == pytest.approx(0.00625)
    assert efficiency(0.0, 80.0) == 0.0
    assert efficiency(0.5, 0.0) is None
    assert efficiency(0.5, None) is None
    assert verdict(80.0, 100, 100, 75) == "ПРОЕКТ ПРИГОДЕН"
    assert verdict(74.0, 100, 100, 75) == "ПРОЕКТ НЕ ПРИГОДЕН"
    assert verdict(99.0, 40, 100, 75) == "НАКОПЛЕНИЕ ДАННЫХ (n=40 из 100)"
    assert verdict(None, 0, 100, 75) == "НАКОПЛЕНИЕ ДАННЫХ (n=0 из 100)"


async def test_accuracy_counts_only_evaluated_issued_predictions(db: Database) -> None:
    """Точность пары: 7 успешных из 9 проверенных; невыданные и без данных MOEX не входят."""
    for position, ticker in enumerate(["SBER", "LKOH", "GAZP", "ROSN", "GMKN", "VTBR", "MTSS"]):
        await _prediction(db, 100 + position, ticker=ticker, ok=1)
    for position, ticker in enumerate(["ALRS", "CHMF"]):
        await _prediction(db, 200 + position, ticker=ticker, ok=0)
    await _prediction(db, 300, ticker="MOEX", ok=None)  # нет данных MOEX
    await _prediction(db, 301, ticker="SNGS", ok=1, is_issued=0)  # невыданный
    await _prediction(db, 302, ticker="PLZL", ok=None, evaluated=False)  # не проверен

    percent, n = await _stats(db).accuracy("consensus")

    assert n == 9
    assert percent == pytest.approx(700 / 9)
    assert round(percent, 1) == 77.8


async def test_accuracy_filters_by_ticker_pair_and_version(db: Database) -> None:
    await _prediction(db, 1, ticker="SBER", ok=1)
    await _prediction(db, 2, ticker="LKOH", ok=0)
    await _prediction(db, 3, ticker="SBER", ok=0, pair_key="openai/a+openai/b")
    await _prediction(db, 4, ticker="SBER", ok=1, prompt_version="v3")
    stats = _stats(db)

    percent, n = await stats.accuracy("consensus", ticker="SBER", pair_key=PAIR_KEY)
    assert (percent, n) == (pytest.approx(100.0), 2)

    percent, n = await stats.accuracy("consensus", pair_key=PAIR_KEY)
    assert (percent, n) == (pytest.approx(200 / 3), 3)

    percent, n = await stats.accuracy("consensus", prompt_version="v3")
    assert (percent, n) == (pytest.approx(100.0), 1)

    assert await stats.accuracy("unknown/model") == (None, 0)


async def test_cost_of_issued_counts_each_run_once(db: Database) -> None:
    """Три запроса по $0.01; два дали выданные прогнозы (один — два прогноза) → $0.02."""
    first = await _run(db, 1, MODEL, 0.01)
    second = await _run(db, 2, MODEL, 0.01)
    third = await _run(db, 3, MODEL, 0.01)
    await _prediction(db, 1, model=MODEL, ticker="SBER", run_id=first, pair_key=None)
    await _prediction(db, 1, model=MODEL, ticker="LKOH", run_id=first, pair_key=None)
    await _prediction(db, 2, model=MODEL, ticker="GAZP", run_id=second, pair_key=None)
    await _prediction(
        db, 3, model=MODEL, ticker="ROSN", run_id=third, pair_key=None, is_issued=0
    )
    stats = _stats(db)

    assert await stats.cost_of_issued(MODEL) == pytest.approx(0.02)
    assert await stats.total_cost(MODEL) == pytest.approx(0.03)


async def test_cost_of_issued_limited_by_sample_ids(db: Database) -> None:
    first = await _run(db, 1, MODEL, 0.01)
    second = await _run(db, 2, MODEL, 0.01)
    only = await _prediction(db, 1, model=MODEL, ticker="SBER", run_id=first, pair_key=None)
    await _prediction(db, 2, model=MODEL, ticker="LKOH", run_id=second, pair_key=None)

    assert await _stats(db).cost_of_issued(MODEL, [only]) == pytest.approx(0.01)


async def test_pair_cost_uses_ok_runs_of_both_models_by_clusters(db: Database) -> None:
    """Пара: успешные запросы обеих моделей по кластерам выборки; чужие и повторы — нет."""
    await _run(db, 1, PAIR_A, 0.02)
    await _run(db, 1, PAIR_B, 0.03)
    await _run(db, 2, PAIR_A, 0.02)
    await _run(db, 2, PAIR_A, 0.01, status="invalid_json")  # неудачный повтор
    await _run(db, 3, PAIR_A, 0.05)  # кластер вне выборки
    await _run(db, 1, MODEL, 0.5)  # модель не из пары
    first = await _prediction(db, 1, ticker="SBER")
    second = await _prediction(db, 2, ticker="LKOH")

    assert await _stats(db).cost_of_issued("consensus", [first, second]) == pytest.approx(0.07)


async def test_report_on_empty_database(db: Database) -> None:
    text = await _stats(db).report(NOW)

    assert "Состояние проекта: pair_test" in text
    assert "Нет проверенных прогнозов" in text


async def test_report_shows_progress_of_running_test(db: Database) -> None:
    repo = Repository(db)
    await repo.get_or_create_test(f"pair:{PAIR_KEY}", "pair", "v2", NOW)
    await _prediction(db, 1, ticker="SBER", ok=1)
    await _prediction(db, 2, ticker="LKOH", ok=None, evaluated=False)

    text = await _stats(db).report(NOW)

    assert f"pair:{PAIR_KEY}" in text
    assert "проверено 1 из 100" in text
    assert "НАКОПЛЕНИЕ ДАННЫХ (n=1 из 100)" in text


async def test_report_shows_closed_pair_test(db: Database) -> None:
    repo = Repository(db)
    await repo.kv_set("project_state", "candidates_test")
    test = await repo.get_or_create_test(f"pair:{PAIR_KEY}", "pair", "v2", NOW)
    await repo.close_test(test["id"], 100, 80.0, 0.5, 1.0, 0.00625, "suitable", NOW)
    await _prediction(db, 1, ticker="SBER", ok=1)

    text = await _stats(db).report(NOW)

    assert "Состояние проекта: candidates_test" in text
    assert "SBER" in text
    assert "ПРОЕКТ ПРИГОДЕН" in text
