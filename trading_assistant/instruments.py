"""Реестр торговых инструментов из config/instruments.json."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .config import ConfigError


class Instrument(BaseModel):
    """Один инструмент: поля как в config/instruments.json."""
    ticker: str
    name: str
    type: str
    secid: str
    board: str
    market: str
    aliases: list[str]
    enabled: bool


class InstrumentRegistry:
    def __init__(self, items: list[Instrument]) -> None:
        self._items = items
        self._by_ticker = {item.ticker: item for item in items}

    @classmethod
    def load(cls, path: Path) -> InstrumentRegistry:
        """Загрузить реестр из JSON. Битый файл -> ConfigError."""
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"не прочитать файл инструментов {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"не распарсить JSON инструментов {path}: {exc}") from exc
        try:
            items = [Instrument.model_validate(item) for item in data]
        except (ValidationError, TypeError) as exc:
            raise ConfigError(f"инструменты в {path} не прошли валидацию: {exc}") from exc
        return cls(items)

    def get(self, ticker: str) -> Instrument | None:
        """Инструмент по тикеру, только если enabled. Иначе None."""
        item = self._by_ticker.get(ticker)
        if item is None or not item.enabled:
            return None
        return item

    def enabled(self) -> list[Instrument]:
        """Все включённые инструменты."""
        return [item for item in self._items if item.enabled]

    def prompt_block(self) -> str:
        """Строки для промпта модели: 'TICKER — Имя (синонимы: ...)'. Только enabled."""
        lines = [
            f"{item.ticker} — {item.name} (синонимы: {', '.join(item.aliases)})"
            for item in self.enabled()
        ]
        return "\n".join(lines)
