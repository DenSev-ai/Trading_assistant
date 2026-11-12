"""Тесты evaluation.py: выборка теста, рейтинг, предложения и Evaluator.update (STEP 4C)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_assistant.clock import to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.evaluation import (
    Evaluator,
    best_pair,
    classify,
    rank_models,
    select_sample,
    success_pct,
)
from trading_assistant.repository import Repository
from trading_assistant.stats import Stats

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PAIR_A = "anthropic/claude-sonnet-5.5"
PAIR_B = "x-ai/grok-4.7"
PAIR_KEY = f"{PAIR_A}+{PAIR_B}"
PAIR_SUBJECT = f"pair:{PAIR_KEY}"
CANDIDATE = "openai/gpt-5.4-mini"
VERSION = "v2"


def _settings(sample_n: int = 4) -> AppSettings:
    """Настройки сервиса с уменьшенной выборкой: поведение то же, данных меньше."""
    cfg = load_settings(Path("config/settings.yaml"))
    cfg.evaluation.sample_n = sample_n
    return cfg


def _evaluator(db: Database, cfg: AppSettings) -> Evaluator:
    """Evaluator с репозиторием, статистикой и часами, зафиксированными на NOW."""
    repo = Repository(db)
    stats = Stats(repo, cfg.signal, cfg.evaluation, lambda: NOW)
    return Evaluator(repo, stats, cfg, lambda: NOW)


async def _predictions(
    db: Database,
    model: str,
    flags: list[int | None],
    *,
    pair_key: str | None = None,
    prompt_version: str = VERSION,
    start_cluster: int = 1,
    evaluated: bool = True,
) -> list[int]:
    """Выданные прогнозы модели: по одному на кластер, флаг — сбылся ли (None — нет данных)."""
    ids: list[int] = []
    for offset, flag in enumerate(flags):
        cluster_id = start_cluster + offset
        await db.execute(
            "INSERT OR IGNORE INTO clusters (id, title, title_norm, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                cluster_id,
                f"Кластер {cluster_id}",
                f"кластер {cluster_id}",
                to_iso(NOW),
                to_iso(NOW),
            ),
        )
        prediction_id = await db.execute(
            "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
            "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
            "is_issued, is_successful, evaluated_at) "
            "VALUES (?, ?, ?, 'up', 90, 8, 30, 20.0, 'причина', ?, ?, ?, 1, ?, ?)",
            (
                cluster_id,
                model,
                f"T{cluster_id}",
                to_iso(NOW),
                prompt_version,
                pair_key,
                flag,
                to_iso(NOW) if evaluated else None,
            ),
        )
        ids.append(prediction_id)
    return ids


async def _tests(db: Database, role: str | None = None, version: str = VERSION) -> list[dict]:
    """Записи model_tests для версии промта (при необходимости — только нужной роли)."""
    rows = await db.fetchall(
        "SELECT * FROM model_tests WHERE prompt_version = ? ORDER BY id", (version,)
    )
    return [row for row in rows if role is None or row["role"] == role]


def _row(prediction_id: int, created_at: str, ok: int | None, evaluated: bool = True) -> dict:
    """Строка прогноза для чистых функций select_sample и success_pct."""
    return {
        "id": prediction_id,
        "created_at": created_at,
        "evaluated_at": "2026-12-01T00:00:00+00:00" if evaluated else None,
        "is_successful": ok,
    }


def _rows(count: int, ok: int = 1, start: int = 0) -> list[dict]:
    """Ряд проверенных прогнозов с возрастающим временем создания."""
    return [
        _row(start + index, f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00", ok)
        for index in range(count)
    ]


def test_select_sample_needs_all_rows() -> None:
    assert select_sample(_rows(3), 4) is None
    assert len(select_sample(_rows(4), 4)) == 4


def test_select_sample_waits_for_unchecked_among_first_rows() -> None:
    rows = _rows(4)
    rows[2] = _row(2, rows[2]["created_at"], None, evaluated=False)

    assert select_sample(rows, 4) is None


def test_select_sample_replaces_unknown_result_by_next_in_time() -> None:
    rows = _rows(5)
    rows[1] = _row(1, rows[1]["created_at"], None, evaluated=True)

    sample = select_sample(rows, 4)

    assert sample is not None
    assert [row["id"] for row in sample] == [0, 2, 3, 4]


def test_select_sample_orders_by_creation_time() -> None:
    rows = list(reversed(_rows(4)))

    sample = select_sample(rows, 4)

    assert [row["id"] for row in sample] == [0, 1, 2, 3]


def test_success_pct_and_classify_boundary() -> None:
    rows = _rows(4)
    rows[3] = _row(3, rows[3]["created_at"], 0)

    sample = select_sample(rows, 4)

    assert success_pct(sample) == pytest.approx(75.0)
    assert classify(75.0, 75) == "suitable"
    assert classify(74.99, 75) == "unsuitable"


def test_rank_models_keeps_only_suitable_by_efficiency() -> None:
    tests = [
        {"subject": "openai/a", "verdict": "suitable", "efficiency": 0.02},
        {"subject": "openai/b", "verdict": "unsuitable", "efficiency": 0.001},
        {"subject": "x-ai/c", "verdict": "suitable", "efficiency": None},
        {"subject": "deepseek/d", "verdict": "suitable", "efficiency": 0.01},
    ]

    ranked = rank_models(tests)

    assert [test["subject"] for test in ranked] == ["deepseek/d", "openai/a", "x-ai/c"]


def test_best_pair_needs_different_vendors() -> None:
    ranked = [
        {"subject": "openai/a", "efficiency": 0.01},
        {"subject": "openai/b", "efficiency": 0.02},
    ]

    assert best_pair(ranked) is None


def test_best_pair_takes_minimum_sum() -> None:
    ranked = [
        {"subject": "openai/a", "efficiency": 0.01},
        {"subject": "x-ai/b", "efficiency": 0.05},
        {"subject": "deepseek/c", "efficiency": 0.02},
    ]

    first, second = best_pair(ranked)

    assert (first["subject"], second["subject"]) == ("openai/a", "deepseek/c")


async def test_pair_test_suitable_switches_project_state(db: Database) -> None:
    cfg = _settings()
    await _predictions(db, "consensus", [1, 1, 1, 0], pair_key=PAIR_KEY)

    proposals = await _evaluator(db, cfg).update(NOW)

    pair_test = (await _tests(db, role="pair"))[0]
    assert await Repository(db).kv_get("project_state") == "candidates_test"
    assert (pair_test["status"], pair_test["verdict"]) == ("done", "suitable")
    assert (pair_test["n"], pair_test["success_pct"]) == (4, pytest.approx(75.0))
    assert [proposal.kind for proposal in proposals] == ["project_result"]
    assert "ПРОЕКТ ПРИГОДЕН" in proposals[0].text


async def test_pair_test_unsuitable_marks_project_failed(db: Database) -> None:
    cfg = _settings()
    await _predictions(db, "consensus", [1, 0, 0, 0], pair_key=PAIR_KEY)

    proposals = await _evaluator(db, cfg).update(NOW)

    assert await Repository(db).kv_get("project_state") == "project_failed"
    assert [proposal.kind for proposal in proposals] == ["project_result"]
    assert "ПРОЕКТ НЕ ПРИГОДЕН" in proposals[0].text


async def test_unsuitable_pair_model_gets_replacement_proposal(db: Database) -> None:
    cfg = _settings()
    repo = Repository(db)
    await _predictions(db, PAIR_A, [1, 1, 1, 1], start_cluster=1)
    await _predictions(db, PAIR_B, [1, 1, 0, 0], start_cluster=11)
    await repo.get_or_create_test(CANDIDATE, "candidate", VERSION, NOW)
    candidate_test = await repo.get_or_create_test(CANDIDATE, "candidate", VERSION, NOW)
    await repo.close_test(candidate_test["id"], 4, 90.0, 0.001, 0.002, 0.0000111, "suitable", NOW)

    proposals = await _evaluator(db, cfg).update(NOW)

    assert [proposal.kind for proposal in proposals] == ["replace_model"]
    assert PAIR_B in proposals[0].text
    assert CANDIDATE in proposals[0].text
    assert [model.id for model in cfg.llm.pair] == [PAIR_A, PAIR_B]


async def test_candidate_test_closes_only_when_sample_is_ready(db: Database) -> None:
    cfg = _settings()
    await Repository(db).kv_set("project_state", "candidates_test")
    await _predictions(db, CANDIDATE, [1, 1, 1], start_cluster=1)

    await _evaluator(db, cfg).update(NOW)

    running = [test for test in await _tests(db, role="candidate") if test["subject"] == CANDIDATE]
    assert running[0]["status"] == "running"

    await _predictions(db, CANDIDATE, [1], start_cluster=10)
    await _evaluator(db, cfg).update(NOW)

    done = [test for test in await _tests(db, role="candidate") if test["subject"] == CANDIDATE]
    assert (done[0]["status"], done[0]["verdict"]) == ("done", "suitable")


async def test_unsuitable_candidate_is_not_ranked(db: Database) -> None:
    cfg = _settings(sample_n=5)
    repo = Repository(db)
    await repo.kv_set("project_state", "candidates_test")
    await _predictions(db, CANDIDATE, [1, 1, 1, 0, 0])  # 60% < 75%

    await _evaluator(db, cfg).update(NOW)

    done = [test for test in await _tests(db, role="candidate") if test["subject"] == CANDIDATE]
    assert done[0]["verdict"] == "unsuitable"
    assert done[0]["efficiency"] is None
    assert rank_models(await repo.done_tests()) == []


async def test_pair_test_waits_for_unchecked_prediction(db: Database) -> None:
    cfg = _settings()
    await _predictions(db, "consensus", [1, 1, 1], pair_key=PAIR_KEY, start_cluster=1)
    await _predictions(
        db, "consensus", [None], pair_key=PAIR_KEY, start_cluster=9, evaluated=False
    )

    await _evaluator(db, cfg).update(NOW)

    pair_test = (await _tests(db, role="pair"))[0]
    assert pair_test["status"] == "running"
    assert await Repository(db).kv_get("project_state") is None


async def test_new_prompt_version_creates_new_tests(db: Database) -> None:
    cfg = _settings()
    await _predictions(db, "consensus", [1, 1, 1, 0], pair_key=PAIR_KEY)
    await _evaluator(db, cfg).update(NOW)
    before = await _tests(db, version=VERSION)

    cfg.llm.prompt_version = "v3"
    await _evaluator(db, cfg).update(NOW)
    after_old = await _tests(db, version=VERSION)
    after_new = await _tests(db, version="v3")

    assert [(test["id"], test["status"]) for test in after_old] == [
        (test["id"], test["status"]) for test in before
    ]
    assert len(after_new) >= 3
    assert {test["id"] for test in after_new}.isdisjoint({test["id"] for test in before})


async def test_repeated_update_keeps_verdict_and_adds_no_duplicates(db: Database) -> None:
    cfg = _settings()
    await _predictions(db, "consensus", [1, 1, 1, 0], pair_key=PAIR_KEY)

    first = await _evaluator(db, cfg).update(NOW)
    before = next(test for test in await _tests(db) if test["role"] == "pair")
    second = await _evaluator(db, cfg).update(NOW)
    third = await _evaluator(db, cfg).update(NOW)
    after = await _tests(db)

    pair_after = next(test for test in after if test["role"] == "pair")
    assert [proposal.kind for proposal in first] == ["project_result"]
    assert second == []
    assert third == []
    assert (pair_after["verdict"], pair_after["finished_at"]) == (
        before["verdict"],
        before["finished_at"],
    )
    keys = [(test["subject"], test["role"], test["prompt_version"]) for test in after]
    assert len(keys) == len(set(keys))


async def test_project_failed_creates_no_candidates(db: Database) -> None:
    cfg = _settings()
    await Repository(db).kv_set("project_state", "project_failed")

    await _evaluator(db, cfg).update(NOW)

    assert await _tests(db, role="candidate") == []
    assert len(await _tests(db)) == 3


async def test_best_pair_proposal_after_all_candidates(db: Database) -> None:
    cfg = _settings()
    repo = Repository(db)
    await repo.kv_set("project_state", "candidates_test")
    others = [model.id for model in cfg.llm.candidates if model.id != CANDIDATE]
    for position, model_id in enumerate(others):
        await repo.get_or_create_test(model_id, "candidate", VERSION, NOW)
        test = await repo.get_or_create_test(model_id, "candidate", VERSION, NOW)
        efficiency = 0.01 + position / 100
        await repo.close_test(test["id"], 4, 80.0, 0.01, 0.02, efficiency, "suitable", NOW)
    await _predictions(db, CANDIDATE, [1, 1, 1, 1])

    proposals = await _evaluator(db, cfg).update(NOW)

    assert [proposal.kind for proposal in proposals] == ["best_pair"]
    assert CANDIDATE in proposals[0].text
    assert "deepseek/deepseek-v4-pro" in proposals[0].text
    assert PAIR_SUBJECT in [test["subject"] for test in await _tests(db, role="pair")]
