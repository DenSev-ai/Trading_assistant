# CONTRACTS — схема данных, алгоритмы, форматы

## 1. База данных (SQLite, WAL). Все времена — TEXT, UTC ISO 8601.
`Database.init()` выполняет этот DDL (`CREATE ... IF NOT EXISTS`) и `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON`.
```sql
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sources_count INTEGER NOT NULL DEFAULT 1,
  main_article_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|analyzed|failed|skipped
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  summary TEXT,
  published_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  fulltext TEXT,
  status TEXT NOT NULL,                      -- filtered|no_fulltext|clustered
  cluster_id INTEGER REFERENCES clusters(id)
);
CREATE INDEX IF NOT EXISTS ix_articles_fetched ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS ix_clusters_created ON clusters(created_at);
CREATE TABLE IF NOT EXISTS model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL,                      -- ok|invalid_json|http_error|fatal
  raw_response TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_created ON model_runs(created_at);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  model TEXT NOT NULL,                       -- id модели или 'consensus'
  ticker TEXT NOT NULL,
  direction TEXT NOT NULL,                   -- up|down
  confidence INTEGER NOT NULL,
  severity INTEGER NOT NULL,
  horizon_days INTEGER NOT NULL,             -- календарные дни, 1..signal.max_horizon_days
  expected_change_pct REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  prompt_version TEXT NOT NULL,              -- версия промта; статистика разных версий не смешивается
  pair_key TEXT,                             -- только для model='consensus': 'idA+idB' (ids по алфавиту)
  is_issued INTEGER NOT NULL DEFAULT 0,      -- 1 = прошёл ВСЕ пороги §5 («выданный прогноз»); только такие считаются в точности
  run_id INTEGER REFERENCES model_runs(id),  -- вызов модели, давший прогноз (NULL у consensus)
  base_price REAL,
  check_date TEXT,                           -- дата первой свечи, достигшей порога (успех), или последней свечи окна (неуспех)
  check_price REAL,                          -- экстремум свечи (high для up, low для down) при успехе; close последней свечи при неуспехе
  max_move_pct REAL,                         -- макс. движение в сторону прогноза за срок, %
  target_hit INTEGER,                        -- 1|0: достигнута ли ещё и предсказанная величина expected_change_pct
  is_successful INTEGER,                     -- 1|0|NULL: достигнуто ли движение >= signal.min_move_pct
  evaluated_at TEXT,                         -- не NULL = проверка завершена
  UNIQUE(cluster_id, model, ticker)
);
CREATE TABLE IF NOT EXISTS model_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,                     -- id модели или 'pair:idA+idB'
  role TEXT NOT NULL,                        -- pair_model|candidate|pair
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',    -- running|done
  started_at TEXT NOT NULL,
  finished_at TEXT,
  n INTEGER,                                 -- размер выборки при завершении
  success_pct REAL,
  cost_issued_usd REAL,                      -- стоимость вызовов, давших выданные прогнозы
  cost_total_usd REAL,                       -- вся стоимость вызовов за тест (справочно)
  efficiency REAL,                           -- cost_issued_usd / success_pct (меньше = лучше)
  verdict TEXT,                              -- suitable|unsuitable
  UNIQUE(subject, role, prompt_version)
);
CREATE INDEX IF NOT EXISTS ix_tests_subject ON model_tests(subject, status);
CREATE INDEX IF NOT EXISTS ix_pred_ticker ON predictions(ticker, created_at);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  ticker TEXT NOT NULL,
  kind TEXT NOT NULL,                        -- alert|digest|suppressed
  sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliv_ticker ON deliveries(ticker, sent_at);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```
**Выданный прогноз** (`is_issued=1`) — запись, прошедшая ВСЕ пороги §5 (для модели — её собственные, для `consensus` — пороги консенсуса). **Сигнал** = запись `predictions` с `model='consensus'` и `is_issued=1`. Принцип проекта: лучше меньше прогнозов, но точнее.
Состояние проекта хранится в `kv`: `project_state` = `pair_test` (старт: боевая пара набирает выборку) | `candidates_test` (проект признан пригодным, идут тесты кандидатов) | `project_failed` (выборка набрана, точность ниже порога).
Очистка при старте: `articles.fulltext` и `model_runs.raw_response` старше 30 дней → NULL.

