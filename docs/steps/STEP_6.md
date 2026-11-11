# STEP 6 — Главный цикл, расписание, запуск через Планировщик Windows

Прочитай: `docs/SPEC.md` (§6), `docs/CONTRACTS.md` (§1).

## Создай файлы
`trading_assistant/runner.py`, `trading_assistant/platform_win.py`, `scripts/install_task.ps1`, `scripts/uninstall_task.ps1`; доработай `__main__.py` (все флаги из SPEC §6), `README.md` (раздел «Автозапуск»). Тесты: `tests/test_runner.py`, `tests/test_lock.py`.

## Сигнатуры
```python
# platform_win.py   (на не-Windows все функции — no-op)
def prevent_sleep() -> None        # ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
def allow_sleep() -> None          # SetThreadExecutionState(ES_CONTINUOUS)
class SingleInstance:              # контекстный менеджер; файл data/service.lock, msvcrt.locking (Windows) / fcntl.flock (иначе)
    def __init__(self, path: Path); def __enter__(self); def __exit__(...)   # занят -> AlreadyRunning

# runner.py
class Runner:
    def __init__(self, deps: Deps, clock: Clock, sleep=asyncio.sleep, once: bool = False, force: bool = False)
    async def run(self) -> int      # код выхода
    async def run_cycle(self, now: datetime) -> CycleReport
    def request_stop(self) -> None
```
## Логика `Runner.run`
1. Не рабочий день (`is_working_day` False) и нет `--force` → лог и выход 0.
2. `Phase.STOPPED` и нет `--force`/`--once` → лог и выход 0.
3. Инициализация БД, `cleanup`, запуск бота (если настроен) как фоновая задача (`asyncio.create_task`).
4. Цикл: фаза STOPPED → корректное завершение; иначе `run_cycle` затем `await sleep(cycle_interval_sec)`. Порядок в `run_cycle`: `collect_stage` → `analyze_stage` → `base_price_stage` → `backtest_stage` → `dispatch_stage` → `reports_stage` → `HealthMonitor.check`. Ошибка любого этапа логируется (с traceback в `errors.log`), остальные этапы выполняются. Время успешного завершения цикла пишется в `kv.last_cycle_ok`.
5. Завершение: по `stop_at`, по `request_stop()`, по Ctrl+C/Ctrl+Break (на Windows: `signal.signal(SIGINT/SIGBREAK, ...)`, который вызывает `request_stop`), либо по наличию файла `data/stop.flag` (проверяется каждый цикл, файл удаляется). При завершении: отмена фоновых задач, закрытие БД и HTTP-клиентов, `allow_sleep()`.
6. `--once`: один цикл без проверок фазы и дня, выход 0.
Запуск `prevent_sleep()` в начале и `allow_sleep()` в `finally`.

## Скрипты Планировщика
`scripts/install_task.ps1` (параметры: `-PythonPath` по умолчанию `.venv\Scripts\pythonw.exe`, `-WorkDir` = корень репозитория):
- задача `TradingAssistant`, триггер: ежедневно пн–пт в 07:45;
- `-WakeToRun`, `-StartWhenAvailable`, `-AllowStartIfOnBatteries`, `-DontStopIfGoingOnBatteries`, `-MultipleInstances IgnoreNew`, `-ExecutionTimeLimit (New-TimeSpan -Hours 16)`;
- действие: `pythonw.exe -m trading_assistant`, рабочая папка — корень репозитория;
- затем показывает `Get-ScheduledTask`.
`uninstall_task.ps1` удаляет задачу. В README описать: как включить таймеры пробуждения в схеме электропитания, как запустить задачу вручную для проверки, как смотреть `data/app.log`.

## Таблицы тестов
`Runner` (FakeClock, `sleep` подменён, заглушки этапов, записывающие порядок вызовов):
| сценарий | результат |
|---|---|
| суббота, без `--force` | выход 0, этапы не вызваны |
| будний день 22:41 МСК | выход 0, этапы не вызваны |
| будний день 12:00, `once=True` | ровно 1 цикл, порядок этапов collect→analyze→base_price→backtest→dispatch→reports→health |
| этап analyze бросает исключение | остальные этапы выполнены, ошибка в логе, `last_cycle_ok` записан |
| время переходит 22:40 | цикл завершается, `allow_sleep` вызван |
| создан `data/stop.flag` | выход после текущего цикла, файл удалён |
| `request_stop()` во время sleep | выход |
`SingleInstance`: второй экземпляр на том же файле → `AlreadyRunning`; после `__exit__` блокировка снимается.
`platform_win`: на не-Windows вызовы не падают (no-op); на Windows вызов `prevent_sleep`/`allow_sleep` проходит без исключения.
`__main__`: `--check-config` → код 1 при отсутствии `OPENROUTER_API_KEY`; `--report` не требует сети.

## Готово, когда
`pytest`, `ruff check .` без ошибок; ручной запуск `python -m trading_assistant --once --dry-run` и `python -m trading_assistant --force` (остановка Ctrl+C) работает; `install_task.ps1` создаёт задачу.
