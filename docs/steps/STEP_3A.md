# STEP 3A — LLM-клиент, промт, разбор ответа, один вызов модели с повторами

Прочитай: `docs/SPEC.md` (§1, §3), `docs/CONTRACTS.md` (§3, §4). `prompts/analyze_news.txt` и блок `llm` в `config/settings.yaml` готовы — не меняй. Делай ТОЛЬКО этот подшаг.

## Создай файлы
`trading_assistant/llm/__init__.py` (пустой), `llm/client.py`, `llm/analyzer.py`, `llm/budget.py` (ТОЛЬКО функция `cost_usd`, класс `Budget` — в подшаге 3B). Тесты: `tests/test_parse_analysis.py`, `tests/test_client.py`, `tests/test_prompt.py`, `tests/test_run_model.py`.

## Сигнатуры (копируй имена и типы как есть)
```python
# llm/client.py
@dataclass
class LLMReply: text: str; tokens_in: int; tokens_out: int; latency_ms: int
class LLMError(Exception):
    def __init__(self, kind: str, message: str = ""): ...   # kind: 'transient' | 'fatal' | 'bad'
class OpenRouterClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str, timeout: float)
    async def complete(self, model: ModelSpec, system: str, user: str, temperature: float, max_tokens: int) -> LLMReply

# llm/budget.py
def cost_usd(tokens_in: int, tokens_out: int, model: ModelSpec) -> float   # tokens_in/1e6*price_in + tokens_out/1e6*price_out

# llm/analyzer.py
@dataclass
class RunRecord: model: str; status: str; raw_response: str | None; tokens_in: int; tokens_out: int; cost_usd: float; latency_ms: int
@dataclass
class ModelOutcome: model: str; status: str; analysis: LLMAnalysis | None; cost_usd: float
def build_system_prompt(template: str, registry: InstrumentRegistry, signal: SignalSettings) -> str
def build_user_message(title: str, text: str, sources_count: int, max_chars: int, published_msk: str, source: str) -> str
def parse_analysis(raw: str, registry: InstrumentRegistry) -> LLMAnalysis
async def run_model(client: OpenRouterClient, model: ModelSpec, system: str, user: str,
                    cfg: LLMSettings, registry: InstrumentRegistry,
                    sleep=asyncio.sleep) -> tuple[ModelOutcome, list[RunRecord]]
```

## Алгоритмы (делай буквально)
**`OpenRouterClient.complete`:** `POST {base_url}/chat/completions`, заголовок `Authorization: Bearer <key>`, тело JSON: `model=model.id`, `messages=[{"role":"system","content":system},{"role":"user","content":user}]`, `temperature`, `max_tokens`, `response_format={"type":"json_object"}`; поле `reasoning={"effort": model.reasoning_effort}` добавлять ТОЛЬКО если `model.reasoning_effort` не None. Таймаут — параметр `timeout`. Ответ: текст = `choices[0].message.content`; токены = `usage.prompt_tokens`, `usage.completion_tokens` (нет `usage` → 0). Задержку измерь через `time.monotonic()` в миллисекундах (разрешённое исключение из правила про время).
Ошибки: `httpx.TimeoutException`, `httpx.ConnectError` → `LLMError('transient')`; HTTP 401/402/403 → `LLMError('fatal')`; HTTP 429 и 5xx → `LLMError('transient')`; прочие 4xx → `LLMError('bad')`; `content` равен None или пуст → `LLMError('transient')`.
**`build_system_prompt`:** `template.format(instruments=registry.prompt_block(), max_horizon=signal.max_horizon_days, min_move=<число>)`, где число — `signal.min_move_pct` без лишних нулей (15.0 → "15", 15.5 → "15.5"). Шаблон уже содержит `{{` и `}}` для фигурных скобок JSON; файл шаблона НЕ меняй.
**`build_user_message`:** ровно такой формат (переводы строк `\n`):
```
Дата публикации (МСК): {published_msk}
Источник: {source}
Источников в кластере: {sources_count}
<<<NEWS_DATA>>>
{title}

{text[:max_chars]}
<<<END_NEWS_DATA>>>
```
**`parse_analysis`:** (1) убрать ограждения ```` ```json ````/```` ``` ````; (2) взять подстроку от первой `{` до последней `}` (нет скобок → `ValueError`); (3) `json.loads`; (4) валидировать `LLMAnalysis` (ошибка валидации → `ValueError`); (5) выбросить инструменты, чей `ticker` не найден через `registry.get(...)` (запись в лог `warning`), остальные оставить. НЕ отбрасывать инструменты за `expected_change_pct`/`confidence`/`horizon_days` — это решает `is_issued` в подшаге 3B.
**`run_model`:** до 3 HTTP-запросов, паузы между ними `cfg.retry_pauses_sec` (после 1-го неудачного — `await sleep(pauses[0])`, после 2-го — `await sleep(pauses[1])`). Для каждого запроса — один `RunRecord`: `status` ∈ `ok | invalid_json | http_error | fatal`. Повтор при `LLMError('transient')` и при `ValueError` из `parse_analysis`. `LLMError('fatal')` → сразу `status='fatal'`, без повторов. `LLMError('bad')` → `http_error`, без повторов. После 3-й неудачи итоговый `status` = статус последней попытки (`http_error` или `invalid_json`). `ok` → `ModelOutcome(status='ok', analysis=...)`. `cost_usd` итога = сумма по всем записям.

## Тесты (обязательны)
`parse_analysis`:
| raw | результат |
|---|---|
| `{"severity":7,"instruments":[{"ticker":"SBER","direction":"UP","confidence":90,"horizon_days":5,"expected_change_pct":3,"reason":"x"}]}` | 1 инструмент, direction `up` |
| то же в ```` ```json ... ``` ```` | то же |
| `Вот ответ: {...}` | разобрано |
| `not json` | ValueError |
| тикеры `XXXX` и `SBER` | остался только `SBER` |
| `severity` 11 | ValueError |
| `{"severity":0,"instruments":[]}` | пусто, без ошибки |
| `horizon_days` 181 и `expected_change_pct` 10 | валидно, инструмент сохранён |
`cost_usd`: 1000 вход + 500 выход, цены 0.1 и 0.5 → `0.00035` (`pytest.approx`); цены 0 → `0.0`.
`build_system_prompt`: нет необработанных `{...}` кроме JSON-примеров; есть «15%» и «180 календарных»; для `min_move_pct=15.5` есть «15.5%».
`build_user_message`: содержит дату, источник, оба маркера; текст обрезан до `max_chars`.
`OpenRouterClient` (httpx.MockTransport): 200 → `LLMReply`; в теле есть `response_format`; `reasoning` есть только если задан `reasoning_effort`; 401 → fatal; 429 → transient; 500 → transient; 400 → bad; `content: null` → transient; таймаут → transient.
`run_model` (MockTransport + `sleep` пишет паузы в список): 1) 200 валидный JSON → 1 запрос, `ok`; 2) 500, 500, 200 → 3 запроса, паузы `[1, 30]`, `ok`; 3) 500 ×3 → `http_error`, 3 записи; 4) невалидный JSON ×3 → `invalid_json`; 5) 401 → 1 запрос, `fatal`; 6) 429, затем 200 → `ok`; 7) `content` null ×3 → `http_error`.

## Готово, когда
`pytest` и `ruff check .` без ошибок. Эталонные тесты `tests/acceptance/*` НЕ редактируй и не удаляй.

Источников в кластере: {sources_count}
<<<NEWS_DATA>>>
{title}

{text[:max_chars]}
<<<END_NEWS_DATA>>>
```
