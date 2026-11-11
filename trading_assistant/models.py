"""Pydantic-схемы ответа LLM (CONTRACTS §3) и датаклассы предметной области."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, field_validator


class LLMInstrument(BaseModel):
    """Один инструмент из ответа модели: тикер, направление, оценка."""
    ticker: str
    direction: str
    confidence: int
    horizon_days: int
    expected_change_pct: float
    reason: str

    @field_validator("direction")
    @classmethod
    def _direction_lower(cls, v: str) -> str:
        return v.lower()

    @field_validator("reason")
    @classmethod
    def _reason_trim(cls, v: str) -> str:
        return v[:300]

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError("confidence must be in 0..100")
        return v

    @field_validator("horizon_days")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("horizon_days must be >= 1")
        return v

    @field_validator("expected_change_pct")
    @classmethod
    def _expected_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("expected_change_pct must be > 0")
        return v


class LLMAnalysis(BaseModel):
    """Весь ответ модели: важность новости и список инструментов."""
    severity: int
    instruments: list[LLMInstrument]

    @field_validator("severity")
    @classmethod
    def _severity_range(cls, v: int) -> int:
        if not 0 <= v <= 10:
            raise ValueError("severity must be in 0..10")
        return v


@dataclass
class Article:
    id: int | None
    url: str
    source: str
    title: str
    title_norm: str
    summary: str | None
    published_at: str
    fetched_at: str
    fulltext: str | None
    status: str
    cluster_id: int | None


@dataclass
class Cluster:
    id: int | None
    title: str
    title_norm: str
    created_at: str
    updated_at: str
    sources_count: int
    main_article_id: int | None
    status: str
    attempts: int


@dataclass
class Prediction:
    id: int | None
    cluster_id: int
    model: str
    ticker: str
    direction: str
    confidence: int
    severity: int
    horizon_days: int
    expected_change_pct: float
    reason: str
    created_at: str
    prompt_version: str
    pair_key: str | None
    is_issued: int
    run_id: int | None
    base_price: float | None
    check_date: str | None
    check_price: float | None
    max_move_pct: float | None
    target_hit: int | None
    is_successful: int | None
    evaluated_at: str | None
