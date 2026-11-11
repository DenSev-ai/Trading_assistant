# STEP 3C — этап анализа `analyze_stage`

Прочитай: `docs/SPEC.md` (§1, §5), `docs/CONTRACTS.md` (§1, §3, §4, §5, §9). Подшаги 3A и 3B выполнены. Делай ТОЛЬКО этот подшаг.

## Создай/измени файлы
`pipeline.py` (допиши `analyze_stage`), `repository.py` (методы ниже), `__main__.py` (флаг `--once` теперь выполняет сбор и анализ). Тест: `tests/test_analyze_stage.py`.

## Методы репозитория (добавь)
`pending_clusters(since: datetime) -> list[dict]` — кластеры `status='pending'`, `created_at >= since`, порядок: `sources_count` по убыванию, затем `created_at` по убыванию;
`cluster_text(cluster_id) -> dict | None` — `title`, `fulltext`, `source`, `published_at`, `sources_count` главной статьи;
`add_model_run(...) -> int` (возвращает id, пишет `prompt_version`);
`add_prediction(cluster_id, model, ticker, direction, confidence, severity, horizon_days, expected_change_pct, reason, created_at, prompt_version, is_issued, run_id, pair_key) -> int | None` (дубль по UNIQUE игнорировать, вернуть None);
`set_cluster_status(cluster_id, status)`; `kv_get(key) -> str | None`; `kv_set(key, value)`.
При первом `kv_get('project_state')` без значения считай `'pair_test'` (запиши его через `kv_set` при старте `analyze_stage`).

## `analyze_stage(deps, now) -> AnalyzeStats`
Для каждого `pending` кластера (свежее 24 ч), с ограничением `asyncio.Semaphore(cfg.llm.concurrency)`:
1. Если `Budget.allowed(now)` False → прекратить (один раз `log.warning`), остальные кластеры остаются `pending`.
2. Собрать текст: `cluster_text`; системный промпт строится ОДИН раз за вызов этапа.
3. Параллельно (`asyncio.gather`) вызвать `run_model` для двух моделей пары. Если `kv.project_state == 'candidates_test'` и `Budget.allowed(now, reserve_share=cfg.llm.candidate_budget_share)` — ещё и для активных кандидатов: первые `cfg.llm.candidates_parallel` моделей из `cfg.llm.candidates`, для которых нет записи `model_tests` со `status='done'` и текущей `prompt_version` (в этом подшаге таблица `model_tests` может быть пуста — тогда берутся первые из списка; подшаг 4C заменит выбор).
4. Все `RunRecord` → `add_model_run`; сохрани `run_id` каждой модели.
5. Результаты кандидатов: для валидных ответов — `add_prediction` под id модели, `is_issued = is_issued(item, severity, cfg.signal)`, `run_id`, `pair_key=None`. Сбой кандидата не влияет на кластер.
6. Если обе модели пары `ok`: для каждой модели — `add_prediction` на каждый инструмент (`is_issued` по её порогам, `run_id`); затем `build_consensus` → `add_prediction(model='consensus', is_issued=1, run_id=None, pair_key='idA+idB')` (id пары по алфавиту), `confidence/severity/horizon/expected` из строки консенсуса. Кластер → `analyzed`. Пустой ответ обеих моделей — это норма: `analyzed`, ни одного прогноза, не ошибка.
7. Если любая модель пары не `ok` (не fatal) → кластер `failed`. Если `fatal` → кластер остаётся `pending`, этап прерывается.
`AnalyzeStats`: `analyzed, failed, issued_rows, consensus_rows, skipped_budget, cost_usd` (dataclass с нулями по умолчанию).
Ключ пары `pair_key` формируй функцией `make_pair_key(ids: list[str]) -> str` = `"+".join(sorted(ids))` (положи в `llm/consensus.py`).

## Тесты (FakeClock, `:memory:` БД, MockTransport; не вызывай сеть)
1) обе модели согласны и проходят пороги → 2 + 1 `predictions`, `is_issued=1`, кластер `analyzed`; 2) модели расходятся по направлению → 2 предсказания, 0 consensus, `analyzed`; 3) одна `http_error` → `failed`, consensus нет; 4) fatal (401) → кластер `pending`; 5) бюджет исчерпан → ничего не вызвано; 6) кандидат падает → кластер всё равно `analyzed`; 7) больший `sources_count` обработан первым; 8) `project_state='pair_test'` → кандидаты НЕ вызываются; 9) `candidates_test` → не более `candidates_parallel` кандидатов; 10) расход ≥ 70% лимита → кандидаты не вызываются, пара вызывается; 11) обе модели вернули пустой список → `analyzed`, 0 `predictions`; 12) модель пары с `confidence=80` при согласии по направлению → consensus нет, у неё `is_issued=0`; 13) `model_runs` содержат `prompt_version='v2'`; 14) `make_pair_key(['x-ai/b','anthropic/a'])` → `'anthropic/a+x-ai/b'`.

## Живой тест (`live`, по умолчанию пропускается)
`tests/test_live_llm.py`: реальный вызов `run_model` для каждой модели пары на короткой новости; ответ разбирается `parse_analysis`. На новости «ЦБ оставил ключевую ставку без изменений, как и ожидал рынок» `is_issued` должен быть False (печатай ответ; тест не падает от одной ошибки модели).

## Готово, когда
`pytest` и `ruff check .` без ошибок; `python -m trading_assistant --once --dry-run` выполняет сбор и анализ без исключений.
