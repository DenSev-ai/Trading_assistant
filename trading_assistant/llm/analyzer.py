"""Промпт, разбор ответа модели и один вызов модели с повторами (CONTRACTS §3, §4)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError

from ..config import LLMSettings, ModelSpec, SignalSettings
from ..instruments import InstrumentRegistry
from ..models import LLMAnalysis, LLMInstrument
from . import budget
from .client import LLMError, OpenRouterClient

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class RunRecord:
    """Один HTTP-запрос к модели — строка таблицы model_runs."""
    model: str
    status: str
    raw_response: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int


@dataclass
class ModelOutcome:
    """Итог одного вызова модели: удался ли и сколько стоил."""
    model: str
    status: str
    analysis: LLMAnalysis | None
    cost_usd: float


def build_system_prompt(
    template: str, registry: InstrumentRegistry, signal: SignalSettings
) -> str:
    """Подставить в шаблон промпта список инструментов, срок и минимальное движение."""
    return template.format(
        instruments=registry.prompt_block(),
        max_horizon=signal.max_horizon_days,
        min_move=f"{signal.min_move_pct:g}",
    )


def build_user_message(
    title: str,
    text: str,
    sources_count: int,
    max_chars: int,
    published_msk: str,
    source: str,
) -> str:
    """Пользовательское сообщение с метаданными новости и её текстом в маркерах."""
    return (
        f"Дата публикации (МСК): {published_msk}\n"
        f"Источник: {source}\n"
        f"Источников в кластере: {sources_count}\n"
        "<<<NEWS_DATA>>>\n"
        f"{title}\n\n"
        f"{text[:max_chars]}\n"
        "<<<END_NEWS_DATA>>>"
    )


def parse_analysis(raw: str, registry: InstrumentRegistry) -> LLMAnalysis:
    """Разобрать ответ: снять ограждения, взять JSON, проверить схему, отбросить чужие тикеры."""
    cleaned = raw.replace("```json", "").replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("в ответе модели нет JSON-объекта")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"ответ модели не JSON: {exc}") from exc
    try:
        analysis = LLMAnalysis.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"ответ модели не соответствует схеме: {exc}") from exc

    instruments: list[LLMInstrument] = []
    for item in analysis.instruments:
        if registry.get(item.ticker) is None:
            logger.warning("модель назвала неизвестный тикер %r — отброшен", item.ticker)
            continue
        instruments.append(item)
    return LLMAnalysis(severity=analysis.severity, instruments=instruments)


def _empty_record(model_id: str, status: str) -> RunRecord:
    """Запись о неудачном HTTP-запросе: без ответа, токенов и стоимости."""
    return RunRecord(
        model=model_id,
        status=status,
        raw_response=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0,
    )


def _total_cost(records: list[RunRecord]) -> float:
    """Суммарная стоимость всех запросов одного вызова модели."""
    return sum(record.cost_usd for record in records)


async def run_model(
    client: OpenRouterClient,
    model: ModelSpec,
    system: str,
    user: str,
    cfg: LLMSettings,
    registry: InstrumentRegistry,
    sleep: Sleep = asyncio.sleep,
) -> tuple[ModelOutcome, list[RunRecord]]:
    """Вызвать модель с повторами; вернуть итог и запись о каждом HTTP-запросе."""
    records: list[RunRecord] = []
    pauses = cfg.retry_pauses_sec
    # Основной запрос + повтор на каждую паузу: при [1, 30] это 3 запроса (CONTRACTS §4).
    attempts = 1 + len(pauses)

    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            reply = await client.complete(model, system, user, cfg.temperature, cfg.max_tokens)
        except LLMError as exc:
            if exc.kind == "fatal":
                logger.error("модель %s: фатальная ошибка (%s)", model.id, exc)
                records.append(_empty_record(model.id, "fatal"))
                return ModelOutcome(model.id, "fatal", None, _total_cost(records)), records
            if exc.kind == "bad":
                logger.warning("модель %s: отказ без повтора (%s)", model.id, exc)
                records.append(_empty_record(model.id, "http_error"))
                return ModelOutcome(model.id, "http_error", None, _total_cost(records)), records
            logger.warning(
                "модель %s: временная ошибка (%s), попытка %d из %d",
                model.id,
                exc,
                attempt + 1,
                attempts,
            )
            records.append(_empty_record(model.id, "http_error"))
            if not is_last:
                await sleep(pauses[attempt])
            continue

        cost = budget.cost_usd(reply.tokens_in, reply.tokens_out, model)
        try:
            analysis = parse_analysis(reply.text, registry)
        except ValueError as exc:
            logger.warning(
                "модель %s: невалидный ответ (%s), попытка %d из %d",
                model.id,
                exc,
                attempt + 1,
                attempts,
            )
            records.append(
                RunRecord(
                    model=model.id,
                    status="invalid_json",
                    raw_response=reply.text,
                    tokens_in=reply.tokens_in,
                    tokens_out=reply.tokens_out,
                    cost_usd=cost,
                    latency_ms=reply.latency_ms,
                )
            )
            if not is_last:
                await sleep(pauses[attempt])
            continue

        records.append(
            RunRecord(
                model=model.id,
                status="ok",
                raw_response=reply.text,
                tokens_in=reply.tokens_in,
                tokens_out=reply.tokens_out,
                cost_usd=cost,
                latency_ms=reply.latency_ms,
            )
        )
        return ModelOutcome(model.id, "ok", analysis, _total_cost(records)), records

    # Все попытки исчерпаны: итоговый статус — как у последней.
    return ModelOutcome(model.id, records[-1].status, None, _total_cost(records)), records


