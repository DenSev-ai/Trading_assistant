# STEP 1 — Каркас: конфиг, БД, Clock, фаза расписания, чек-лист .env

Прочитай: `docs/SPEC.md`, `docs/CONTRACTS.md` (§1, §2). Репозиторий уже содержит `config/`, `prompts/`, `requirements.txt`, `pyproject.toml`, `.env.example`.

## Создай файлы
`trading_assistant/__init__.py` (пустой), `config.py`, `clock.py`, `db.py`, `models.py`, `instruments.py`, `logging_setup.py`, `__main__.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_clock.py`, `tests/test_db.py`, `tests/test_instruments.py`, `README.md`.

## Сигнатуры
```python
# clock.py
class Clock(Protocol):
    def now(self) -> datetime: ...               # aware, UTC
class SystemClock:  def now(self) -> datetime    # datetime.now(UTC) — единственное место с реальным временем
class FakeClock:    def __init__(self, start: datetime); def now(self); def set(self, dt); def advance(self, **kw)
class Phase(StrEnum): PASSIVE="passive"; ACTIVE="active"; STOPPED="stopped"
def get_phase(now: datetime, sched: ScheduleSettings, tz: ZoneInfo) -> Phase
def is_working_day(now: datetime, sched: ScheduleSettings, tz: ZoneInfo) -> bool
def to_iso(dt: datetime) -> str                  # UTC, 'YYYY-MM-DDTHH:MM:SS+00:00'
def msk_date(iso: str, tz: ZoneInfo) -> date

# config.py  (pydantic-модели; ВСЕ поля из config/settings.yaml, extra="forbid")
class ScheduleSettings, PollingSettings, NewsSource, NewsSettings, ModelSpec, LLMSettings,
      SignalSettings, EvaluationSettings, TelegramSettings, ReportsSettings, AppSettings,
      EnvSettings(BaseSettings)
# LLMSettings: pair (ровно 2 модели с РАЗНЫМ префиксом id до «/»), candidates, candidates_parallel, candidate_budget_share
# SignalSettings: min_confidence, min_severity, min_move_pct, max_horizon_days, magnitude_tolerance, priority_move_pct, priority_fast_days
# EvaluationSettings: sample_n, min_success_pct;  ReportsSettings: enabled, daily_time, weekly_weekday, weekly_time, event_reports
# Валидация: min_move_pct > 0; 0 <= magnitude_tolerance < 1; priority_move_pct >= min_move_pct; 1 <= max_horizon_days; 0 < min_success_pct <= 100; sample_n >= 1
def load_settings(path: Path = Path("config/settings.yaml")) -> AppSettings
def load_keywords(path: Path) -> Keywords        # include: list[str], exclude: list[str]
def env_checklist(env: EnvSettings) -> tuple[list[str], bool]   # (строки, ok)

# instruments.py
class Instrument(BaseModel)                      # поля как в instruments.json
class InstrumentRegistry:
    def __init__(self, items: list[Instrument])
    @classmethod def load(cls, path: Path) -> "InstrumentRegistry"   # битый файл -> ConfigError
    def get(self, ticker: str) -> Instrument | None                  # только enabled
    def enabled(self) -> list[Instrument]
    def prompt_block(self) -> str                # строки "SBER — Сбербанк (синонимы: Сбер, ...)"

# db.py
class Database:
    def __init__(self, path: str)                # ":memory:" допустим
    async def init(self) -> None                 # DDL из CONTRACTS §1 + PRAGMA
    async def execute(self, sql: str, params: tuple = ()) -> int      # возвращает lastrowid
    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]
    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None
    async def cleanup(self, now: datetime, days: int = 30) -> None    # CONTRACTS §1 «Очистка»
    async def close(self) -> None
class ConfigError(Exception)                     # в config.py
```
`models.py`: pydantic `LLMInstrument`, `LLMAnalysis` (CONTRACTS §3: валидаторы `direction` → lower, `reason` обрезать до 300, `confidence` 0–100, `horizon_days` ≥ 1 (верхней границы нет: превышение `max_horizon_days` не ошибка, а `is_issued=0`), `expected_change_pct` > 0, `severity` 0–10) и dataclass `Article`, `Cluster`, `Prediction` с полями таблиц.

## `__main__.py` (Шаг 1)
`python -m trading_assistant --check-config` → печатает чек-лист (`env_checklist`), код выхода 0/1. Остальные флаги из SPEC §6 пока не реализуй (argparse принимает только `--check-config`). Без аргументов — печатает справку и выходит.
`logging_setup.py`: `setup_logging(data_dir, level)` — консоль + `data/app.log` (RotatingFileHandler 1 МБ × 3) + `data/errors.log` (только ERROR). Создаёт папку. Секреты в логи не пишутся вообще, маскирование не требуется.

## Таблицы тестов
`get_phase` (tz Москва, `active_from=09:45`, `stop_at=22:40`; вход в UTC):
| now (МСК) | Phase |
|---|---|
| 00:00 | PASSIVE |
| 08:00 | PASSIVE |
| 09:44:59 | PASSIVE |
| 09:45:00 | ACTIVE |
| 22:39:59 | ACTIVE |
| 22:40:00 | STOPPED |
| 23:59 | STOPPED |

`is_working_day`: суббота → False; воскресенье → False; пятница не в списке → True; пятница из `non_working_dates` → False; суббота при `skip_weekends=False` → True. 
`env_checklist`: нет `OPENROUTER_API_KEY` → ok=False, строка `[НЕТ] OPENROUTER_API_KEY`; пустой `TELEGRAM_BOT_TOKEN` → `[ОПЦИОНАЛЬНО] TELEGRAM_BOT_TOKEN`; пустые `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `REPORT_EMAIL_TO` → `[ОПЦИОНАЛЬНО]` (ok не меняется); значение ключа и пароля нигде в строках не встречается.
`load_settings`: реальный `config/settings.yaml` загружается; лишнее поле → `ConfigError`; отрицательный `cycle_interval_sec` → `ConfigError`; пара из двух моделей с одним префиксом (`openai/a`, `openai/b`) → `ConfigError`; `min_move_pct: 0` → `ConfigError`; `magnitude_tolerance: 1` → `ConfigError`.
`InstrumentRegistry`: `get("SBER").name == "Сбербанк"`; `get("XXXX") is None`; `enabled=false` → `None`; `prompt_block()` содержит все включённые тикеры.
`Database`: после `init()` существуют 7 таблиц (clusters, articles, model_runs, predictions, model_tests, deliveries, kv; служебная sqlite_sequence не считается); повторный `init()` без ошибки; `UNIQUE(cluster_id, model, ticker)` не даёт вставить дубль; `cleanup` обнуляет `raw_response` записи старше 30 дней и не трогает свежую.
`to_iso`/`msk_date`: `2026-11-06T21:30:00+00:00` → дата МСК `2026-11-07`.
`LLMAnalysis`: `direction="UP"` → `"up"`; `reason` 400 символов → 300; `confidence=120` → ошибка валидации; `horizon_days=181` → валидно; `expected_change_pct=0` → ошибка валидации.

## README.md (кратко)
Что это (цель — SPEC §1); установка (`python -m venv .venv`, `pip install -r requirements.txt`); `.env` из `.env.example`; команды `--check-config`, `pytest`, `ruff check .`.

## Готово, когда
`pytest` и `ruff check .` без ошибок; `python -m trading_assistant --check-config` печатает чек-лист.