## 2. Конфиги
- `config/settings.yaml` → `AppSettings` (pydantic, `extra="forbid"`). Неверный файл → понятная ошибка, сервис не стартует.
- `config/keywords.yaml`: `include` (список подстрок), `exclude` (подстроки-стоп). Сравнение без учёта регистра по подстроке.
- `config/instruments.json`: список `{ticker, name, type: share|index, secid, board, market, aliases, enabled}`. Единственный источник тикеров. Тикер вне списка (или `enabled=false`) отбрасывается.
- `.env` → `EnvSettings` (pydantic-settings): `OPENROUTER_API_KEY` (обязательна), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` (через запятую; оба необязательны), `SMTP_HOST`, `SMTP_PORT` (по умолчанию 465, SSL/TLS), `SMTP_USER`, `SMTP_PASSWORD`, `REPORT_EMAIL_TO` (все необязательны; без них отчёты пишутся только в `data/reports/`, в чек-листе `[ОПЦИОНАЛЬНО]`).
- Провайдер LLM заменяем: адрес и ключ — `llm.base_url` и `OPENROUTER_API_KEY`; клиент работает по OpenAI-совместимому протоколу, код не привязан к OpenRouter.
- Чек-лист `--check-config`: строка на переменную: `[OK] NAME` / `[НЕТ] NAME` (обязательная, код выхода 1) / `[ОПЦИОНАЛЬНО] NAME`. Значения не печатать.

## 3. Ответ LLM (только JSON)
```json
{"severity": 0-10, "instruments": [{"ticker": "SBER", "direction": "up|down", "confidence": 0-100,
  "horizon_days": 1-180, "expected_change_pct": 18.5, "reason": "до 300 символов"}]}
```
`horizon_days` — календарные дни. `expected_change_pct` — положительное число (направление задаёт `direction`). `severity=0` и `instruments=[]` — новость не касается инструментов или сильного движения не будет (норма, не ошибка). `parse_analysis` НЕ отбрасывает инструменты с `expected_change_pct` < `min_move_pct`, `confidence` < порога и т. п.: валидный ответ сохраняется как есть, а флаг `is_issued` ставит `is_issued(...)` по §5. Если у модели в ответе `horizon_days` > `signal.max_horizon_days` — ответ валиден, но `is_issued=0`. Запрос: `response_format={"type":"json_object"}`, `temperature` и `max_tokens` из конфига, `reasoning={"effort": X}` только если для модели в конфиге задан `reasoning_effort`.
Системный промпт — `prompts/analyze_news.txt` (плейсхолдеры `{instruments}`, `{max_horizon}` = `signal.max_horizon_days`, `{min_move}` = `signal.min_move_pct`; промпт ОДИН для всех моделей — пары и кандидатов; различия только в параметрах конфига). Версия промта `llm.prompt_version` пишется в `model_runs.prompt_version` и `predictions.prompt_version`; при её смене статистика и тесты начинаются заново (§9). Пользовательское сообщение:
```
Дата публикации (МСК): {published_msk}
Источник: {source}
Источников в кластере: {sources_count}
<<<NEWS_DATA>>>
{title}

{text}
<<<END_NEWS_DATA>>>
```
Текст обрезается до `llm.max_text_chars`.
Разбор ответа (`parse_analysis`): снять ```-ограждения; взять текст от первой `{` до последней `}`; `json.loads`; pydantic-валидация (`direction` приводится к нижнему регистру, `reason` обрезается до 300 символов). Нарушение схемы → `invalid_json`. Инструмент с неизвестным тикером отбрасывается (в лог), остальные остаются.

## 4. Ретраи и бюджет
- Один вызов модели = до **3 HTTP-запросов**: основной + 2 повтора с паузами `llm.retry_pauses_sec` = `[1, 30]` секунд. Повтор при: таймауте, 429, 5xx, `invalid_json`. После неудачи последнего повтора `model_runs.status` = `http_error|invalid_json`, кластер → `failed` (повторно не обрабатывается).
- 401/402/403 не повторять: `status='fatal'`, анализ в этом цикле прекращается, кластер остаётся `pending`.
- Если хотя бы одна модель пары не дала валидный ответ — кластер `failed`, консенсус не строится.
- Стоимость: `tokens_in/1e6*price_in + tokens_out/1e6*price_out` (цены в `llm.models`, токены из `usage` ответа). Каждый вызов пишется в `model_runs`.
- Перед каждым кластером: расход за текущие сутки МСК ≥ `llm.daily_budget_usd` → анализ прекращается до следующих суток (кластеры остаются `pending`), админу один алерт в сутки. Одновременных кластеров не больше `llm.concurrency`.
- Кандидаты (`llm.candidates`): вызываются так же, пишут `predictions` под своим именем; их сбой не влияет на кластер и консенсус. Вызываются только при `project_state = candidates_test`, не более `llm.candidates_parallel` одновременно (первые по списку, чей тест не завершён и не имеет вердикта для текущей `prompt_version`), и только пока расход за сутки МСК < `llm.candidate_budget_share * llm.daily_budget_usd`. Боевая пара вызывается всегда, пока расход < `daily_budget_usd`.
- `model_runs.cost_usd` — стоимость одного HTTP-запроса; все запросы (в т. ч. повторы и вызовы без прогноза) входят в общий расход и лимит.

