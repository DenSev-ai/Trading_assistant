# STEP 4D — отчёты на e-mail: ежедневный, еженедельный, событийные

Прочитай: `docs/SPEC.md` (§1, §2), `docs/CONTRACTS.md` (§2, §10). Подшаги 4A–4C выполнены. Делай ТОЛЬКО этот подшаг.

## Создай/измени файлы
`trading_assistant/reports/__init__.py` (пустой), `reports/mailer.py`, `reports/reports.py`; `pipeline.py` (`reports_stage`); `__main__.py` (флаг `--send-report daily|weekly`); `config.py`/`EnvSettings` — если ещё нет, добавь поля `SMTP_HOST`, `SMTP_PORT` (по умолчанию 465), `SMTP_USER`, `SMTP_PASSWORD`, `REPORT_EMAIL_TO` (все `str | None`, значения не печатать). Тесты: `tests/test_mailer.py`, `tests/test_reports.py`.

## Сигнатуры
```python
# reports/mailer.py
class Mailer(Protocol):
    async def send(self, subject: str, body: str) -> bool
class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, to: str, timeout: float)
    async def send(self, subject: str, body: str) -> bool      # aiosmtplib.send(..., use_tls=True); любая ошибка -> лог без пароля, False
class FileMailer:
    def __init__(self, directory: Path, now_fn, kind_fn=None)
    async def send(self, subject: str, body: str) -> bool      # пишет data/reports/YYYY-MM-DD_HHMM_<тема-слаг>.txt, True
def make_mailer(env: EnvSettings, cfg: AppSettings, now_fn) -> Mailer    # SmtpMailer, если заданы SMTP_HOST, SMTP_USER, SMTP_PASSWORD, REPORT_EMAIL_TO; иначе FileMailer
# reports/reports.py
def build_daily(data: dict) -> tuple[str, str]
def build_weekly(data: dict) -> tuple[str, str]
def build_event(kind: str, payload: dict) -> tuple[str, str]
class ReportService:
    def __init__(self, repo, stats, evaluator, cfg: AppSettings, mailer: Mailer, file_mailer: FileMailer, now_fn)
    async def run(self, now: datetime) -> list[str]            # какие отчёты ушли за этот вызов
    async def event(self, kind: str, key: str, payload: dict) -> bool   # True, если письмо отправлено впервые
```

## Правила
1. ЛЮБОЙ отчёт сначала сохраняется в `data/reports/` через `file_mailer`, затем (если настроен SMTP) отправляется `mailer`. Ошибка отправки только логируется (без пароля), `run` исключений наружу не бросает.
2. Если SMTP не настроен — при старте (`--once`, основной запуск) пишется одна строка предупреждения в лог: «E-mail отчёты не настроены: SMTP_* и REPORT_EMAIL_TO пусты; отчёты сохраняются в data/reports/». Она также попадает в первый ежедневный отчёт. Это НЕ ошибка и сервис не останавливается.
3. **Ежедневная сводка:** в рабочие дни (`is_working_day`), не раньше `reports.daily_time` по МСК, один раз за дату (`kv.last_daily_report` = дата МСК). Содержимое (`data` — dict, поля продумай сам): состояние проекта; число собранных/отфильтрованных/кластеров/проанализированных за предыдущие сутки МСК; выдано моделями; согласовано парой; отправлено алертов; расход за сутки и остаток лимита; число непроверенных прогнозов; последние 50 строк `data/errors.log` (если файла нет — пропустить).
4. **Еженедельный отчёт:** в день `reports.weekly_weekday` не раньше `reports.weekly_time`, один раз за ISO-неделю (`kv.last_weekly_report`). Содержимое: `Stats.report(now)`; проверено за 7 дней; расход за неделю и всего; прогресс всех тестов («n из N»); доля `target_hit` среди проверенных выданных; число невыданных прогнозов (охват); рейтинг эффективности; текст последних `Proposal`.
5. **Событийные письма** (`reports.event_reports`): `kind` ∈ `test_done`, `project_suitable`, `project_unsuitable`, `replace_proposal`, `best_pair_proposal`, `budget_exhausted`, `llm_fatal`. `event(kind, key, payload)` отправляет письмо один раз на `key` (запись в `kv` по ключу `event:<kind>:<key>`). `ReportService.run` не вызывает `event` сам: `backtest_stage` и `analyze_stage` вызывают его при появлении `Proposal` и при `budget_exhausted`/`fatal`.
6. `reports.enabled = false` → `run` ничего не делает.
7. Пароль и ключи нигде не печатаются.

## Тесты (FakeClock, FakeMailer, `tmp_path` для файлов)
1) ежедневная сводка в 09:46 в будний день → 1 отправка, повторный `run` → 0; 2) выходной → 0; 3) до `daily_time` → 0; 4) еженедельный в понедельник → 1 отправка, во вторник → 0; 5) SMTP падает → `run` без исключения, файл сохранён; 6) нет SMTP → `make_mailer` возвращает `FileMailer`, предупреждение в логе; 7) пароль не встречается в тексте отчёта и логе; 8) `event` с тем же `key` второй раз → False, письмо одно; 9) `reports.enabled=false` → 0; 10) тема и текст дневной сводки содержат состояние проекта и расход.

## Готово, когда
`pytest` и `ruff check .` без ошибок; `python -m trading_assistant --send-report daily` создаёт файл в `data/reports/` (и письмо, если SMTP настроен).
