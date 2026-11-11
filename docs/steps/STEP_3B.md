# STEP 3B — выдача прогноза, консенсус, приоритет, бюджет

Прочитай: `docs/SPEC.md` (§1), `docs/CONTRACTS.md` (§1, §4, §5). Подшаг 3A уже выполнен. Делай ТОЛЬКО этот подшаг.

## Создай/измени файлы
`trading_assistant/llm/consensus.py`; в `trading_assistant/llm/budget.py` ДОПИШИ класс `Budget` (функцию `cost_usd` не трогай); в `repository.py` добавь `spent_since(since_utc: datetime) -> float`. Тесты: `tests/test_consensus.py`, `tests/test_budget.py`. Эталонные тесты `tests/acceptance/test_acc_consensus.py` теперь должны проходить (раньше они пропускались) — их НЕ редактируй.

## Сигнатуры
```python
# llm/consensus.py
@dataclass(frozen=True)
class ConsensusRow: ticker: str; direction: str; confidence: int; severity: int; horizon_days: int; expected_change_pct: float; reason: str
def is_issued(item: LLMInstrument, severity: int, signal: SignalSettings) -> bool
def build_consensus(a: LLMAnalysis, b: LLMAnalysis, signal: SignalSettings) -> list[ConsensusRow]
def is_priority(row: ConsensusRow, signal: SignalSettings) -> bool

# llm/budget.py
class Budget:
    def __init__(self, repo: Repository, daily_limit: float, tz: ZoneInfo)
    async def spent_today(self, now: datetime) -> float                      # сумма model_runs.cost_usd с начала суток МСК (в UTC)
    async def allowed(self, now: datetime, reserve_share: float = 1.0) -> bool   # spent_today < daily_limit * reserve_share
```

## Алгоритмы (буквально)
**`is_issued`:** True тогда и только тогда, когда одновременно `item.confidence >= signal.min_confidence`, `severity >= signal.min_severity`, `item.expected_change_pct >= signal.min_move_pct`, `1 <= item.horizon_days <= signal.max_horizon_days`.
**`build_consensus`:** для каждого инструмента `x` из `a`, у которого в `b` есть инструмент `y` с тем же `ticker`:
1. оба должны пройти `is_issued` (каждый со своей `severity` из своего ответа), иначе пропустить;
2. `x.direction != y.direction` → пропустить;
3. `lo = min(m1, m2)`, `hi = max(m1, m2)` (m — `expected_change_pct`); `(hi - lo) / lo > signal.magnitude_tolerance` → пропустить (допусти погрешность `1e-9`: 15 и 18 проходят, 15 и 18.01 нет);
4. строка: `direction` общий, `confidence = min`, `severity = min(severity_a, severity_b)`, `horizon_days = max(h1, h2)`, `expected_change_pct = lo`, `reason` — от модели с меньшим `confidence` (при равенстве — от `x`).
Сроки моделей между собой НЕ сравниваются.
**`is_priority`:** `expected_change_pct > signal.priority_move_pct` ИЛИ (`expected_change_pct >= signal.min_move_pct` И `horizon_days < signal.priority_fast_days`).
**`Budget.spent_today`:** начало суток = полночь МСК текущей даты, переведённая в UTC; сумма `cost_usd` из `model_runs` с `created_at >= начало`.

## Тесты
Используй таблицы из `CONTRACTS.md` §5 (7 строк) и дополнительно: граничные значения `is_issued` (`confidence` 84/85, `severity` 5/6, `expected_change_pct` 14.9/15, `horizon_days` 180/181); `is_priority`: 25.0 при h=150 → False, 25.1 → True, 15 при h=119 → True, 15 при h=120 → False; два тикера, один совпал → 1 строка; `severity` берётся минимальный.
`Budget`: лимит 0.50: расход 0.49 → allowed, 0.50 → не allowed; расход вчера (МСК) не учитывается (`FakeClock`); `reserve_share=0.7`: 0.34 → allowed, 0.35 → не allowed.

## Готово, когда
`pytest` и `ruff check .` без ошибок; файл `tests/acceptance/test_acc_consensus.py` проходит (не пропускается).
