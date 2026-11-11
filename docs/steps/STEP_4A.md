# STEP 4A — MOEX: свечи, цена, проверка прогноза `evaluate`

Прочитай: `docs/SPEC.md` (§5, §8), `docs/CONTRACTS.md` (§6). Предыдущие шаги выполнены. Делай ТОЛЬКО этот подшаг.

## Создай файлы
`trading_assistant/market/__init__.py` (пустой), `market/moex.py`, `trading_assistant/backtest.py`. Тесты: `tests/test_moex.py`, `tests/test_backtest.py`, фикстуры `tests/fixtures/moex_candles.json`, `tests/fixtures/moex_marketdata.json`. Эталонный файл `tests/acceptance/test_acc_backtest.py` теперь должен ПРОХОДИТЬ — не редактируй его.

## Сигнатуры
```python
# market/moex.py
@dataclass(frozen=True)
class Candle: date: date; high: float; low: float; close: float
class MoexClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str = "https://iss.moex.com")
    async def last_price(self, ins: Instrument) -> float | None
    async def daily_candles(self, ins: Instrument, start: date, end: date) -> list[Candle]
def parse_candles(payload: dict) -> list[Candle]
def parse_last_price(payload: dict, is_index: bool) -> float | None

# backtest.py
@dataclass
class EvalResult: check_date: date | None; check_price: float | None; max_move_pct: float | None; target_hit: int | None; is_successful: int | None
def evaluate(direction: str, horizon_days: int, expected_pct: float, created_msk: date,
             base_price: float | None, candles: list[Candle], today_msk: date,
             min_move_pct: float) -> EvalResult | None
```
Имена аргументов `evaluate` менять нельзя (эталонные тесты вызывают их по имени).

## Алгоритмы
**`parse_candles`:** `payload["candles"]["columns"]` — список имён, `payload["candles"]["data"]` — список строк. Находи индексы колонок `begin`, `high`, `low`, `close` по ИМЕНИ, не по позиции. `date` = первые 10 символов `begin` (формат `YYYY-MM-DD`). Строки с `None` в `high`, `low` или `close` пропускай. Результат — по возрастанию даты.
**`daily_candles`:** `GET {base_url}/iss/engines/stock/markets/{market}/boards/{board}/securities/{secid}/candles.json?from={start}&till={end}&interval=24&iss.meta=off`. Для индексов используй `ins.market` и `ins.board` из реестра (они уже `index` и `SNDX`). Любая ошибка сети или код не 200 → вернуть `[]` и записать `log.warning`, исключений наружу не пускать. Таймаут — из переданного клиента.
**`last_price`:** `GET .../securities/{secid}.json?iss.meta=off&iss.only=marketdata&marketdata.columns=SECID,LAST,CURRENTVALUE`. Акции — колонка `LAST`; индексы (`ins.type == "index"`) — `CURRENTVALUE`. Значение `None`/нет колонки → попробуй `daily_candles` за последние 7 дней и верни `close` последней свечи; нет данных → `None`. Ошибки сети → `None`.
**`evaluate`:** реализуй точно по `CONTRACTS.md` §6 (8 шагов алгоритма). Движение: `up` → `(high - base) * 100.0 / base`; `down` → `(base - low) * 100.0 / base` (именно такой порядок действий). День создания в окно НЕ входит. Окно: `created_msk < date <= created_msk + horizon_days`.

## Тесты
Все строки таблицы в `CONTRACTS.md` §6 и дополнительно: `parse_candles` — порядок колонок в фикстуре отличается от реального; строка с `None` в `high` пропускается; `parse_last_price`: акция → `LAST`, индекс → `CURRENTVALUE`, `null` → `None`; `MoexClient` (MockTransport): код 500 → `[]`/`None` без исключения; в URL есть `iss.meta=off` и (для last_price) `iss.only=marketdata`; пустой `LAST` → берётся `close` последней свечи.

## Готово, когда
`pytest` и `ruff check .` без ошибок; `tests/acceptance/test_acc_backtest.py` проходит (не пропускается).
