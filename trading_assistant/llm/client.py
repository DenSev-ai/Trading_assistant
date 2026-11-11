"""Клиент OpenAI-совместимого API: один HTTP-запрос к модели (CONTRACTS §3, §4)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from ..config import ModelSpec

logger = logging.getLogger(__name__)

# Коды, при которых повтор бессмысленен: проблема в ключе или доступе (CONTRACTS §4).
FATAL_STATUSES = frozenset({401, 402, 403})
# 429 и 5xx — временные сбои, их повторяем.
TRANSIENT_STATUSES = frozenset({429})
SERVER_ERROR_MIN = 500
CLIENT_ERROR_MIN = 400
SUCCESS_RANGE = range(200, 300)


@dataclass
class LLMReply:
    """Успешный ответ модели: текст, число токенов и задержка запроса."""
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class LLMError(Exception):
    """Ошибка вызова модели. kind: 'transient' (повтор), 'fatal' (стоп), 'bad' (без повтора)."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.message = message


class OpenRouterClient:
    """Тонкая обёртка над httpx.AsyncClient: POST /chat/completions с JSON-ответом."""

    def __init__(
        self, client: httpx.AsyncClient, api_key: str, base_url: str, timeout: float
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def complete(
        self,
        model: ModelSpec,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMReply:
        """Один запрос к модели; ошибки транспорта и HTTP -> LLMError с видом."""
        payload: dict[str, object] = {
            "model": model.id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if model.reasoning_effort is not None:
            payload["reasoning"] = {"effort": model.reasoning_effort}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Замер задержки — единственное разрешённое исключение из правила про Clock (STEP 3A).
        started = time.monotonic()
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMError("transient", f"таймаут {self._timeout} с") from exc
        except httpx.ConnectError as exc:
            raise LLMError("transient", f"нет связи: {exc}") from exc
        except httpx.RequestError as exc:
            raise LLMError("transient", f"ошибка транспорта: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        self._check_status(resp.status_code)

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("transient", "не удалось разобрать ответ API") from exc
        if not content:
            raise LLMError("transient", "пустой ответ модели")

        usage = data.get("usage") or {}
        reply = LLMReply(
            text=content,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )
        logger.debug(
            "модель %s ответила за %d мс, токенов %d/%d",
            model.id,
            reply.latency_ms,
            reply.tokens_in,
            reply.tokens_out,
        )
        return reply

    @staticmethod
    def _check_status(status_code: int) -> None:
        """Классифицировать HTTP-код: 2xx — ок, иначе LLMError нужного вида."""
        if status_code in FATAL_STATUSES:
            raise LLMError("fatal", f"HTTP {status_code}")
        if status_code in TRANSIENT_STATUSES or status_code >= SERVER_ERROR_MIN:
            raise LLMError("transient", f"HTTP {status_code}")
        if status_code >= CLIENT_ERROR_MIN:
            raise LLMError("bad", f"HTTP {status_code}")
        if status_code not in SUCCESS_RANGE:
            raise LLMError("transient", f"HTTP {status_code}")
