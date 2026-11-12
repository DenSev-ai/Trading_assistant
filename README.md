# Trading assistant

Сервис для российского фондового рынка: собирает новости → отбирает по ключевым словам → кластеризует → анализирует двумя LLM → делает прогноз, только если модели согласны → шлёт в Telegram → проверяет прогноз по ценам MOEX. Подробная цель — `docs/SPEC.md` (§1).

## Установка

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните обязательные секреты (в первую очередь `OPENROUTER_API_KEY`):
```powershell
copy .env.example .env
```

## Команды

- Проверка конфига и `.env` (печатает чек-лист, код выхода 0/1):
  ```powershell
  python -m trading_assistant --check-config
  ```
- Тесты:
  ```powershell
  .venv\Scripts\python.exe -m pytest
  ```
- Линтер:
  ```powershell
  .venv\Scripts\python.exe -m ruff check .
  ```

Запуск сервиса: `python -m trading_assistant` из корня репозитория. Для одного цикла
используйте `--once`, для работы в нерабочий день — `--force`, для проверки без
отправки в Telegram — `--dry-run`. `--report` выводит статистику без сетевых запросов.

## Автозапуск

Из корня проекта установите или удалите задачу планировщика Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1
```

Задача `TradingAssistant` запускается по будням в 07:45 и может пробуждать компьютер.
В параметрах текущей схемы электропитания Windows откройте дополнительные параметры,
раздел «Сон» → «Разрешить таймеры пробуждения» и включите их для нужного режима питания.
Для ручной проверки выполните `Start-ScheduledTask -TaskName TradingAssistant`.
Смотрите журнал `data\app.log` и ошибки `data\errors.log`. Сервис самостоятельно
останавливается в 22:40 по московскому времени; второй экземпляр не запускается из-за
блокировки `data\service.lock`. Для остановки можно создать файл `data\stop.flag`.
