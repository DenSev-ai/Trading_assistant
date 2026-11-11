"""Эталонные тесты CONTRACTS §8–§9 (выборка теста, вердикт, эффективность). Не править в шагах."""
import pytest

evaluation = pytest.importorskip("trading_assistant.evaluation")
stats = pytest.importorskip("trading_assistant.stats")


def row(i, created, ok, evaluated=True):
    return {
        "id": i,
        "created_at": created,
        "evaluated_at": "2026-12-01T00:00:00+00:00" if evaluated else None,
        "is_successful": ok,
    }


def make(n, start=0):
    return [row(start + i, f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00", 1) for i in range(n)]


def test_sample_needs_enough_rows():
    assert evaluation.select_sample(make(99), 100) is None


def test_sample_waits_for_unevaluated_among_first_100():
    rows = make(100)
    rows[50] = row(50, rows[50]["created_at"], None, evaluated=False)
    assert evaluation.select_sample(rows, 100) is None


def test_fast_successes_cannot_push_out_slow_ones():
    first = make(100)
    first[10] = row(10, first[10]["created_at"], None, evaluated=False)  # медленный, не проверен
    later = [row(1000 + i, f"2026-02-01T00:00:{i:02d}+00:00", 1) for i in range(40)]
    assert evaluation.select_sample(first + later, 100) is None


def test_unknown_result_is_replaced_by_next_in_time():
    rows = make(101)
    rows[5] = row(5, rows[5]["created_at"], None, evaluated=True)  # нет данных MOEX
    sample = evaluation.select_sample(rows, 100)
    assert sample is not None and len(sample) == 100
    assert all(r["is_successful"] is not None for r in sample)
    assert 5 not in [r["id"] for r in sample]


def test_sample_is_ordered_by_creation_time():
    rows = list(reversed(make(100)))
    sample = evaluation.select_sample(rows, 100)
    assert [r["id"] for r in sample] == list(range(100))


def test_success_pct_and_classify_boundary():
    rows = make(100)
    for i in range(25):
        rows[i] = row(i, rows[i]["created_at"], 0)
    sample = evaluation.select_sample(rows, 100)
    assert evaluation.success_pct(sample) == 75.0
    assert evaluation.classify(75.0, 75) == "suitable"
    assert evaluation.classify(74.99, 75) == "unsuitable"


def test_efficiency():
    assert stats.efficiency(0.5, 80.0) == pytest.approx(0.00625)
    assert stats.efficiency(0.5, 0.0) is None
    assert stats.efficiency(0.5, None) is None
    assert stats.efficiency(0.0, 80.0) == 0.0


def test_verdict_texts():
    assert stats.verdict(80.0, 100, 100, 75) == "ПРОЕКТ ПРИГОДЕН"
    assert stats.verdict(75.0, 100, 100, 75) == "ПРОЕКТ ПРИГОДЕН"
    assert stats.verdict(74.0, 100, 100, 75) == "ПРОЕКТ НЕ ПРИГОДЕН"
    assert stats.verdict(90.0, 40, 100, 75) == "НАКОПЛЕНИЕ ДАННЫХ (n=40 из 100)"
    assert stats.verdict(None, 0, 100, 75) == "НАКОПЛЕНИЕ ДАННЫХ (n=0 из 100)"