## 5. Выдача прогноза и консенсус
**`is_issued(item, severity, cfg) -> bool`** — порог одной модели. ВСЕ условия: `confidence >= signal.min_confidence`; `severity >= signal.min_severity`; `expected_change_pct >= signal.min_move_pct`; `1 <= horizon_days <= signal.max_horizon_days`. Невыданный прогноз сохраняется (`is_issued=0`) для анализа, но не отправляется и не участвует в точности и в расчёте стоимости теста.

**`build_consensus(a, b, cfg)`** — вход: валидные ответы двух моделей пары. Для каждого тикера, присутствующего в обоих, КАЖДАЯ модель которых прошла `is_issued`:
1. `direction` разные → пропускается.
2. Расхождение величин: `(max(m1,m2) - min(m1,m2)) / min(m1,m2) > signal.magnitude_tolerance` → пропускается (`m` = `expected_change_pct`).
3. Иначе строка `consensus` (`is_issued=1`, `pair_key` из id пары): `direction` общий; `expected_change_pct = min(m1,m2)` (осторожное значение); `horizon_days = max(h1,h2)` (срок, к которому ожидают результат обе модели; сроки не сравниваются, оба ≤ `max_horizon_days`); `confidence = min`; `severity = min(s1,s2)`; `reason` — от модели с меньшим `confidence`.
Проверка точности отдельной модели идёт по её собственным `horizon_days` и `expected_change_pct`; проверка `consensus` — по его значениям.

| A (dir,conf,m,h) | B (dir,conf,m,h) | Результат |
|---|---|---|
| up,90,15,60 | up,88,18,90 | consensus up, conf 88, m 15, h 90 (3/15 = 0.20 допустимо) |
| up,90,15,60 | up,90,19,60 | нет (4/15 = 0.267 > 0.20) |
| up,90,15,60 | down,90,15,60 | нет |
| up,90,15,30 | up,90,15,170 | consensus up, m 15, h 170 (сроки не сравниваются) |
| up,84,20,60 | up,90,20,60 | нет (у A confidence ниже 85) |
| up,90,14.9,60 | up,90,15,60 | нет (у A величина < 15) |
| up,90,20,181 | up,90,20,60 | нет (у A срок > 180) |

**Приоритетный алерт** (`is_priority(row, cfg)`): `expected_change_pct > signal.priority_move_pct` ИЛИ (`expected_change_pct >= signal.min_move_pct` И `horizon_days < signal.priority_fast_days`). Приоритетный алерт отправляется сверх `telegram.daily_alert_limit`, но НЕ обходит анти-спам (`telegram.antispam_hours`).

