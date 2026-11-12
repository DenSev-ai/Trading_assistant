"""Конвейеры сбора новостей, анализа кластеров и проверки прогнозов (шаги 2-4)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .backtest import evaluate
from .clock import get_phase, msk_date, to_iso
from .config import AppSettings, Keywords, ModelSpec, NewsSource, SignalSettings
from .evaluation import Evaluator, Proposal
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
from .market.moex import MoexClient
from .news.clustering import find_cluster
from .news.fulltext import RobotsCache, get_fulltext
from .news.prefilter import normalize_title, passes_prefilter
from .news.sources import RawItem, fetch_feed, parse_feed
from .reports.reports import PROPOSALS_KEY, ReportService
from .repository import Repository
from .stats import Stats
from .telegram.dispatcher import Dispatcher, DispatchStats
from .telegram.health import COLLECT_OK_KEY, SOURCES_DOWN_KEY

logger = logging.getLogger(__name__)

# Окно анализа: кластеры старше суток не анализируются (STEP 3C).
ANALYZE_WINDOW_HOURS = 24
# Состояние проекта в kv: пара набирает выборку / тестируются кандидаты / проект не пригоден.
PROJECT_STATE_KEY = "project_state"
PROJECT_STATE_PAIR_TEST = "pair_test"
PROJECT_STATE_CANDIDATES_TEST = "candidates_test"
CONSENSUS_MODEL = "consensus"
# Виды событийных писем для предложений сервиса (STEP 4D).
PROJECT_RESULT_KIND = "project_result"
PROPOSAL_EVENT_KINDS = {
    "replace_model": "replace_proposal",
    "best_pair": "best_pair_proposal",
}
PROJECT_SUITABLE_KIND = "project_suitable"
PROJECT_UNSUITABLE_KIND = "project_unsuitable"


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

    # Здоровье сервиса: был ли в этом цикле хоть один рабочий источник (HealthMonitor).
    if source_items:
        await deps.repository.kv_set(COLLECT_OK_KEY, to_iso(now))
        await deps.repository.kv_delete(SOURCES_DOWN_KEY)
    else:
        await deps.repository.kv_set(SOURCES_DOWN_KEY, to_iso(now))

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
    reports: ReportService | None = None


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
            await _budget_event(deps, budget, now)
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
            await _fatal_event(deps, cluster_id, pair_results)
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


BASE_PRICE_WINDOW_HOURS = 24


async def _budget_event(deps: AnalyzeDeps, budget: Budget, now: datetime) -> None:
    """Событийное письмо об исчерпании суточного бюджета (один раз за сутки МСК)."""
    if deps.reports is None:
        return
    day = now.astimezone(deps.tz).date().isoformat()
    spent = await budget.spent_today(now)
    await deps.reports.event(
        "budget_exhausted",
        day,
        {
            "text": "Анализ новостей остановлен до следующих суток: кластеры остаются в очереди.",
            "date": day,
            "spent_usd": f"{spent:.4f}",
            "limit_usd": f"{deps.settings.llm.daily_budget_usd:.4f}",
        },
    )


async def _fatal_event(
    deps: AnalyzeDeps, cluster_id: int, results: list[tuple[ModelOutcome, list[RunRecord]]]
) -> None:
    """Событийные письма о фатальном сбое LLM (401/402/403), по одному на модель и кластер."""
    if deps.reports is None:
        return
    for outcome, _ in results:
        if outcome.status != "fatal":
            continue
        await deps.reports.event(
            "llm_fatal",
            f"{outcome.model}:{cluster_id}",
            {
                "text": "Анализ прерван: проверьте ключ и баланс провайдера LLM.",
                "model": outcome.model,
                "cluster_id": cluster_id,
            },
        )
CANDLES_LOOKBACK_DAYS = 7
LAST_BACKTEST_KEY = "last_backtest"


@dataclass
class PriceDeps:
    """Зависимости этапов цены и проверки: клиент MOEX, БД, настройки, реестр, пояс."""
    moex: MoexClient
    repository: Repository
    settings: AppSettings
    registry: InstrumentRegistry
    tz: ZoneInfo
    reports: ReportService | None = None


@dataclass
class BasePriceStats:
    """Счётчики этапа базовых цен: просмотрено, записано, без цены, вне реестра."""
    checked: int = 0
    priced: int = 0
    missing: int = 0
    unknown: int = 0


@dataclass
class BacktestStats:
    """Счётчики этапа проверки: просмотрено, оценено, ждут срока, вне реестра, пропуск."""
    checked: int = 0
    evaluated: int = 0
    waiting: int = 0
    unknown: int = 0
    skipped: bool = False
    proposals: list[Proposal] = field(default_factory=list)


async def base_price_stage(deps: PriceDeps, now: datetime) -> BasePriceStats:
    """Записать базовые цены выданных прогнозов за сутки; цены нет — оставить NULL."""
    stats = BasePriceStats()
    since = now - timedelta(hours=BASE_PRICE_WINDOW_HOURS)
    rows = await deps.repository.predictions_without_base_price(since)
    for row in rows:
        stats.checked += 1
        instrument = deps.registry.get(row["ticker"])
        if instrument is None:
            logger.warning("тикер %s вне реестра: базовая цена не записана", row["ticker"])
            stats.unknown += 1
            continue
        price = await deps.moex.last_price(instrument)
        if price is None:
            stats.missing += 1
            continue
        await deps.repository.set_base_price(int(row["id"]), price)
        stats.priced += 1
    return stats


def _elapsed_sec(iso: str, now: datetime) -> float:
    """Сколько секунд прошло с момента iso (UTC) до now."""
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds()


async def backtest_stage(deps: PriceDeps, now: datetime) -> BacktestStats:
    """Проверить выданные прогнозы по дневным свечам MOEX; не чаще интервала из конфига."""
    stats = BacktestStats()
    last = await deps.repository.kv_get(LAST_BACKTEST_KEY)
    interval = deps.settings.polling.backtest_interval_sec
    if last is not None and _elapsed_sec(last, now) < interval:
        stats.skipped = True
        return stats

    groups: dict[str, list[dict]] = {}
    for row in await deps.repository.unevaluated_predictions():
        groups.setdefault(row["ticker"], []).append(row)

    today_msk = now.astimezone(deps.tz).date()
    for ticker, rows in groups.items():
        instrument = deps.registry.get(ticker)
        if instrument is None:
            logger.warning("тикер %s вне реестра: прогнозы не проверяются", ticker)
            stats.unknown += len(rows)
            continue
        created = [msk_date(row["created_at"], deps.tz) for row in rows]
        start = min(created) - timedelta(days=CANDLES_LOOKBACK_DAYS)
        candles = await deps.moex.daily_candles(instrument, start, today_msk)
        for row, created_msk in zip(rows, created, strict=True):
            stats.checked += 1
            result = evaluate(
                row["direction"],
                int(row["horizon_days"]),
                float(row["expected_change_pct"]),
                created_msk,
                row["base_price"],
                candles,
                today_msk,
                deps.settings.signal.min_move_pct,
            )
            if result is None:
                stats.waiting += 1
                continue
            await deps.repository.set_evaluation(
                int(row["id"]),
                result.check_date.isoformat() if result.check_date is not None else None,
                result.check_price,
                result.max_move_pct,
                result.target_hit,
                result.is_successful,
                to_iso(now),
            )
            stats.evaluated += 1

    await deps.repository.kv_set(LAST_BACKTEST_KEY, to_iso(now))
    stats.proposals = await _evaluation_update(deps, now)
    return stats


async def _evaluation_update(deps: PriceDeps, now: datetime) -> list[Proposal]:
    """Обновить тесты пары, моделей и кандидатов после проверки прогнозов (STEP 4C, 4D)."""
    stats = Stats(deps.repository, deps.settings.signal, deps.settings.evaluation, lambda: now)
    evaluator = Evaluator(deps.repository, stats, deps.settings, lambda: now)
    done_before = {int(test["id"]) for test in await deps.repository.done_tests()}
    proposals = await evaluator.update(now)
    for proposal in proposals:
        logger.info("предложение (%s): %s", proposal.kind, proposal.text)
    if proposals:
        await deps.repository.kv_set(
            PROPOSALS_KEY, "\n".join(f"{p.kind}: {p.text}" for p in proposals)
        )
    await _report_events(deps.repository, deps.reports, done_before, proposals)
    return proposals


@dataclass
class ReportsDeps:
    """Зависимости этапа отчётов: сервис ежедневного, еженедельного и событийных писем."""

    service: ReportService


async def reports_stage(deps: ReportsDeps, now: datetime) -> list[str]:
    """Отправить отчёты, время которых наступило; вернуть виды отправленных отчётов."""
    return await deps.service.run(now)


async def _report_events(
    repo: Repository,
    reports: ReportService | None,
    done_before: set[int],
    proposals: list[Proposal],
) -> None:
    """Событийные письма о закрытых тестах и предложениях сервиса (по одному на событие)."""
    if reports is None:
        return
    for test in await repo.done_tests():
        if int(test["id"]) in done_before:
            continue
        await reports.event("test_done", str(test["id"]), _test_payload(test))
    for proposal in proposals:
        kind = PROPOSAL_EVENT_KINDS.get(proposal.kind)
        if proposal.kind == PROJECT_RESULT_KIND:
            kind = await _project_result_kind(repo)
        if kind is None:
            continue
        await reports.event(kind, proposal.text, {"text": proposal.text})


async def _project_result_kind(repo: Repository) -> str:
    """Вид события по состоянию проекта после закрытия теста пары: пригоден / не пригоден."""
    state = await repo.kv_get(PROJECT_STATE_KEY)
    if state == PROJECT_STATE_CANDIDATES_TEST:
        return PROJECT_SUITABLE_KIND
    return PROJECT_UNSUITABLE_KIND


def _test_payload(test: dict) -> dict:
    """Данные события о завершённом тесте: вердикт, выборка, точность, стоимость."""
    return {
        "text": (
            f"{test['subject']} ({test['role']}): вердикт {test['verdict']}, "
            f"сбылось {float(test['success_pct'] or 0.0):.1f}% (n={test['n']}), "
            f"стоимость выданных ${float(test['cost_issued_usd'] or 0.0):.4f}, "
            f"эффективность {test['efficiency']}"
        ),
    }


@dataclass
class DispatchDeps:
    """Зависимости этапа доставки: диспетчер Telegram, настройки и пояс."""

    dispatcher: Dispatcher
    settings: AppSettings
    tz: ZoneInfo


async def dispatch_stage(deps: DispatchDeps, now: datetime) -> DispatchStats:
    """Отправить сигналы и утренний дайджест по фазе расписания (шестой этап цикла)."""
    phase = get_phase(now, deps.settings.schedule, deps.tz)
    return await deps.dispatcher.dispatch(phase, now)

