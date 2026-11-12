"""Конвейеры сбора новостей и анализа кластеров моделями (шаги 2-3)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import AppSettings, Keywords, ModelSpec, NewsSource, SignalSettings
from .instruments import InstrumentRegistry
from .llm.analyzer import (
    ModelOutcome,
    RunRecord,
    build_system_prompt,
    build_user_message,
    run_model,
)
from .llm.budget import Budget
from .llm.client import OpenRouterClient
from .llm.consensus import ConsensusRow, build_consensus, is_issued, make_pair_key
from .news.clustering import find_cluster
from .news.fulltext import RobotsCache, get_fulltext
from .news.prefilter import normalize_title, passes_prefilter
from .news.sources import RawItem, fetch_feed, parse_feed
from .repository import Repository

logger = logging.getLogger(__name__)

# Окно анализа: кластеры старше суток не анализируются (STEP 3C).
ANALYZE_WINDOW_HOURS = 24
# Состояние проекта в kv: пара набирает выборку / тестируются кандидаты / проект не пригоден.
PROJECT_STATE_KEY = "project_state"
PROJECT_STATE_PAIR_TEST = "pair_test"
PROJECT_STATE_CANDIDATES_TEST = "candidates_test"
CONSENSUS_MODEL = "consensus"


@dataclass
class CollectDeps:
    client: httpx.AsyncClient
    repository: Repository
    settings: AppSettings
    keywords: Keywords


@dataclass
class CollectStats:
    fetched: int = 0
    duplicates: int = 0
    filtered: int = 0
    no_fulltext: int = 0
    clustered: int = 0
    new_clusters: int = 0


async def collect_stage(deps: CollectDeps, now: datetime) -> CollectStats:
    """Один этап сбора новостей для всех включённых источников.

    Загрузка лент — параллельно (asyncio.gather), обработка записей —
    последовательно, чтобы кластеризация была детерминирована.
    """
    stats = CollectStats()
    enabled = [s for s in deps.settings.news.sources if s.enabled]

    # Этап 1: параллельно загружаем и парсим все ленты.
    async def fetch_and_parse(src: NewsSource) -> list[RawItem]:
        xml = await fetch_feed(deps.client, src)
        if xml is None:
            return []
        return await asyncio.to_thread(
            parse_feed,
            src.name,
            xml,
            now,
            deps.settings.news.window_hours,
            deps.settings.news.max_items_per_feed,
        )

    results = await asyncio.gather(
        *(fetch_and_parse(s) for s in enabled), return_exceptions=True
    )
    source_items: list[tuple[NewsSource, list[RawItem]]] = []
    for src, result in zip(enabled, results, strict=False):
        if isinstance(result, Exception):
            logger.warning("источник %s упал: %s", src.name, result)
            continue
        stats.fetched += len(result)
        source_items.append((src, result))

    # Этап 2: обрабатываем записи последовательно (детерминированная кластеризация).
    robots = RobotsCache(deps.client, deps.settings.news.user_agent)
    for src, items in source_items:
        for item in items:
            if await deps.repository.article_exists(item.url):
                stats.duplicates += 1
                continue
            if not passes_prefilter(item.title, item.summary, deps.keywords):
                await deps.repository.add_article(
                    item, normalize_title(item.title), "filtered", None, now
                )
                stats.filtered += 1
                continue
            fulltext = await get_fulltext(
                deps.client, robots, item, src, deps.settings.news.max_fulltext_bytes
            )
            if fulltext is None or len(fulltext) < deps.settings.news.min_fulltext_chars:
                await deps.repository.add_article(
                    item, normalize_title(item.title), "no_fulltext", fulltext, now
                )
                stats.no_fulltext += 1
                continue
            title_norm = normalize_title(item.title)
            window_start = now - timedelta(hours=deps.settings.news.cluster_window_hours)
            candidates = await deps.repository.recent_clusters(window_start)
            cluster_id = find_cluster(
                title_norm,
                [(r["id"], r["title_norm"]) for r in candidates],
                deps.settings.news.cluster_similarity,
            )
            article_id = await deps.repository.add_article(
                item, title_norm, "clustered", fulltext, now
            )
            if cluster_id is None:
                await deps.repository.create_cluster(article_id, item.title, title_norm, now)
                stats.new_clusters += 1
            else:
                await deps.repository.attach_to_cluster(article_id, cluster_id, src.name, now)
            stats.clustered += 1

    return stats


@dataclass
class AnalyzeDeps:
    """Зависимости этапа анализа: клиент LLM, БД, настройки, реестр, шаблон промпта, пояс."""
    client: OpenRouterClient
    repository: Repository
    settings: AppSettings
    registry: InstrumentRegistry
    prompt_template: str
    tz: ZoneInfo


@dataclass
class AnalyzeStats:
    """Счётчики этапа анализа: кластеры, строки прогнозов и расход."""
    analyzed: int = 0
    failed: int = 0
    issued_rows: int = 0
    consensus_rows: int = 0
    skipped_budget: int = 0
    cost_usd: float = 0.0


def _published_msk(iso: str, tz: ZoneInfo) -> str:
    """Дата публикации статьи для промпта: 'ДД.ММ.ГГГГ ЧЧ:ММ' по местному времени."""
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def _user_message(row: dict, cfg: AppSettings, tz: ZoneInfo) -> str:
    """Пользовательское сообщение с метаданными и текстом главной статьи кластера."""
    return build_user_message(
        title=row["title"],
        text=row["fulltext"] or "",
        sources_count=int(row["sources_count"]),
        max_chars=cfg.llm.max_text_chars,
        published_msk=_published_msk(row["published_at"], tz),
        source=row["source"],
    )


async def _call_model(
    deps: AnalyzeDeps,
    semaphore: asyncio.Semaphore,
    model: ModelSpec,
    system: str,
    user: str,
) -> tuple[ModelOutcome, list[RunRecord]]:
    """Вызвать модель с повторами, соблюдая общий лимит одновременных вызовов."""
    async with semaphore:
        return await run_model(
            deps.client, model, system, user, deps.settings.llm, deps.registry
        )


async def _save_runs(
    repository: Repository,
    cluster_id: int,
    records: list[RunRecord],
    prompt_version: str,
    now: datetime,
) -> int | None:
    """Записать все HTTP-запросы вызова модели; вернуть id успешного (для run_id)."""
    ok_run_id: int | None = None
    for record in records:
        run_id = await repository.add_model_run(
            cluster_id=cluster_id,
            model=record.model,
            prompt_version=prompt_version,
            status=record.status,
            raw_response=record.raw_response,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            cost_usd=record.cost_usd,
            latency_ms=record.latency_ms,
            now=now,
        )
        if record.status == "ok":
            ok_run_id = run_id
    return ok_run_id


async def _save_predictions(
    repository: Repository,
    cluster_id: int,
    outcome: ModelOutcome,
    run_id: int | None,
    prompt_version: str,
    signal: SignalSettings,
    now: datetime,
) -> int:
    """Сохранить прогнозы модели по её инструментам; вернуть число выданных (is_issued=1)."""
    analysis = outcome.analysis
    if analysis is None:
        return 0
    issued = 0
    for item in analysis.instruments:
        flag = is_issued(item, analysis.severity, signal)
        inserted = await repository.add_prediction(
            cluster_id=cluster_id,
            model=outcome.model,
            ticker=item.ticker,
            direction=item.direction,
            confidence=item.confidence,
            severity=analysis.severity,
            horizon_days=item.horizon_days,
            expected_change_pct=item.expected_change_pct,
            reason=item.reason,
            created_at=now,
            prompt_version=prompt_version,
            is_issued=flag,
            run_id=run_id,
            pair_key=None,
        )
        if inserted is not None and flag:
            issued += 1
    return issued


async def _save_consensus(
    repository: Repository,
    cluster_id: int,
    rows: list[ConsensusRow],
    prompt_version: str,
    pair_key: str,
    now: datetime,
) -> int:
    """Сохранить строки консенсуса пары (is_issued=1, run_id=None); вернуть число записей."""
    saved = 0
    for row in rows:
        inserted = await repository.add_prediction(
            cluster_id=cluster_id,
            model=CONSENSUS_MODEL,
            ticker=row.ticker,
            direction=row.direction,
            confidence=row.confidence,
            severity=row.severity,
            horizon_days=row.horizon_days,
            expected_change_pct=row.expected_change_pct,
            reason=row.reason,
            created_at=now,
            prompt_version=prompt_version,
            is_issued=1,
            run_id=None,
            pair_key=pair_key,
        )
        if inserted is not None:
            saved += 1
    return saved


async def analyze_stage(deps: AnalyzeDeps, now: datetime) -> AnalyzeStats:
    """Один этап анализа: свежие pending-кластеры -> модели -> прогнозы и консенсус."""
    stats = AnalyzeStats()
    cfg = deps.settings
    repository = deps.repository

    state = await repository.kv_get(PROJECT_STATE_KEY)
    if state is None:
        state = PROJECT_STATE_PAIR_TEST
        await repository.kv_set(PROJECT_STATE_KEY, state)

    system = build_system_prompt(deps.prompt_template, deps.registry, cfg.signal)
    budget = Budget(repository, cfg.llm.daily_budget_usd, deps.tz)
    semaphore = asyncio.Semaphore(cfg.llm.concurrency)
    pair_key = make_pair_key([model.id for model in cfg.llm.pair])

    candidates: list[ModelSpec] = []
    if state == PROJECT_STATE_CANDIDATES_TEST:
        done = await repository.subjects_with_done_test(cfg.llm.prompt_version)
        pending = [model for model in cfg.llm.candidates if model.id not in done]
        candidates = pending[: cfg.llm.candidates_parallel]

    since = now - timedelta(hours=ANALYZE_WINDOW_HOURS)
    clusters = await repository.pending_clusters(since)

    for position, cluster in enumerate(clusters):
        if not await budget.allowed(now):
            stats.skipped_budget = len(clusters) - position
            logger.warning(
                "суточный бюджет исчерпан: анализ остановлен, кластеров отложено %d",
                stats.skipped_budget,
            )
            break

        cluster_id = int(cluster["id"])
        text = await repository.cluster_text(cluster_id)
        if text is None:
            logger.warning("у кластера %s нет главной статьи — помечен failed", cluster_id)
            await repository.set_cluster_status(cluster_id, "failed")
            stats.failed += 1
            continue

        user = _user_message(text, cfg, deps.tz)
        pair_results = await asyncio.gather(
            *(_call_model(deps, semaphore, model, system, user) for model in cfg.llm.pair)
        )
        run_ids: dict[str, int | None] = {}
        for outcome, records in pair_results:
            run_ids[outcome.model] = await _save_runs(
                repository, cluster_id, records, cfg.llm.prompt_version, now
            )
            stats.cost_usd += outcome.cost_usd

        if any(outcome.status == "fatal" for outcome, _ in pair_results):
            logger.error(
                "фатальная ошибка модели: анализ прерван, кластер %s остаётся pending",
                cluster_id,
            )
            return stats

        if candidates and await budget.allowed(
            now, reserve_share=cfg.llm.candidate_budget_share
        ):
            candidate_results = await asyncio.gather(
                *(_call_model(deps, semaphore, model, system, user) for model in candidates)
            )
            for outcome, records in candidate_results:
                candidate_run_id = await _save_runs(
                    repository, cluster_id, records, cfg.llm.prompt_version, now
                )
                stats.cost_usd += outcome.cost_usd
                stats.issued_rows += await _save_predictions(
                    repository,
                    cluster_id,
                    outcome,
                    candidate_run_id,
                    cfg.llm.prompt_version,
                    cfg.signal,
                    now,
                )

        if any(outcome.status != "ok" for outcome, _ in pair_results):
            await repository.set_cluster_status(cluster_id, "failed")
            stats.failed += 1
            continue

        for outcome, _ in pair_results:
            stats.issued_rows += await _save_predictions(
                repository,
                cluster_id,
                outcome,
                run_ids[outcome.model],
                cfg.llm.prompt_version,
                cfg.signal,
                now,
            )

        first = pair_results[0][0].analysis
        second = pair_results[1][0].analysis
        if first is not None and second is not None:
            stats.consensus_rows += await _save_consensus(
                repository,
                cluster_id,
                build_consensus(first, second, cfg.signal),
                cfg.llm.prompt_version,
                pair_key,
                now,
            )
        await repository.set_cluster_status(cluster_id, "analyzed")
        stats.analyzed += 1

    return stats
