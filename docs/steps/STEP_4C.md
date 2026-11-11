# STEP 4C — статистика, эффективность и тесты моделей (`Evaluator`)

Прочитай: `docs/SPEC.md` (§1), `docs/CONTRACTS.md` (§1, §8, §9). Подшаги 4A и 4B выполнены. Делай ТОЛЬКО этот подшаг.

## Создай/измени файлы
`trading_assistant/stats.py`, `trading_assistant/evaluation.py`; `repository.py` (методы ниже); `pipeline.py` (в конец `backtest_stage` добавь вызов `Evaluator.update(now)`); `__main__.py` (флаг `--report`); `pipeline.analyze_stage` — выбор кандидатов теперь через `model_tests` (см. ниже). Тесты: `tests/test_stats.py`, `tests/test_evaluation.py`. Эталонный файл `tests/acceptance/test_acc_evaluation.py` теперь должен ПРОХОДИТЬ — не редактируй.

## Чистые функции (эталон проверяет их)
```python
# evaluation.py
def select_sample(rows: list[dict], sample_n: int) -> list[dict] | None
def success_pct(sample: list[dict]) -> float
def classify(pct: float, min_success_pct: float) -> str     # 'suitable' | 'unsuitable'
def rank_models(tests: list[dict]) -> list[dict]            # только verdict='suitable', по efficiency по возрастанию (None — в конец)
def best_pair(ranked: list[dict]) -> tuple[dict, dict] | None
# stats.py
def efficiency(cost_issued: float, success_pct: float | None) -> float | None
def verdict(percent: float | None, n: int, sample_n: int, min_success_pct: float) -> str
```
Алгоритмы `select_sample`, `success_pct`, `classify` — буквально по `CONTRACTS.md` §9 («Чистые функции»). `best_pair`: из `ranked` выбери две модели с РАЗНЫМ префиксом `id` до символа `/` и минимальной суммой `efficiency`; нет такой пары → `None`. `verdict`: если `n >= sample_n` и `percent` не None: `'ПРОЕКТ ПРИГОДЕН'` при `percent >= min_success_pct`, иначе `'ПРОЕКТ НЕ ПРИГОДЕН'`; иначе `f'НАКОПЛЕНИЕ ДАННЫХ (n={n} из {sample_n})'`. `efficiency`: `None` при `success_pct` равном None или `<= 0`, иначе `cost_issued / success_pct`.

