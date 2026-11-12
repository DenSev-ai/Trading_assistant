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

    async def spent_since(self, since_utc: datetime) -> float:
        """Сумма model_runs.cost_usd за запросы не раньше since_utc (UTC)."""
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs WHERE created_at >= ?",
            (to_iso(since_utc),),
        )
        return float(row["total"]) if row else 0.0
