# STEP 5 — Telegram: алерты, дайджест, команды, анти-спам

Прочитай: `docs/SPEC.md` (§1, §6, §7), `docs/CONTRACTS.md` (§1, §5, §7, §8).

## Создай файлы
`trading_assistant/telegram/__init__.py`, `telegram/formatter.py`, `telegram/dispatcher.py`, `telegram/bot.py`, `telegram/health.py`; допиши `pipeline.py` (`dispatch_stage`), `repository.py`, `__main__.py` (флаг `--dry-run`). Тесты: `tests/test_formatter.py`, `tests/test_dispatcher.py`, `tests/test_bot_auth.py`, `tests/test_health.py`.

## Сигнатуры
```python
# telegram/formatter.py   (чистые функции)
def escape(text: str) -> str                          # html.escape
def format_alert(sig: SignalView, accuracy: tuple[float | None, int], cfg: TelegramSettings, priority: bool = False) -> str   # CONTRACTS §7
def format_digest(sigs: list[SignalView], accuracies: dict[str, tuple[float | None, int]],
                  counts: dict[str, int], cfg: TelegramSettings) -> list[str]     # список сообщений <= 4096
def split_blocks(header: str, blocks: list[str], footer: str, limit: int = 4096) -> list[str]
@dataclass class SignalView: prediction_id; ticker; name; direction; confidence; horizon_days; expected_change_pct; reason; sources_count; severity; url: str | None; title: str | None; created_at

# telegram/dispatcher.py
class Notifier(Protocol): async def send(self, chat_id: int, text: str) -> bool
class LogNotifier:     # пишет в лог, возвращает True
class BotNotifier:     # aiogram Bot.send_message(parse_mode="HTML", disable_web_page_preview=True); TelegramForbiddenError/ошибка -> лог, False
class Dispatcher:
    def __init__(self, repo, stats, notifier, chat_ids: list[int], cfg: TelegramSettings, registry, sleep=asyncio.sleep)
    async def dispatch(self, phase: Phase, now: datetime) -> DispatchStats
        # ACTIVE: новые сигналы (consensus, is_issued=1, нет записи в deliveries) -> анти-спам по тикеру (antispam_hours, по deliveries kind='alert'|'digest'; НЕ обходится даже приоритетными)
        #   суточный лимит: обычных алертов за сутки МСК <= daily_alert_limit (5); сверх лимита уходят только is_priority (CONTRACTS §5); остальные -> в дайджест следующего утра (если моложе 24 ч)
        #   подавленные анти-спамом: deliveries kind='suppressed' (в дайджест не попадают); остальные: format_alert -> всем chat_ids с паузой send_pause_sec -> deliveries kind='alert' (после успешной отправки хотя бы одному)
        #   точность в алерте: Stats.accuracy('consensus', ticker, pair_key=текущая пара, prompt_version=текущая) (CONTRACTS §7)
        # В 09:45 (первый цикл ACTIVE за день, kv.last_digest_date != сегодня МСК): дайджест из сигналов за 24 ч без записи в deliveries, по тикеру последний
        # PASSIVE: ничего не отправляет, сигналы копятся
        # Записи старше 24 ч в дайджест не идут
```
`bot.py`: `TradingBot(dispatcher, repo, stats, chat_ids)` на aiogram 3 Router; фильтр: `message.chat.id not in chat_ids` → игнор без ответа. Команды: `/start`, `/help` (список команд), `/stats [ТИКЕР]` (`accuracy` для `consensus` текущей пары и версии промта; без тикера — общая и топ-5 тикеров; при `n < stat_min_n` — «статистики пока мало»), `/last` (5 последних consensus-сигналов), `/status` (фаза, число pending-кластеров, расход за сутки и лимит, последние `kv`-метки циклов). `drop_pending_updates=True` при старте polling.
`health.py`: `HealthMonitor.check(now)` — молчит, пока всё ок; шлёт алерт (один раз на сбой, сброс флага после восстановления) при: нет успешного цикла `> 3*cycle_interval_sec`; бюджет исчерпан; fatal-ошибка LLM (401/402/403); недоступны все RSS-источники. Состояния — в `kv`.

## Правила
- `--dry-run`: используется `LogNotifier` вместо `BotNotifier`, `deliveries` не пишутся.
- Без `TELEGRAM_BOT_TOKEN` или `TELEGRAM_CHAT_IDS`: `LogNotifier`, бот не запускается.
- Сообщение > 4096 символов делится по границам блоков (`split_blocks`); блок длиннее лимита обрезается с «…».
- Все динамические значения (`reason`, `title`, `name`) проходят через `escape`; `url` — в атрибут `href` через `html.escape(url, quote=True)`.

## Таблицы тестов
`format_alert` (при `stat_min_n=30`, `stat_ok_n=50`): 
| вход | ожидаемый фрагмент |
|---|---|
| up, 18, h=90, n=52, 80% | «рост не менее 18%», «за срок до 90 дн.», «Сбылось прогнозов по SBER: 80% (n=52).» |
| down, 15.5, h=60 | «падение не менее 15.5%» |
| n=29 | «Статистики по SBER пока мало (n=29)», цифры процента нет |
| n=30, 83% | «83% (n=30, по небольшой выборке)» |
| n=49 / n=50 | «по небольшой выборке» / без пометки |
| priority=True | в тексте «Приоритетный прогноз» |
| url=None | строки «Источник» нет |
| reason=`<b>x</b>` | в тексте `&lt;b&gt;x&lt;/b&gt;` |
| всегда | в конце «Не является инвестиционной рекомендацией.»; «Вероятность» нет |
`format_digest`: 3 сигнала по 2 тикерам → блоки в порядке `severity` ↓, затем `sources_count` ↓; тикер с 2 сигналами → один блок и «Прогнозов по тикеру: 2»; дисклеймер один раз; 200 блоков → несколько сообщений, каждое ≤ 4096.
`Dispatcher` (FakeNotifier, `:memory:` БД, `FakeClock`): 1) ACTIVE + сигнал → 1 сообщение каждому chat_id, запись `deliveries`; 2) повторный вызов → 0 сообщений; 3) PASSIVE → 0 сообщений; 4) второй сигнал по тому же тикеру через 2 ч → `suppressed`, не отправлен; через 13 ч → отправлен; 5) невыданный прогноз (`is_issued=0`) → не отправлен; 5а) 6-й обычный сигнал за сутки (разные тикеры) → не отправлен (в дайджест), приоритетный 6-й (28%) → отправлен; 5б) приоритетный сигнал по тикеру, по которому алерт был 2 ч назад → `suppressed` (анти-спам не обходится); 6) `send` возвращает False для одного chat_id → остальным отправлено, запись `deliveries` создана; 7) в 09:45 дайджест накопленных за ночь сигналов, повторно в тот же день не шлётся; 8) сигнал старше 24 ч в дайджест не попадает; 9) нет сигналов → дайджеста нет.
`bot` (auth): сообщение из чужого chat_id → ответа нет; из разрешённого → `/help` отвечает.
`health`: 1) всё ок → 0 сообщений; 2) нет цикла 10 минут при интервале 3 мин → 1 алерт; повтор → 0; после восстановления и нового сбоя → снова 1 алерт.

## Готово, когда
`pytest`, `ruff check .` без ошибок; `python -m trading_assistant --once --dry-run` печатает в лог сообщения вместо отправки.
