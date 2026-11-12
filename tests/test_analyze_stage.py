"""Тесты pipeline.analyze_stage: вызовы моделей, прогнозы, консенсус, бюджет (STEP 3C)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from trading_assistant.clock import to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.llm.client import OpenRouterClient
from trading_assistant.llm.consensus import make_pair_key
from trading_assistant.pipeline import AnalyzeDeps, AnalyzeStats, analyze_stage
from trading_assistant.repository import Repository

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
TEMPLATE = Path("prompts/analyze_news.txt").read_text(encoding="utf-8")
PAIR_A = "anthropic/claude-sonnet-5.5"
PAIR_B = "x-ai/grok-4.7"
CANDIDATES = ("openai/gpt-5.4-mini", "deepseek/deepseek-v4-pro")
THIRD_CANDIDATE = "anthropic/claude-haiku-5.5"
EMPTY = '{"severity":2,"instruments":[]}'


def _content(
    ticker: str = "SBER",
    direction: str = "up",
    confidence: int = 90,
    horizon_days: int = 60,
    expected_change_pct: float = 18.0,
    severity: int = 8,
) -> str:
    """JSON-ответ модели с одним инструментом."""
    return json.dumps(
        {
            "severity": severity,
            "instruments": [
                {
                    "ticker": ticker,
                    "direction": direction,
                    "confidence": confidence,
                    "horizon_days": horizon_days,
                    "expected_change_pct": expected_change_pct,
                    "reason": "проверка",
                }
            ],
        },
        ensure_ascii=False,
    )


def _ok(content: str) -> httpx.Response:
    """Успешный ответ API с usage (1000 входных и 500 выходных токенов)."""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )


def _error(status_code: int) -> httpx.Response:
    """Ответ API с ошибкой (4xx без повторов, кроме 429)."""
    return httpx.Response(status_code, json={"error": "x"})


class _Api:
    """Поддельный OpenRouter: очередь ответов на каждую модель и журнал вызовов."""

    def __init__(self, replies: dict[str, list[httpx.Response]]) -> None:
        self._replies = {model: list(queue) for model, queue in replies.items()}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        self.calls.append(model)
        queue = self._replies.get(model)
        if not queue:
            raise AssertionError(f"лишний запрос к модели {model}")
        return queue.pop(0)

    def count(self, model: str) -> int:
        """Сколько запросов ушло к модели."""
        return self.calls.count(model)


def _settings() -> AppSettings:
    """Настройки сервиса из config/settings.yaml (тесты правят копию по месту)."""
    return load_settings(Path("config/settings.yaml"))


async def _add_cluster(
    db: Database,
    title: str = "ЦБ повысил ключевую ставку",
    sources: int = 1,
    created_at: datetime = NOW,
    status: str = "pending",
) -> int:
    """Вставить кластер с главной статьёй и вернуть его id."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, sources_count, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, title.lower(), to_iso(created_at), to_iso(created_at), sources, status),
    )
    article_id = await db.execute(
        "INSERT INTO articles (url, source, title, title_norm, summary, published_at, fetched_at, "
        "fulltext, status, cluster_id) VALUES (?, 'interfax', ?, ?, NULL, ?, ?, ?, 'clustered', ?)",
        (
            f"https://example.com/{cluster_id}",
            title,
            title.lower(),
            to_iso(created_at),
            to_iso(created_at),
            "Полный текст новости о ставке и рынке. " * 5,
            cluster_id,
        ),
    )
    await db.execute(
        "UPDATE clusters SET main_article_id = ? WHERE id = ?", (article_id, cluster_id)
    )
    return cluster_id


async def _add_spent(db: Database, cost: float, moment: datetime = NOW) -> None:
    """Записать расход прошлых вызовов (для проверки суточного бюджета)."""
    cluster_id = await _add_cluster(db, title="Старая новость", status="analyzed")
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, cost_usd, created_at) "
        "VALUES (?, 'test/model', 'v2', 'ok', ?, ?)",
        (cluster_id, cost, to_iso(moment)),
    )


