"""Репозиторий статей, кластеров и расходов на модели (все SQL здесь, не в pipeline)."""
from __future__ import annotations

from datetime import datetime

from .clock import to_iso
from .db import Database
from .news.sources import RawItem


class Repository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def article_exists(self, url: str) -> bool:
        row = await self._db.fetchone("SELECT 1 FROM articles WHERE url = ?", (url,))
        return row is not None

    async def add_article(
        self,
        item: RawItem,
        title_norm: str,
        status: str,
        fulltext: str | None,
        now: datetime,
    ) -> int:
        return await self._db.execute(
            "INSERT INTO articles (url, source, title, title_norm, summary, "
            "published_at, fetched_at, fulltext, status, cluster_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                item.url,
                item.source,
                item.title,
                title_norm,
                item.summary,
                to_iso(item.published_at),
                to_iso(now),
                fulltext,
                status,
            ),
        )

    async def recent_clusters(self, since: datetime) -> list[dict]:
        return await self._db.fetchall(
            "SELECT id, title_norm FROM clusters WHERE created_at >= ? ORDER BY created_at DESC",
            (to_iso(since),),
        )

    async def create_cluster(
        self, article_id: int, title: str, title_norm: str, now: datetime
    ) -> int:
        cluster_id = await self._db.execute(
            "INSERT INTO clusters (title, title_norm, created_at, updated_at, "
            "sources_count, status) VALUES (?, ?, ?, ?, 1, 'pending')",
            (title, title_norm, to_iso(now), to_iso(now)),
        )
        await self._db.execute(
            "UPDATE articles SET cluster_id = ? WHERE id = ?",
            (cluster_id, article_id),
        )
        return cluster_id

    async def attach_to_cluster(
        self, article_id: int, cluster_id: int, source: str, now: datetime
    ) -> None:
        """Присоединить статью к кластеру. Пересчитать sources_count и main_article_id."""
        await self._db.execute(
            "UPDATE articles SET cluster_id = ? WHERE id = ?",
            (cluster_id, article_id),
        )
        rows = await self._db.fetchall(
            "SELECT COUNT(DISTINCT source) AS cnt FROM articles WHERE cluster_id = ?",
            (cluster_id,),
        )
        sources_count = rows[0]["cnt"] if rows else 1
        latest = await self._db.fetchone(
            "SELECT id FROM articles WHERE cluster_id = ? ORDER BY published_at DESC LIMIT 1",
            (cluster_id,),
        )
        main_article_id = latest["id"] if latest else article_id
        await self._db.execute(
            "UPDATE clusters SET sources_count = ?, updated_at = ?, main_article_id = ? "
            "WHERE id = ?",
            (sources_count, to_iso(now), main_article_id, cluster_id),
        )

    async def pending_clusters(self, since: datetime) -> list[dict]:
        """Кластеры status='pending' не старше since: сначала по числу источников, затем новые."""
        return await self._db.fetchall(
            "SELECT id, sources_count, created_at FROM clusters "
            "WHERE status = 'pending' AND created_at >= ? "
            "ORDER BY sources_count DESC, created_at DESC",
            (to_iso(since),),
        )

    async def cluster_text(self, cluster_id: int) -> dict | None:
        """Данные главной статьи кластера; None, если у кластера нет ни одной статьи."""
        sql = (
            "SELECT a.title AS title, a.fulltext AS fulltext, a.source AS source, "
            "a.published_at AS published_at, c.sources_count AS sources_count "
            "FROM clusters AS c "
        )
        row = await self._db.fetchone(
            sql + "LEFT JOIN articles AS a ON a.id = c.main_article_id WHERE c.id = ?",
            (cluster_id,),
        )
        if row is not None and row["title"] is not None:
            return row
        # main_article_id ещё не выставлен: берём самую свежую статью кластера.
        return await self._db.fetchone(
            sql + "JOIN articles AS a ON a.cluster_id = c.id "
            "WHERE c.id = ? ORDER BY a.published_at DESC LIMIT 1",
            (cluster_id,),
        )

    async def add_model_run(
        self,
        cluster_id: int,
        model: str,
        prompt_version: str,
        status: str,
        raw_response: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
        now: datetime,
    ) -> int:
        """Записать один HTTP-запрос к модели в model_runs; вернуть id записи."""
        return await self._db.execute(
            "INSERT INTO model_runs (cluster_id, model, prompt_version, status, raw_response, "
            "tokens_in, tokens_out, cost_usd, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cluster_id,
                model,
                prompt_version,
                status,
                raw_response,
                tokens_in,
                tokens_out,
                cost_usd,
                latency_ms,
                to_iso(now),
            ),
        )

    async def add_prediction(
        self,
        cluster_id: int,
        model: str,
        ticker: str,
        direction: str,
        confidence: int,
        severity: int,
        horizon_days: int,
        expected_change_pct: float,
        reason: str,
        created_at: datetime,
        prompt_version: str,
        is_issued: bool | int,
        run_id: int | None,
        pair_key: str | None,
    ) -> int | None:
        """Записать прогноз; дубль по UNIQUE(cluster_id, model, ticker) игнорировать -> None."""
        existing = await self._db.fetchone(
            "SELECT id FROM predictions WHERE cluster_id = ? AND model = ? AND ticker = ?",
            (cluster_id, model, ticker),
        )
        if existing is not None:
            return None
        return await self._db.execute(
            "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
            "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
            "is_issued, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cluster_id,
                model,
                ticker,
                direction,
                confidence,
                severity,
                horizon_days,
                expected_change_pct,
                reason,
                to_iso(created_at),
                prompt_version,
                pair_key,
                1 if is_issued else 0,
                run_id,
            ),
        )

    async def set_cluster_status(self, cluster_id: int, status: str) -> None:
        """Сменить статус кластера (pending|analyzed|failed|skipped)."""
        await self._db.execute(
            "UPDATE clusters SET status = ? WHERE id = ?",
            (status, cluster_id),
        )

    async def subjects_with_done_test(self, prompt_version: str) -> set[str]:
        """Модели, чей тест уже завершён (model_tests.status='done') для этой версии промта."""
        rows = await self._db.fetchall(
            "SELECT DISTINCT subject FROM model_tests WHERE prompt_version = ? AND status = 'done'",
            (prompt_version,),
        )
        return {row["subject"] for row in rows}

    async def kv_get(self, key: str) -> str | None:
        """Значение ключа из kv или None, если ключа нет."""
        row = await self._db.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row is not None else None

    async def kv_set(self, key: str, value: str) -> None:
        """Записать значение в kv (вставка или обновление)."""
        await self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def spent_since(self, since_utc: datetime) -> float:
        """Сумма model_runs.cost_usd за запросы не раньше since_utc (UTC)."""
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs WHERE created_at >= ?",
            (to_iso(since_utc),),
        )
        return float(row["total"]) if row else 0.0
