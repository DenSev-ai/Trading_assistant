"""Тесты db.py: таблицы, инициализация, ограничения, очистка."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from trading_assistant.db import Database

CLUSTER_ROW = (
    1,
    "Кластер",
    "кластер",
    "2026-11-06T00:00:00+00:00",
    "2026-11-06T00:00:00+00:00",
    1,
    "pending",
    0,
)

PREDICTION_ROW = (
    1,
    "test-model",
    "SBER",
    "up",
    90,
    7,
    30,
    15.5,
    "причина прогноза",
    "2026-11-06T00:00:00+00:00",
    "v1",
)


async def test_seven_tables_exist(db: Database) -> None:
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {row["name"] for row in rows}
    expected = {
        "clusters",
        "articles",
        "model_runs",
        "predictions",
        "model_tests",
        "deliveries",
        "kv",
    }
    assert expected.issubset(names - {"sqlite_sequence"})


async def test_init_idempotent(db: Database) -> None:
    await db.init()  # повторный init не должен падать


async def _insert_cluster(db: Database) -> None:
    await db.execute(
        "INSERT INTO clusters (id, title, title_norm, created_at, updated_at, "
        "sources_count, status, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        CLUSTER_ROW,
    )


async def test_unique_prediction_constraint(db: Database) -> None:
    await _insert_cluster(db)
    await db.execute(
        "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
        "horizon_days, expected_change_pct, reason, created_at, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        PREDICTION_ROW,
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
            "horizon_days, expected_change_pct, reason, created_at, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            PREDICTION_ROW,
        )


async def test_cleanup_nulls_old_raw_response(db: Database) -> None:
    await _insert_cluster(db)
    old = "2020-01-01T00:00:00+00:00"
    fresh = "2026-11-06T12:00:00+00:00"
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, "
        "raw_response, created_at) VALUES (1, 'm', 'v', 'ok', 'old response', ?)",
        (old,),
    )
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, "
        "raw_response, created_at) VALUES (1, 'm', 'v', 'ok', 'fresh response', ?)",
        (fresh,),
    )
    now = datetime(2026, 11, 6, tzinfo=UTC)
    await db.cleanup(now, days=30)

    old_row = await db.fetchone("SELECT raw_response FROM model_runs WHERE created_at = ?", (old,))
    assert old_row is not None and old_row["raw_response"] is None

    fresh_row = await db.fetchone(
        "SELECT raw_response FROM model_runs WHERE created_at = ?", (fresh,)
    )
    assert fresh_row is not None and fresh_row["raw_response"] == "fresh response"