async def _predictions(db: Database) -> list[dict]:
    """Все прогнозы в порядке вставки."""
    return await db.fetchall(
        "SELECT model, ticker, direction, confidence, is_issued, run_id, pair_key, prompt_version "
        "FROM predictions ORDER BY id"
    )


async def _cluster_status(db: Database, cluster_id: int) -> str:
    """Статус кластера по id."""
    row = await db.fetchone("SELECT status FROM clusters WHERE id = ?", (cluster_id,))
    return row["status"] if row else ""


class _Reports:
    """Поддельный сервис отчётов: запоминает событийные письма."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def event(self, kind: str, key: str, payload: dict) -> bool:
        self.events.append((kind, key, payload))
        return True


async def _run(
    db: Database,
    api: _Api,
    registry: InstrumentRegistry,
    now: datetime = NOW,
    settings: AppSettings | None = None,
    reports: _Reports | None = None,
) -> AnalyzeStats:
    """Выполнить analyze_stage против поддельного HTTP-транспорта."""
    cfg = settings or _settings()
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    try:
        llm = OpenRouterClient(http, "secret-key", cfg.llm.base_url, cfg.llm.timeout_sec)
        deps = AnalyzeDeps(
            client=llm,
            repository=Repository(db),
            settings=cfg,
            registry=registry,
            prompt_template=TEMPLATE,
            tz=MSK,
            reports=reports,
        )
        return await analyze_stage(deps, now)
    finally:
        await http.aclose()


async def test_agreeing_models_write_consensus(db, registry) -> None:
    cluster_id = await _add_cluster(db)
    api = _Api({PAIR_A: [_ok(_content())], PAIR_B: [_ok(_content())]})
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert stats.failed == 0
    assert stats.issued_rows == 2
    assert stats.consensus_rows == 1
    assert await _cluster_status(db, cluster_id) == "analyzed"

    rows = await _predictions(db)
    assert [row["model"] for row in rows].count("consensus") == 1
    assert len(rows) == 3
    assert sum(row["is_issued"] for row in rows) == 3
    consensus = rows[-1]
    assert consensus["pair_key"] == make_pair_key([PAIR_A, PAIR_B])
    assert consensus["run_id"] is None
    assert consensus["prompt_version"] == "v2"
    model_rows = [row for row in rows if row["model"] != "consensus"]
    assert all(row["run_id"] is not None for row in model_rows)


async def test_disagreeing_directions_no_consensus(db, registry) -> None:
    cluster_id = await _add_cluster(db)
    api = _Api(
        {PAIR_A: [_ok(_content(direction="up"))], PAIR_B: [_ok(_content(direction="down"))]}
    )
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert stats.consensus_rows == 0
    assert await _cluster_status(db, cluster_id) == "analyzed"
    rows = await _predictions(db)
    assert len(rows) == 2
    assert "consensus" not in [row["model"] for row in rows]


async def test_http_error_makes_cluster_failed(db, registry) -> None:
    cluster_id = await _add_cluster(db)
    api = _Api({PAIR_A: [_ok(_content())], PAIR_B: [_error(400)]})
    stats = await _run(db, api, registry)

    assert stats.failed == 1
    assert stats.analyzed == 0
    assert stats.consensus_rows == 0
    assert await _cluster_status(db, cluster_id) == "failed"
    assert await _predictions(db) == []
    runs = await db.fetchall("SELECT model, status FROM model_runs ORDER BY id")
    assert [(row["model"], row["status"]) for row in runs] == [
        (PAIR_A, "ok"),
        (PAIR_B, "http_error"),
    ]


async def test_fatal_error_keeps_cluster_pending(db, registry) -> None:
    cluster_id = await _add_cluster(db)
    api = _Api({PAIR_A: [_error(401)], PAIR_B: [_ok(_content())]})
    stats = await _run(db, api, registry)

    assert stats.analyzed == 0
    assert stats.failed == 0
    assert await _cluster_status(db, cluster_id) == "pending"
    assert await _predictions(db) == []


async def test_budget_exhausted_calls_nothing(db, registry) -> None:
    await _add_cluster(db, title="Первая новость")
    await _add_cluster(db, title="Вторая новость")
    await _add_spent(db, 0.5)
    api = _Api({})
    stats = await _run(db, api, registry)

    assert api.calls == []
    assert stats.analyzed == 0
    assert stats.skipped_budget == 2
    statuses = await db.fetchall("SELECT status FROM clusters WHERE status = 'pending'")
    assert len(statuses) == 2


async def test_fatal_error_sends_event(db, registry) -> None:
    """Фатальный сбой LLM (401) — одно событийное письмо на модель и кластер (4D)."""
    await _add_cluster(db)
    api = _Api({PAIR_A: [_error(401)], PAIR_B: [_ok(_content())]})
    reports = _Reports()

    await _run(db, api, registry, reports=reports)

    assert [event[0] for event in reports.events] == ["llm_fatal"]
    _, key, payload = reports.events[0]
    assert key == f"{PAIR_A}:1"
    assert payload["model"] == PAIR_A


async def test_budget_exhausted_sends_event(db, registry) -> None:
    """Исчерпан суточный бюджет — событийное письмо с датой суток МСК (4D)."""
    await _add_cluster(db, title="Первая новость")
    await _add_spent(db, 0.5)
    api = _Api({})
    reports = _Reports()

    await _run(db, api, registry, reports=reports)

    assert [event[0] for event in reports.events] == ["budget_exhausted"]
    _, key, payload = reports.events[0]
    assert key == "2026-11-06"  # сутки МСК от NOW (15:00 МСК)
    assert payload["spent_usd"] == "0.5000"
    assert payload["limit_usd"] == "0.5000"


async def test_candidate_failure_keeps_cluster_analyzed(db, registry) -> None:
    await Repository(db).kv_set("project_state", "candidates_test")
    cluster_id = await _add_cluster(db)
    api = _Api(
        {
            PAIR_A: [_ok(_content())],
            PAIR_B: [_ok(_content())],
            CANDIDATES[0]: [_error(400)],
            CANDIDATES[1]: [_ok(_content(confidence=70))],
        }
    )
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert stats.consensus_rows == 1
    assert await _cluster_status(db, cluster_id) == "analyzed"
    rows = await _predictions(db)
    assert {row["model"] for row in rows} == {PAIR_A, PAIR_B, CANDIDATES[1], "consensus"}
    assert api.count(CANDIDATES[0]) == 1


async def test_bigger_cluster_processed_first(db, registry) -> None:
    big = await _add_cluster(db, title="Санкции против эмитента", sources=3)
    small = await _add_cluster(db, title="Рядовая новость про ставку", sources=1)
    api = _Api(
        {
            PAIR_A: [_ok(_content(ticker="SBER")), _ok(_content(ticker="LKOH"))],
            PAIR_B: [_ok(_content(ticker="SBER")), _ok(_content(ticker="LKOH"))],
        }
    )
    stats = await _run(db, api, registry)

    assert stats.analyzed == 2
    rows = await db.fetchall(
        "SELECT cluster_id, ticker FROM predictions WHERE model = ?", (PAIR_A,)
    )
    assert {(row["cluster_id"], row["ticker"]) for row in rows} == {
        (big, "SBER"),
        (small, "LKOH"),
    }


async def test_pair_test_state_skips_candidates(db, registry) -> None:
    await _add_cluster(db)
    api = _Api({PAIR_A: [_ok(EMPTY)], PAIR_B: [_ok(EMPTY)]})
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert sorted(api.calls) == sorted([PAIR_A, PAIR_B])
    assert all(model not in CANDIDATES for model in api.calls)
    assert await Repository(db).kv_get("project_state") == "pair_test"


async def test_candidates_limited_by_parallel(db, registry) -> None:
    await Repository(db).kv_set("project_state", "candidates_test")
    await _add_cluster(db)
    settings = _settings()
    settings.llm.candidates_parallel = 2
    api = _Api(
        {
            PAIR_A: [_ok(EMPTY)],
            PAIR_B: [_ok(EMPTY)],
            CANDIDATES[0]: [_ok(EMPTY)],
            CANDIDATES[1]: [_ok(EMPTY)],
        }
    )
    stats = await _run(db, api, registry, settings=settings)

    assert stats.analyzed == 1
    called = [model for model in api.calls if model in CANDIDATES]
    assert sorted(called) == sorted(CANDIDATES)


async def test_candidate_with_done_test_skipped(db, registry) -> None:
    await Repository(db).kv_set("project_state", "candidates_test")
    await db.execute(
        "INSERT INTO model_tests (subject, role, prompt_version, status, started_at, finished_at, "
        "n, success_pct, cost_issued_usd, cost_total_usd, efficiency, verdict) "
        "VALUES (?, 'candidate', 'v2', 'done', ?, ?, 100, 80.0, 0.0, 0.0, 0.0, 'suitable')",
        (CANDIDATES[0], to_iso(NOW), to_iso(NOW)),
    )
    await _add_cluster(db)
    api = _Api(
        {
            PAIR_A: [_ok(EMPTY)],
            PAIR_B: [_ok(EMPTY)],
            CANDIDATES[1]: [_ok(EMPTY)],
            THIRD_CANDIDATE: [_ok(EMPTY)],
        }
    )
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    called = [model for model in api.calls if model not in (PAIR_A, PAIR_B)]
    assert sorted(called) == sorted([CANDIDATES[1], THIRD_CANDIDATE])


async def test_candidate_budget_share_blocks_candidates(db, registry) -> None:
    await Repository(db).kv_set("project_state", "candidates_test")
    await _add_cluster(db)
    await _add_spent(db, 0.36)
    api = _Api({PAIR_A: [_ok(EMPTY)], PAIR_B: [_ok(EMPTY)]})
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert api.count(PAIR_A) == 1
    assert api.count(PAIR_B) == 1
    assert all(model not in CANDIDATES for model in api.calls)


async def test_both_empty_answers_analyzed_without_predictions(db, registry) -> None:
    cluster_id = await _add_cluster(db)
    api = _Api({PAIR_A: [_ok(EMPTY)], PAIR_B: [_ok(EMPTY)]})
    stats = await _run(db, api, registry)

    assert stats.analyzed == 1
    assert stats.issued_rows == 0
    assert stats.consensus_rows == 0
    assert await _cluster_status(db, cluster_id) == "analyzed"
    assert await _predictions(db) == []


async def test_low_confidence_model_blocks_consensus(db, registry) -> None:
    await _add_cluster(db)
    api = _Api(
        {PAIR_A: [_ok(_content(confidence=90))], PAIR_B: [_ok(_content(confidence=80))]}
    )
    stats = await _run(db, api, registry)

    assert stats.consensus_rows == 0
    rows = await _predictions(db)
    assert len(rows) == 2
    issued = {row["model"]: row["is_issued"] for row in rows}
    assert issued[PAIR_A] == 1
    assert issued[PAIR_B] == 0


async def test_model_runs_store_prompt_version(db, registry) -> None:
    await _add_cluster(db)
    api = _Api({PAIR_A: [_ok(_content())], PAIR_B: [_ok(_content())]})
    await _run(db, api, registry)

    rows = await db.fetchall("SELECT DISTINCT prompt_version FROM model_runs")
    assert [row["prompt_version"] for row in rows] == ["v2"]


async def test_old_cluster_not_analyzed(db, registry) -> None:
    old = NOW - timedelta(hours=25)
    await _add_cluster(db, created_at=old)
    api = _Api({})
    stats = await _run(db, api, registry)

    assert api.calls == []
    assert stats.analyzed == 0
    assert stats.skipped_budget == 0


def test_make_pair_key_sorts_ids() -> None:
    assert make_pair_key(["x-ai/b", "anthropic/a"]) == "anthropic/a+x-ai/b"