## 6. Проверка прогноза (`evaluate`, модуль `trading_assistant/backtest.py`)
Успех = цена ХОТЬ РАЗ за срок достигла движения `>= min_move_pct` (15%) в сторону `direction`, даже если потом вернулась. Проверка идёт по дневным свечам MOEX: для `up` берётся `high` свечи, для `down` — `low` (внутридневной экстремум дня).
Сигнатура (аргументы именно с такими именами): `evaluate(direction, horizon_days, expected_pct, created_msk, base_price, candles, today_msk, min_move_pct) -> EvalResult | None`. Свечи берёт `MoexClient.daily_candles(ins, start, end) -> list[Candle]` (колонки `high`, `low`, `close`, `begin` разбираются по ИМЕНИ колонки); `last_price` при пустом `LAST` берёт `close` последней свечи. `candles` — `list[Candle]` по возрастанию даты; `Candle` — frozen dataclass `(date, high, low, close)` в `trading_assistant/market/moex.py`. `EvalResult` — dataclass `(check_date, check_price, max_move_pct, target_hit, is_successful)`.
**Алгоритм (выполнять по шагам, ничего не придумывать):**
1. `end_date = created_msk + timedelta(days=horizon_days)` (календарные дни).
2. `base = base_price`; если `None` — `close` последней свечи с `date <= created_msk`; такой свечи нет → вернуть `None`. `base <= 0` → вернуть `None`.
3. `window` = свечи с `created_msk < date <= end_date`. День создания в окно НЕ входит (неизвестно, что было до создания прогноза; это осторожное допущение).
4. Движение свечи: `up`: `(high - base) * 100.0 / base`; `down`: `(base - low) * 100.0 / base`. Использовать именно эту формулу (умножение до деления — чтобы 15.0 получалось точно). `max_move_pct` = максимум по `window` (если окно непустое).
5. Есть свеча окна с движением `>= min_move_pct` → вернуть `EvalResult(check_date=дата ПЕРВОЙ такой свечи, check_price=её high (up) или low (down), max_move_pct=максимум по всему окну (все свечи до сегодня), target_hit=int(max_move_pct >= expected_pct), is_successful=1)`.
6. Иначе, если `today_msk > end_date` (срок истёк) и окно непустое → `EvalResult(check_date=дата последней свечи окна, check_price=её close, max_move_pct, target_hit=int(max_move_pct >= expected_pct), is_successful=0)`.
7. Иначе, если `today_msk > end_date`, окно пустое и `(today_msk - created_msk).days > horizon_days + 14` → `EvalResult(None, None, None, None, None)` (данных нет совсем; в статистику не входит).
8. Иначе → `None` (ждём; ничего не записывать).
Успех записывается сразу при первом достижении; `target_hit` и `max_move_pct` в этот момент — по данным на сегодня (информативные, в критерии не участвуют).

| direction | base | свечи окна | min_move 15 | результат |
|---|---|---|---|---|
| up | 100 | high 116 (low 99, close 101) | достигнуто внутри дня | is_successful=1, check_price=116 |
| up | 100 | high 116, затем high 90 | вернулась обратно | is_successful=1 |
| up | 100 | high 115 | ровно 15% | 1 |
| up | 100 | high 110, срок идёт | нет | None |
| up | 100 | high 110, 112 (срок истёк) | нет | 0, check_price = close последней свечи |
| down | 100 | low 84 | 16% | 1, check_price=84 |
| down | 100 | high 130, low 99 | high игнорируется | 0 после срока |
| up | 100 | свеча дня создания high 130 | в окно не входит | не учитывается |
| up | 100 | свеча после end_date high 130 | вне окна | не учитывается |

## 7. Форматы сообщений (HTML, до 4096 символов, текст экранировать)
**Алерт:**
```
<b>Сбербанк (SBER)</b> — рост не менее 18% за срок до 90 дн., потому что {reason}.
Уверенность моделей: 88%. Прогноз на основе 3 источников.
Сбылось прогнозов по SBER: 80% (n=52).
Источник: <a href="{url}">{title}</a>

Не является инвестиционной рекомендацией.
```
- «рост»/«падение» из `direction`; процент — `expected_change_pct` без лишних нулей (`15`, `18.5`); срок — `horizon_days` («за срок до N дн.»).
- Статистика (`accuracy_line`): считается по инструменту только для ТЕКУЩЕЙ боевой пары (`pair_key`) и текущей `prompt_version`, по выданным прогнозам, проверка которых завершена. `n < telegram.stat_min_n` → «Статистики по {T} пока мало (n=N): пара или модели недавно менялись.» (цифра не показывается); `stat_min_n <= n < stat_ok_n` → «Сбылось прогнозов по {T}: X% (n=N, по небольшой выборке).»; `n >= stat_ok_n` → «Сбылось прогнозов по {T}: X% (n=N).». Окно времени не ограничено (180 дней в старых версиях отменены).
- Источник — главная статья кластера (`main_article_id`); нет ссылки — строка не выводится.
- Приоритетный алерт (CONTRACTS §5) в начале сообщения получает пометку «Приоритетный прогноз».
**Лимиты:** не более `telegram.daily_alert_limit` обычных алертов за сутки МСК; приоритетные сверх лимита; анти-спам по тикеру (`telegram.antispam_hours`) действует всегда. Не отправленные из-за лимита обычные прогнозы идут в дайджест следующего утра (если им меньше 24 ч).
**Дайджест:** заголовок «Утренний дайджест», затем блоки в формате алерта по одному на тикер (последний по времени сигнал; при нескольких добавить «Прогнозов по тикеру: N»), порядок: `severity` по убыванию, затем `sources_count`. Дисклеймер один в конце. Не влезает в 4096 — делить на сообщения по границам блоков. Нет сигналов — дайджест не отправлять.

