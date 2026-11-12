"""Клиент MOEX ISS: последняя цена и дневные свечи (CONTRACTS §6, SPEC §8)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from ..clock import Clock, SystemClock
from ..instruments import Instrument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    """Дневная свеча: дата, максимум, минимум, цена закрытия."""
    date: date
    high: float
    low: float
    close: float


def _rows(payload: dict, block: str) -> list[dict]:
    """Блок ответа ISS -> список строк в виде словарей по именам колонок."""
    section = payload.get(block) or {}
    columns = section.get("columns") or []
    data = section.get("data") or []
    return [dict(zip(columns, row, strict=False)) for row in data]


def parse_candles(payload: dict) -> list[Candle]:
    """Разобрать блок candles в свечи по возрастанию даты; строки с None пропускаются."""
    candles: list[Candle] = []
    for row in _rows(payload, "candles"):
        begin = row.get("begin")
        high = row.get("high")
        low = row.get("low")
        close = row.get("close")
        if not begin or high is None or low is None or close is None:
            continue
        candles.append(
            Candle(
                date=date.fromisoformat(str(begin)[:10]),
                high=float(high),
                low=float(low),
                close=float(close),
            )
        )
    candles.sort(key=lambda candle: candle.date)
    return candles


def parse_last_price(payload: dict, is_index: bool) -> float | None:
    """Взять LAST для акции или CURRENTVALUE для индекса из блока marketdata."""
    column = "CURRENTVALUE" if is_index else "LAST"
    for row in _rows(payload, "marketdata"):
        value = row.get(column)
        if value is not None:
            return float(value)
    return None


class MoexClient:
    """Запросы к MOEX ISS: последняя цена и дневные свечи по инструменту."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://iss.moex.com",
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._clock = clock or SystemClock()

    def _board_url(self, ins: Instrument) -> str:
        """URL режима торгов бумаги: движок, рынок, режим (до /securities/...)."""
        return (
            f"{self._base_url}/iss/engines/stock/markets/{ins.market}"
            f"/boards/{ins.board}"
        )

    async def _get(self, url: str, params: dict[str, str | int]) -> dict | None:
        """GET с параметрами; ошибка сети, не 200 или не JSON -> лог и None."""
        try:
            resp = await self._client.get(url, params=params)
        except httpx.RequestError as exc:
            logger.warning("MOEX ISS недоступен (%s): %s", url, exc)
            return None
        if resp.status_code != 200:
            logger.warning("MOEX ISS вернул %s для %s", resp.status_code, url)
            return None
        try:
            return resp.json()
        except ValueError:
            logger.warning("MOEX ISS вернул не JSON для %s", url)
            return None

    async def daily_candles(self, ins: Instrument, start: date, end: date) -> list[Candle]:
        """Дневные свечи за период start..end; любая ошибка -> пустой список."""
        url = f"{self._board_url(ins)}/securities/{ins.secid}/candles.json"
        params: dict[str, str | int] = {
            "from": start.isoformat(),
            "till": end.isoformat(),
            "interval": 24,
            "iss.meta": "off",
        }
        payload = await self._get(url, params)
        if payload is None:
            return []
        return parse_candles(payload)

    async def last_price(self, ins: Instrument) -> float | None:
        """Последняя цена; если поле пустое — закрытие последней свечи за 7 дней."""
        url = f"{self._board_url(ins)}/securities/{ins.secid}.json"
        params: dict[str, str | int] = {
            "iss.meta": "off",
            "iss.only": "marketdata",
            "marketdata.columns": "SECID,LAST,CURRENTVALUE",
        }
        payload = await self._get(url, params)
        if payload is not None:
            price = parse_last_price(payload, ins.type == "index")
            if price is not None:
                return price
        today = self._clock.now().date()
        candles = await self.daily_candles(ins, today - timedelta(days=7), today)
        if not candles:
            return None
        return candles[-1].close
