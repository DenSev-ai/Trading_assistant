# STEP 4E — сквозная проверка шагов 3–4 (без нового кода логики)

Прочитай: `docs/SPEC.md` (§1, §5), `docs/CONTRACTS.md` (§5, §6, §9). Подшаги 3A–3C и 4A–4D выполнены. Делай ТОЛЬКО этот подшаг. Новую бизнес-логику НЕ пиши: только интеграционный тест и исправление найденных расхождений.

## Создай файл
`tests/test_end_to_end.py` — один сквозной сценарий на `:memory:` БД, `FakeClock`, `httpx.MockTransport` (OpenRouter и MOEX), без сети.

## Сценарий (шаги теста)
1. Подготовь реестр (SBER), конфиг из `config/settings.yaml` с `evaluation.sample_n = 3` и `min_success_pct = 75` (подмена в тесте).
2. В БД положи 3 кластера `pending` с текстами новостей.
3. MockTransport OpenRouter: обе модели пары на каждый кластер отвечают `up`, SBER, `confidence` 90, `horizon_days` 60, `expected_change_pct` 18 и 17 (расхождение < 20%), `severity` 8.
4. Выполни `analyze_stage` → в БД 3 `consensus`, `is_issued=1`, `pair_key` = `anthropic/claude-sonnet-5.5+x-ai/grok-4.7`.
5. MockTransport MOEX: `last_price` = 100; свечи: для первых двух прогнозов `high=116` через 3 дня после создания; для третьего максимум `high=110` на весь срок.
6. Выполни `base_price_stage`, затем сдвинь `FakeClock` на `61` день и выполни `backtest_stage` → два `is_successful=1`, один `0`.
7. `Evaluator.update` → тест пары `done`, `success_pct = 66.67`, `verdict='unsuitable'`, `kv.project_state='project_failed'`, есть `Proposal('project_result')`.
8. Повтори с третьим прогнозом, у которого `high=116`: `success_pct = 100.0`, `suitable`, `project_state='candidates_test'`, `efficiency` не `None`, `cost_issued_usd` равна сумме `cost_usd` `status='ok'` запросов обеих моделей.
9. Проверь, что `cfg.llm.pair` в ходе теста не изменился.
10. Проверь, что невыданный прогноз (подставь модели `confidence=80`) не попал ни в `accuracy`, ни в выборку теста.

## Дополнительно
Запусти `pytest tests/acceptance -q` — ни один тест не должен быть пропущен (`skipped`). Если пропущены — значит модуль не создан или назван не по контракту: исправь код, не тесты.

## Готово, когда
`pytest` и `ruff check .` без ошибок, `tests/acceptance` проходит полностью без `skipped`.
