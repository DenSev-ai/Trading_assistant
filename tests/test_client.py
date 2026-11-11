"""Тесты OpenRouterClient на httpx.MockTransport (CONTRACTS §3, §4)."""
from __future__ import annotations

import json

import httpx
import pytest

from trading_assistant.config import ModelSpec
from trading_assistant.llm.client import LLMError, LLMReply, OpenRouterClient

BASE_URL = "https://example.invalid/api/v1"
CONTENT = '{"severity": 1, "instruments": []}'


def model(**kw: object) -> ModelSpec:
    """Модель пары из config/settings.yaml; аргументы переопределяют поля."""
    data: dict[str, object] = {
        "id": "anthropic/claude-sonnet-5.5",
        "price_in": 2.0,
        "price_out": 10.0,
    }
    data.update(kw)
    return ModelSpec(**data)  # type: ignore[arg-type]


def ok_body(*, usage: bool = True) -> dict:
    """Тело успешного ответа OpenAI-совместимого API."""
    body: dict = {"choices": [{"message": {"content": CONTENT}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
    return body


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _reply(handler, spec: ModelSpec | None = None) -> tuple[LLMReply, httpx.Request]:
    """Успешный вызов: вернуть ответ модели и отправленный запрос."""
    captured: list[httpx.Request] = []

    def inner(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    client = _client(inner)
    try:
        llm = OpenRouterClient(client, "secret-key", BASE_URL, timeout=5.0)
        reply = await llm.complete(spec or model(), "system prompt", "user prompt", 0.1, 1234)
    finally:
        await client.aclose()
    return reply, captured[0]


async def _error(handler, spec: ModelSpec | None = None) -> LLMError:
    """Неуспешный вызов: вернуть поднятую ошибку."""
    client = _client(handler)
    try:
        llm = OpenRouterClient(client, "secret-key", BASE_URL, timeout=5.0)
        with pytest.raises(LLMError) as info:
            await llm.complete(spec or model(), "system prompt", "user prompt", 0.1, 1234)
    finally:
        await client.aclose()
    return info.value


async def test_success_reply_and_request_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body())

    reply, request = await _reply(handler)
    assert reply.text == CONTENT
    assert (reply.tokens_in, reply.tokens_out) == (100, 20)
    assert reply.latency_ms >= 0

    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 1234
    assert body["response_format"] == {"type": "json_object"}
    assert "reasoning" not in body


async def test_reasoning_only_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body())

    _, request = await _reply(handler, model(reasoning_effort="low"))
    assert json.loads(request.content)["reasoning"] == {"effort": "low"}


async def test_missing_usage_gives_zero_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body(usage=False))

    reply, _ = await _reply(handler)
    assert (reply.tokens_in, reply.tokens_out) == (0, 0)


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "fatal"),
        (402, "fatal"),
        (403, "fatal"),
        (429, "transient"),
        (500, "transient"),
        (503, "transient"),
        (400, "bad"),
        (404, "bad"),
    ],
)
async def test_http_status_classification(status: int, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    assert (await _error(handler)).kind == kind


@pytest.mark.parametrize("content", [None, ""])
async def test_empty_content_is_transient(content: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    assert (await _error(handler)).kind == "transient"


async def test_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    assert (await _error(handler)).kind == "transient"


async def test_connect_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert (await _error(handler)).kind == "transient"