## 8. Статистика (`stats.py`)
`accuracy(model, ticker=None, pair_key=None, prompt_version=None, since=None) -> (percent|None, n)`: считаются записи `predictions` с `is_issued=1 AND evaluated_at IS NOT NULL AND is_successful IS NOT NULL` (и фильтры по аргументам). Только выданные прогнозы: невыданные в точность не входят («лучше меньше, но точнее»). Процент = `100 * SUM(is_successful) / n`.
`cost_of_issued(model, prediction_ids=None, since=None) -> float`: сумма `model_runs.cost_usd` по DISTINCT `run_id` выданных прогнозов модели (один запрос, давший несколько прогнозов, считается один раз). Если передан `prediction_ids` (выборка теста) — только по ним. Для `consensus` пары: пусть `C` — множество `cluster_id` прогнозов выборки; стоимость = сумма `model_runs.cost_usd` тех запросов обеих моделей пары, у которых `cluster_id IN C` и `status='ok'` (один успешный запрос на модель на кластер; неудачные повторы в эту сумму не входят). Остальные запросы (повторы, пустые, без выданных прогнозов) в эту стоимость НЕ входят, но показываются колонкой «весь расход» (все `model_runs` модели с начала теста).
`efficiency(cost_issued, success_pct) -> float | None` = `cost_issued / success_pct` (меньше = лучше); `None`, если `success_pct` ≤ 0.
`--report` печатает: состояние проекта (§9); таблицу: модель | роль | n | сбылось % | стоимость выданных | весь расход | эффективность | вердикт; затем пару `consensus` и тикеры; затем строку критерия проекта: `ПРОЕКТ ПРИГОДЕН` (n ≥ `evaluation.sample_n` и ≥ `evaluation.min_success_pct`), `ПРОЕКТ НЕ ПРИГОДЕН` (n ≥ sample_n и ниже порога) или `НАКОПЛЕНИЕ ДАННЫХ (n=N из sample_n)`.

