# STEP 4B — этапы `base_price_stage` и `backtest_stage`

Прочитай: `docs/SPEC.md` (§5), `docs/CONTRACTS.md` (§1, §6). Подшаг 4A выполнен. Делай ТОЛЬКО этот подшаг.

## Измени файлы
`pipeline.py` (добавь две функции), `repository.py` (методы ниже), `__main__.py` (расширь `--once`: сбор → анализ → базовые цены → проверка). Тест: `tests/test_price_stages.py`.

## Методы репозитория (добавь)
`predictions_without_base_price(since: datetime) -> list[dict]` — выданные (`is_issued=1`), `base_price IS NULL`, `created_at >= since`;
`set_base_price(prediction_id, price)`;
`unevaluated_predictions() -> list[dict]` — выданные (`is_issued=1`) с `evaluated_at IS NULL`;
`set_evaluation(prediction_id, check_date, check_price, max_move_pct, target_hit, is_successful, evaluated_at)` — `check_date` ISO-строкой, `is_successful` может быть `None`.
Невыданные прогнозы (`is_issued=0`) НЕ оцениваются и цен не получают.

## Этапы
**`base_price_stage(deps, now)`:** для `predictions_without_base_price(now - 24 ч)` вызови `MoexClient.last_price(instrument)` (инструмент найди через реестр по `ticker`; нет в реестре → пропустить). Цена получена → `set_base_price`. Нет цены → оставить `NULL` (в следующем цикле повторим; `evaluate` сам возьмёт `close`).
**`backtest_stage(deps, now)`:** не чаще `polling.backtest_interval_sec` (ключ `kv.last_backtest`, время ISO UTC). Затем: `unevaluated_predictions()` сгруппируй по `ticker`; для каждого тикера ОДИН запрос `daily_candles(start = min(created_msk) - 7 дней, end = today_msk)`; для каждого прогноза вызови `evaluate(direction, horizon_days, expected_change_pct, created_msk, base_price, candles, today_msk, cfg.signal.min_move_pct)`. Результат не `None` → `set_evaluation(...)`, `evaluated_at = now` (ISO UTC). `None` → ничего не писать. После цикла записать `kv.last_backtest = now`. Оценка тест-вердиктов (`Evaluator.update`) подключается позже, в подшаге 4C — здесь её НЕ вызывать.
Даты `created_msk`: переведи `created_at` (UTC) в МСК через `clock.msk_date(iso, tz)` (уже есть в проекте).

## Тесты (FakeClock, `:memory:` БД, MockTransport)
1) прогноз без `base_price` → после этапа цена записана; 2) нет цены → остаётся `NULL`; 3) невыданный прогноз не получает цены и не оценивается; 4) `backtest_stage` с кандидатом `up` и свечой `high=116` на 3-й день → `is_successful=1`, `evaluated_at` записан; 5) срок идёт, порога нет → `evaluated_at` остаётся `NULL`; 6) срок истёк → `is_successful=0`; 7) повторный вызов раньше `backtest_interval_sec` → MOEX не вызывается (`FakeClock`); 8) два прогноза по одному тикеру → один запрос свечей; 9) данных MOEX нет и срок + 14 дней прошёл → `is_successful=NULL`, `evaluated_at` записан.

## Готово, когда
`pytest` и `ruff check .` без ошибок; `python -m trading_assistant --once --dry-run` проходит все этапы без исключений.
