"""Тесты run_model и cost_usd: повторы, статусы, стоимость (CONTRACTS §4)."""
from __future__ import annotations

import httpx
import pytest

from trading_assistant.config import LLMSettings, ModelSpec
from trading_assistant.llm.analyzer import ModelOutcome, RunRecord, run_model
from trading_assistant.llm.budget import cost_usd
from trading_assistant.llm.client import OpenRouterClient

BASE_URL = "https://example.invalid/api/v1"
VALID = (
    '{"severity":8,"instruments":[{"ticker":"SBER","direction":"up","confidence":90,'
    '"horizon_days":60,"expected_change_pct":18,"reason":"x"}]}'
)


def _cfg() -> LLMSettings:
    """Настройки llm из config/settings.yaml в объёме, нужном run_model."""
    return LLMSettings(
        base_url=BASE_URL,
        prompt_version="v2",
        temperature=0.1,
        max_tokens=3000,
        candidate_budget_share=0.7,
        max_text_chars=6000,
        timeout_sec=60,
        retry_pauses_sec=[1, 30],
        concurrency=2,
        daily_budget_usd=0.50,
        pair=[
            ModelSpec(
                id="anthropic/claude-sonnet-5.5",
                price_in=2.0,
                price_out=10.0,
                reasoning_effort="low",
            ),
            ModelSpec(id="x-ai/grok-4.7", price_in=2.0, price_out=6.0, reasoning_effort="low"),
        ],
        candidates_parallel=2,
        candidates=[ModelSpec(id="google/gemma-4-31b-it:free", price_in=0, price_out=0)],
    )


class _Server:
    """Поддельный API: отдаёт ответы из очереди и запоминает все запросы."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("лишний HTTP-запрос: повторы не остановились")
        return self.responses.pop(0)


def _ok(content: str) -> httpx.Response:
    """Успешный ответ API с usage (1000 входных и 500 выходных токенов)."""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )


def _status(code: int) -> httpx.Response:
    return httpx.Response(code, json={"error": "x"})


async def _run(
    server: _Server, registry, spec: ModelSpec | None = None
) -> tuple[ModelOutcome, list[RunRecord], list[float]]:
    """Выполнить run_model и вернуть итог, записи и список пауз."""
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    cfg = _cfg()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    try:
        llm = OpenRouterClient(client, "secret-key", cfg.base_url, cfg.timeout_sec)
        outcome, records = await run_model(
            llm, spec or cfg.pair[0], "system", "user", cfg, registry, sleep=sleep
        )
    finally:
        await client.aclose()
    return outcome, records, pauses


async def test_single_success(registry) -> None:
    server = _Server([_ok(VALID)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 1
    assert outcome.status == "ok"
    assert outcome.analysis is not None
    assert [item.ticker for item in outcome.analysis.instruments] == ["SBER"]
    assert [record.status for record in records] == ["ok"]
    assert records[0].raw_response == VALID
    assert pauses == []
    assert outcome.cost_usd == pytest.approx(0.007)


async def test_two_errors_then_success(registry) -> None:
    server = _Server([_status(500), _status(500), _ok(VALID)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 3
    assert pauses == [1, 30]
    assert [record.status for record in records] == ["http_error", "http_error", "ok"]
    assert outcome.status == "ok"
    assert outcome.cost_usd == pytest.approx(sum(record.cost_usd for record in records))


async def test_http_errors_exhausted(registry) -> None:
    server = _Server([_status(500), _status(500), _status(500)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 3
    assert [record.status for record in records] == ["http_error"] * 3
    assert outcome.status == "http_error"
    assert outcome.analysis is None
    assert outcome.cost_usd == 0.0
    assert pauses == [1, 30]


async def test_invalid_json_exhausted(registry) -> None:
    server = _Server([_ok("not json"), _ok("not json"), _ok("not json")])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 3
    assert [record.status for record in records] == ["invalid_json"] * 3
    assert outcome.status == "invalid_json"
    assert outcome.analysis is None
    assert records[0].raw_response == "not json"
    assert outcome.cost_usd == pytest.approx(0.021)
    assert pauses == [1, 30]


async def test_fatal_without_retry(registry) -> None:
    server = _Server([_status(401)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 1
    assert outcome.status == "fatal"
    assert [record.status for record in records] == ["fatal"]
    assert pauses == []


async def test_bad_request_without_retry(registry) -> None:
    server = _Server([_status(400), _ok(VALID)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 1
    assert outcome.status == "http_error"
    assert [record.status for record in records] == ["http_error"]
    assert pauses == []


async def test_rate_limit_then_success(registry) -> None:
    server = _Server([_status(429), _ok(VALID)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 2
    assert outcome.status == "ok"
    assert [record.status for record in records] == ["http_error", "ok"]
    assert pauses == [1]


async def test_null_content_exhausted(registry) -> None:
    null_body = {"choices": [{"message": {"content": None}}]}
    server = _Server([httpx.Response(200, json=null_body) for _ in range(3)])
    outcome, records, pauses = await _run(server, registry)
    assert len(server.requests) == 3
    assert [record.status for record in records] == ["http_error"] * 3
    assert outcome.status == "http_error"
    assert pauses == [1, 30]


def test_cost_usd_formula() -> None:
    spec = ModelSpec(id="test/model", price_in=0.1, price_out=0.5)
    assert cost_usd(1000, 500, spec) == pytest.approx(0.00035)


def test_cost_usd_free_model() -> None:
    spec = ModelSpec(id="google/gemma-4-31b-it:free", price_in=0, price_out=0)
    assert cost_usd(1_000_000, 1_000_000, spec) == 0.0

