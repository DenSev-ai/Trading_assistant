"""Тесты clustering.py: find_cluster."""
from __future__ import annotations

from trading_assistant.news.clustering import find_cluster


def test_find_cluster_similar() -> None:
    candidates = [(1, "центробанк повысил ставку до 17%")]
    assert find_cluster("цб повысил ставку до 17%", candidates, 75) == 1


def test_find_cluster_not_similar() -> None:
    candidates = [(1, "цб повысил ставку до 17%")]
    assert find_cluster("в москве открылся парк", candidates, 75) is None


def test_find_cluster_empty() -> None:
    assert find_cluster("любой заголовок", [], 75) is None


def test_find_cluster_best_below_threshold() -> None:
    candidates = [(1, "совсем другая новость о погоде")]
    assert find_cluster("цб повысил ставку", candidates, 75) is None
