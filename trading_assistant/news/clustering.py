"""Кластеризация новостей по сходству заголовков."""
from __future__ import annotations

from rapidfuzz import fuzz


def find_cluster(title_norm: str, candidates: list[tuple[int, str]], threshold: int) -> int | None:
    """Найти ближайший кластер по token_set_ratio. Лучший >= threshold, иначе None."""
    best_id: int | None = None
    best_score = 0
    for cluster_id, candidate_norm in candidates:
        score = fuzz.token_set_ratio(title_norm, candidate_norm)
        if score > best_score:
            best_score = score
            best_id = cluster_id
    if best_id is not None and best_score >= threshold:
        return best_id
    return None