## 9. Тесты пары и моделей (`evaluation.py`)
Единицы оценки: проект/пара (`subject='pair:idA+idB'`, `role='pair'`, по `consensus`), каждая модель боевой пары (`subject=id`, `role='pair_model'`) и каждый кандидат (`role='candidate'`). Запись `model_tests` уникальна по `(subject, role, prompt_version)`.
**Выборка теста (защита от смещения):** это ПЕРВЫЕ `evaluation.sample_n` (100) ВЫДАННЫХ прогнозов субъекта при данной `prompt_version`, упорядоченных по `predictions.created_at`, затем по `id`. Для пары — `model='consensus'` и `pair_key` этой пары; для модели — `model=id` и `is_issued=1`. Вердикт выносится ТОЛЬКО когда ВСЕ эти 100 прогнозов проверены (`evaluated_at IS NOT NULL`); прогнозы с `is_successful IS NULL` (нет данных MOEX) из выборки исключаются, и на их место берётся следующий по времени создания. Быстро сбывшиеся прогнозы не могут «вытеснить» медленные: состав выборки определяется временем создания, а не временем проверки. Пока в первых 100 есть непроверенные, тест остаётся `running`, а в отчёте пишется «проверено X из 100».
`n` в статистике алертов (§7) и в `--report` для текущего прогресса может считаться по всем проверенным; вердикт теста — только по описанной выше выборке.
**Какие `predictions` считаются «выданными» для модели в тесте:** `is_issued=1` по её собственным порогам (§5). Для консенсуса — строки `model='consensus'`.
Смена `llm.prompt_version` или состава пары начинает новые тесты (старые остаются в истории, в статистику новой пары не входят).
**Критерий пригодности** модели/пары: `success_pct >= evaluation.min_success_pct` (75). Иначе вердикт `unsuitable` — модель отбрасывается даже при лучшей эффективности.
**Эффективность** (только для `suitable`): `cost_issued_usd / success_pct`; чем меньше, тем лучше; стоимость — только вызовов, давших выданные прогнозы (лучше не сделать прогноз, чем ошибиться: молчание модели не делает её дороже).
**Чистые функции модуля `trading_assistant/evaluation.py` (без БД, их проверяют эталонные тесты `tests/acceptance/`):**
- `select_sample(rows: list[dict], sample_n: int) -> list[dict] | None`. `rows` — выданные прогнозы субъекта, каждая строка — dict с ключами `id` (int), `created_at` (str ISO), `evaluated_at` (str | None), `is_successful` (int | None). Шаги: (1) выбросить строки, у которых `evaluated_at` не None и `is_successful` None; (2) отсортировать по `(created_at, id)`; (3) если строк меньше `sample_n` — вернуть `None`; (4) взять первые `sample_n`; (5) если у любой из них `evaluated_at` None — вернуть `None`; (6) иначе вернуть эти строки.
- `success_pct(sample: list[dict]) -> float` = `100.0 * сумма is_successful / len(sample)`.
- `classify(pct: float, min_success_pct: float) -> str`: `"suitable"`, если `pct >= min_success_pct`, иначе `"unsuitable"`.
**Расчёт вердикта (`close_test`)**: берутся выборка и её стоимость по правилам выше; `success_pct = 100 * SUM(is_successful) / 100`; `efficiency = cost_issued_usd / success_pct` (только при `suitable`); запись `model_tests` обновляется ОДИН раз (`status='done'`, `verdict` не меняется при повторных вызовах).
**Порядок (`project_state`, `kv`):**
1. `pair_test`: работает боевая пара из двух сильных моделей разных разработчиков. Сервис собирает выборку `consensus`. По достижении `sample_n`: пара ≥ 75% → `ПРОЕКТ ПРИГОДЕН`, `project_state = candidates_test`, письмо-событие; иначе `project_failed`: работа продолжается, но кандидаты не запускаются, письмо с рекомендацией пересмотреть условия (пользователь может изменить `sample_n`, пороги, промт, пару).
2. Модели пары оцениваются отдельно: у каждой своя запись `model_tests` по её собственным выданным прогнозам. Модель с `success_pct < 75` помечается `unsuitable`, а её партнёр с ≥ 75 — `suitable`. Сервис ПРЕДЛАГАЕТ заменить неподходящую модель лучшим по эффективности `suitable`-кандидатом (письмо и Telegram админу), но НЕ меняет пару сам: меняет пользователь правкой `llm.pair`.
3. `candidates_test`: по очереди из `llm.candidates` запускаются тесты, не более `llm.candidates_parallel` одновременно, от дорогих к дешёвым (порядок списка). Кандидаты анализируют те же новости молча. После завершения — вердикт и эффективность. Если кандидат `unsuitable`, более слабые модели того же семейства пропускать не нужно: решает конфиг.
4. После теста всех кандидатов `--report` выводит рейтинг `suitable` моделей по эффективности и предложение лучшей пары: две `suitable` модели РАЗНЫХ разработчиков (префикс id до `/`) минимальной суммарной эффективности. Это предложение; автозамены нет.
`model_tests.verdict` фиксируется один раз при завершении теста.

## 10. Отчёты (`reports.py`, `mailer.py`)
Отчёты работают с первого запуска. Транспорт: `aiosmtplib`, SSL/TLS (`use_tls=True`), параметры из `.env` (`SMTP_*`, `REPORT_EMAIL_TO`). Нет `SMTP_HOST` или `REPORT_EMAIL_TO` → отчёт пишется только в `data/reports/YYYY-MM-DD_<вид>.txt` (и в лог одной строкой). Ошибка отправки логируется, сервис не останавливается; письмо сохраняется в `data/reports/` в любом случае. Отправка — не блокирует цикл (`asyncio`), один отчёт каждого вида один раз (метки в `kv`: `last_daily_report`, `last_weekly_report`).
- **Ежедневная сводка** (`reports.daily_time`, рабочие дни, первый цикл ACTIVE после этого времени; текст на русском): воронка за прошлые сутки МСК (собрано → предфильтр → кластеры → проанализировано → выдано моделями → консенсус → отправлено алертов/в дайджест), расход за сутки и остаток лимита, состояние проекта, число прогнозов в ожидании проверки, сбои (`errors.log` ≤ 50 строк).
- **Еженедельный отчёт** (`reports.weekly_weekday`, `weekly_time`): таблица из `--report` (§8), число проверенных за неделю, расход за неделю и накопленный, прогресс тестов (n из sample_n по каждому subject), доля `target_hit`, число невыданных прогнозов моделей (охват), текущий рейтинг эффективности, рекомендации сервиса (кого заменить/что тестировать дальше).
- **Событийные письма** (`reports.event_reports`): тест модели или пары завершён (вердикт, n, %, стоимость, эффективность); проект признан пригодным / не пригодным; предложение замены модели; исчерпан суточный бюджет; сбой LLM (401/402/403). Каждое событие — один раз.
