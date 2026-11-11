# STEP 2 — Сбор новостей: RSS, предфильтр, полный текст, кластеры

Прочитай: `docs/SPEC.md` (§5, §8), `docs/CONTRACTS.md` (§1, §2). Файлы Шага 1 не переписывай, добавляй методы только где указано.

## Создай файлы
`trading_assistant/news/__init__.py`, `news/sources.py`, `news/prefilter.py`, `news/fulltext.py`, `news/clustering.py`, `trading_assistant/repository.py`, `trading_assistant/pipeline.py` (пока только `collect_stage`), `tests/test_prefilter.py`, `tests/test_sources.py`, `tests/test_fulltext.py`, `tests/test_clustering.py`, `tests/test_collect_stage.py`, фикстуры `tests/fixtures/rss_sample.xml`, `tests/fixtures/rss_moex.xml`, `tests/fixtures/article.html`. Измени `__main__.py`: добавь флаг `--once` (пока только сбор, печатает счётчики этапов).

## Сигнатуры
```python
# news/sources.py
@dataclass(frozen=True) class RawItem: source: str; url: str; title: str; summary: str; published_at: datetime; content_html: str | None
def parse_feed(source: str, xml_bytes: bytes, now: datetime, window_hours: int, limit: int) -> list[RawItem]
    # feedparser.parse(xml_bytes); published_parsed -> UTC; нет даты -> пропустить запись;
    # старше окна -> пропустить; первые `limit` по убыванию даты; content_html из entry.content[0].value если есть
async def fetch_feed(client: httpx.AsyncClient, src: NewsSource) -> bytes | None   # ошибка/не 200 -> лог и None

# news/prefilter.py
def normalize_title(title: str) -> str          # lower, ё->е, убрать всё кроме букв/цифр/пробела, схлопнуть пробелы
def passes_prefilter(title: str, summary: str, kw: Keywords) -> bool

# news/fulltext.py
def html_to_text(html: str) -> str              # без внешних библиотек: убрать script/style, взять содержимое <p>, join через "\n", unescape
class RobotsCache:                               # кэш robots.txt по хосту
    def __init__(self, client, user_agent: str)
    async def allowed(self, url: str) -> bool    # ошибка загрузки robots.txt -> True; 404 -> True
async def get_fulltext(client, robots, item: RawItem, src: NewsSource, max_bytes: int) -> str | None
    # src.fulltext=="rss": html_to_text(item.content_html) ; "page": robots.allowed -> GET url -> html_to_text; не 200 -> None

# news/clustering.py
def find_cluster(title_norm: str, candidates: list[tuple[int, str]], threshold: int) -> int | None
    # candidates = (cluster_id, title_norm); rapidfuzz.fuzz.token_set_ratio; лучший >= threshold, иначе None

# repository.py  (SQL здесь, не в pipeline)
class Repository:
    def __init__(self, db: Database)
    async def article_exists(self, url: str) -> bool
    async def add_article(self, item: RawItem, title_norm: str, status: str, fulltext: str | None, now: datetime) -> int
    async def recent_clusters(self, since: datetime) -> list[dict]
    async def create_cluster(self, article_id: int, title: str, title_norm: str, now: datetime) -> int
    async def attach_to_cluster(self, article_id: int, cluster_id: int, source: str, now: datetime) -> None
        # sources_count = число уникальных articles.source в кластере; main_article_id = самая свежая статья кластера
```
## `pipeline.collect_stage(deps, now) -> CollectStats`
Для каждого включённого источника: `fetch_feed` → `parse_feed` → для каждой записи: пропустить, если `article_exists`; `passes_prefilter` нет → `add_article(status="filtered")`; иначе `get_fulltext`; `None` или короче `min_fulltext_chars` → `no_fulltext`; иначе `normalize_title`, `find_cluster` по `recent_clusters(now - cluster_window_hours)` → `attach_to_cluster` или `create_cluster`; статус статьи `clustered`. Источники обрабатываются параллельно (`asyncio.gather`, `return_exceptions=True`), исключение одного источника логируется. `CollectStats`: dataclass со счётчиками `fetched, duplicates, filtered, no_fulltext, clustered, new_clusters`.

## Таблицы тестов
`passes_prefilter` (include из keywords: «ставк», «сбер»; exclude: «футбол»):
| title | summary | результат |
|---|---|---|
| ЦБ повысил ключевую ставку | | True |
| Футбол: Сбербанк Арена | | False (exclude главнее) |
| Погода в Москве | | False |
| Погода | Сбербанк объявил дивиденды | True (ищем и в summary) |
| СБЕР вырос | | True (регистр) |

`find_cluster` (threshold 75):
| новый заголовок | кандидат | результат |
|---|---|---|
| «цб повысил ставку до 17%» | «центробанк повысил ставку до 17%» | id кандидата |
| «в москве открылся парк» | «цб повысил ставку до 17%» | None |
Кластеры вне окна 12 ч в кандидаты не попадают (проверить в `collect_stage` с `FakeClock`).

`parse_feed`: фикстура с 3 записями — одна старше окна, одна без даты → вернуть 1; `limit=1` → не больше 1.
`html_to_text`: `<p>a</p><script>x</script><p>b &amp; c</p>` → `"a\nb & c"`.
`RobotsCache`: robots с `Disallow: /doc/` → `allowed("…/doc/1")` False, `allowed("…/news/1")` True; ответ 404 → True. (HTTP через `httpx.MockTransport`.)
`get_fulltext`: режим `rss` берёт `content_html` без сетевого запроса; режим `page` при запрещённом robots → `None`, страница не запрашивается.
`collect_stage` (MockTransport + `:memory:` БД + `FakeClock`): 2 источника, одна и та же новость в обоих с похожими заголовками → 1 кластер, `sources_count=2`; повторный вызов → 0 новых статей (дубли по url); короткий текст → `no_fulltext`; ошибка 500 у одного источника → второй обработан.

## Живые тесты (маркер `live`, не запускаются по умолчанию)
`tests/test_live_sources.py`: для каждого включённого источника `fetch_feed` возвращает непустые данные и `parse_feed` > 0 записей при `window_hours=72`.

## Готово, когда
`pytest`, `ruff check .` без ошибок; `python -m trading_assistant --once` (с реальным `.env`) печатает счётчики этапов.
