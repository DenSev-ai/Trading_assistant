"""Стоимость вызова модели по токенам и ценам из конфига (CONTRACTS §4)."""
from __future__ import annotations

from ..config import ModelSpec

# Цены в конфиге заданы за 1 млн токенов.
TOKENS_PER_PRICE_UNIT = 1e6


def cost_usd(tokens_in: int, tokens_out: int, model: ModelSpec) -> float:
    """Стоимость запроса: вход/1e6*price_in + выход/1e6*price_out."""
    return (
        tokens_in / TOKENS_PER_PRICE_UNIT * model.price_in
        + tokens_out / TOKENS_PER_PRICE_UNIT * model.price_out
    )
