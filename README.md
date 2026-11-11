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

Запуск сервиса: `python -m trading_assistant` из корня репозитория (остальные флаги реализуются на siguientes шагах).