## Классы
```python
class Stats:
    def __init__(self, repo: Repository, signal: SignalSettings, evaluation: EvaluationSettings, now_fn)
    async def accuracy(self, model: str, ticker: str | None = None, pair_key: str | None = None,
                       prompt_version: str | None = None) -> tuple[float | None, int]
    async def cost_of_issued(self, model: str, prediction_ids: list[int] | None = None) -> float
    async def total_cost(self, model: str) -> float
    async def report(self, now: datetime) -> str
@dataclass
class Proposal: kind: str; text: str     # kind: 'replace_model' | 'best_pair' | 'project_result'
class Evaluator:

## `Evaluator.update(now)` (по шагам)
1. Определи текущую пару: `pair_key = make_pair_key([m.id for m in cfg.llm.pair])`.
2. Для субъектов (пара `pair:<pair_key>` роль `pair`; каждая модель пары роль `pair_model`; каждый кандидат из `cfg.llm.candidates` роль `candidate`, но только если `kv.project_state == 'candidates_test'`) создай запись `model_tests`, если её нет (уникальна по `subject, role, prompt_version`), `status='running'`.
3. Для каждой записи `running`: загрузи выданные прогнозы субъекта текущей `prompt_version` (пара — `model='consensus'` и `pair_key`; модель — `model=id`, `is_issued=1`), вызови `select_sample`. `None` → оставить `running`. Иначе: `pct = success_pct(sample)`, `verdict = classify(...)`, `cost_issued = stats.cost_of_issued(...)` по `id` выборки, `cost_total = stats.total_cost(...)`, `efficiency` (только при `suitable`), `status='done'`, `n=len(sample)`, `finished_at=now`. Закрытая запись больше не меняется.
4. Когда тест пары закрыт: `pct >= min_success_pct` → `kv.project_state='candidates_test'` и `Proposal('project_result', ...)` «ПРОЕКТ ПРИГОДЕН»; иначе `project_state='project_failed'` и `Proposal('project_result', ...)` «ПРОЕКТ НЕ ПРИГОДЕН». Состояние переключается один раз (повторный вызов не создаёт повторных `Proposal`).
5. Когда закрыт тест модели пары с `unsuitable`, а в `rank_models` есть `suitable` кандидат → `Proposal('replace_model', ...)`; текст: какую модель заменить, чем, с цифрами. Пару автоматически НЕ менять: `cfg` и `config/settings.yaml` не трогать.
6. Когда закрыты тесты ВСЕХ кандидатов → `Proposal('best_pair', ...)` по `best_pair(rank_models(...))`.
В `analyze_stage` (подшаг 3C) выбор активных кандидатов замени: первые `candidates_parallel` из `cfg.llm.candidates` с `model_tests.status != 'done'` для текущей `prompt_version`.
Методы репозитория: `get_or_create_test(subject, role, prompt_version, now) -> dict`, `running_tests() -> list[dict]`, `close_test(test_id, n, success_pct, cost_issued, cost_total, efficiency, verdict, now)`, `issued_predictions(model, pair_key=None, prompt_version=None) -> list[dict]` (поля `id, created_at, evaluated_at, is_successful, cluster_id, run_id`), `runs_cost(model, cluster_ids=None, only_ok=False) -> float`, `distinct_run_cost(run_ids) -> float`, `done_tests() -> list[dict]`.

## Тесты
`Stats.accuracy`: 10 оценённых выданных (7 успешных, 2 неуспешных, 1 с `is_successful=NULL`) у `consensus` → `(77.8, 9)`; невыданные не считаются; фильтры `ticker`, `pair_key`, `prompt_version` работают.
`cost_of_issued`: 3 запроса модели по 0.01, из них 2 дали выданные прогнозы (один запрос — 2 прогноза) → 0.02; `total_cost` = 0.03. Для пары: сумма `status='ok'` запросов обеих моделей по кластерам выборки.
`Evaluator.update`: 1) пара набрала 100 выданных, 78% → `project_state='candidates_test'`, `suitable`; 2) пара 70% → `project_failed`; 3) модель A 80%, модель B 70% → тест B `unsuitable`, `Proposal('replace_model')` при наличии suitable-кандидата, `cfg.llm.pair` не меняется; 4) кандидат на 99 → `running`, на 100 → `done`; 5) кандидат 74% с лучшей эффективностью → `unsuitable`, в рейтинге нет; 6) смена `prompt_version` → создаются НОВЫЕ записи, старые не трогаются; 7) повторный вызов `update` не создаёт дублей и не меняет вердикт; 8) 100 выданных, но в первых 100 есть непроверенный → тест `running`; 9) `project_failed` → кандидаты не создаются.
`rank_models`/`best_pair`: две модели `openai/a`, `openai/b` не образуют пару; из трёх выбираются две с разными разработчиками и минимальной суммой.
`report` на пустой БД не падает, печатает «Нет проверенных прогнозов».

## Готово, когда
`pytest` и `ruff check .` без ошибок; `tests/acceptance/test_acc_evaluation.py` проходит; `python -m trading_assistant --report` работает на пустой БД.

    def __init__(self, repo: Repository, stats: Stats, cfg: AppSettings, now_fn)
    async def update(self, now: datetime) -> list[Proposal]
```
`accuracy`: считает записи `predictions` с `is_issued=1 AND evaluated_at IS NOT NULL AND is_successful IS NOT NULL` и фильтрами по аргументам; процент = `100 * SUM(is_successful) / n`; нет записей → `(None, 0)`. Округляй до одной десятой при выводе, не при расчёте. `cost_of_issued` и `total_cost` — по `CONTRACTS.md` §8. `report` — текст `--report`: состояние проекта (`kv.project_state`), таблица «модель | роль | n | сбылось % | стоимость выданных | весь расход | эффективность | вердикт», строка `verdict(...)` для пары, прогресс «проверено X из N»; нет данных → «Нет проверенных прогнозов».
