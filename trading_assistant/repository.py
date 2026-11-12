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

    async def predictions_without_base_price(self, since: datetime) -> list[dict]:
        """Выданные прогнозы без базовой цены, созданные не раньше since."""
        return await self._db.fetchall(
            "SELECT id, ticker, created_at FROM predictions "
            "WHERE is_issued = 1 AND base_price IS NULL AND created_at >= ? "
            "ORDER BY created_at, id",
            (to_iso(since),),
        )

    async def set_base_price(self, prediction_id: int, price: float) -> None:
        """Записать базовую цену прогноза."""
        await self._db.execute(
            "UPDATE predictions SET base_price = ? WHERE id = ?",
            (price, prediction_id),
        )

    async def unevaluated_predictions(self) -> list[dict]:
        """Выданные прогнозы, проверка которых ещё не завершена (evaluated_at IS NULL)."""
        return await self._db.fetchall(
            "SELECT id, ticker, direction, horizon_days, expected_change_pct, base_price, "
            "created_at FROM predictions WHERE is_issued = 1 AND evaluated_at IS NULL "
            "ORDER BY created_at, id"
        )

    async def set_evaluation(
        self,
        prediction_id: int,
        check_date: str | None,
        check_price: float | None,
        max_move_pct: float | None,
        target_hit: int | None,
        is_successful: int | None,
        evaluated_at: str,
    ) -> None:
        """Записать результат проверки прогноза (check_date — ISO-дата или None)."""
        await self._db.execute(
            "UPDATE predictions SET check_date = ?, check_price = ?, max_move_pct = ?, "
            "target_hit = ?, is_successful = ?, evaluated_at = ? WHERE id = ?",
            (
                check_date,
                check_price,
                max_move_pct,
                target_hit,
                is_successful,
                evaluated_at,
                prediction_id,
            ),
        )

    async def subjects_with_done_test(self, prompt_version: str) -> set[str]:
        """Модели, чей тест уже завершён (model_tests.status='done') для этой версии промта."""
        rows = await self._db.fetchall(
            "SELECT DISTINCT subject FROM model_tests WHERE prompt_version = ? AND status = 'done'",
            (prompt_version,),
        )
        return {row["subject"] for row in rows}

    async def get_or_create_test(
        self, subject: str, role: str, prompt_version: str, now: datetime
    ) -> dict:
        """Запись model_tests по (subject, role, prompt_version); если её нет — создать running."""
        await self._db.execute(
            "INSERT OR IGNORE INTO model_tests (subject, role, prompt_version, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (subject, role, prompt_version, to_iso(now)),
        )
        row = await self._db.fetchone(
            "SELECT * FROM model_tests WHERE subject = ? AND role = ? AND prompt_version = ?",
            (subject, role, prompt_version),
        )
        if row is None:
            raise RuntimeError(f"не создать запись model_tests для {subject}/{role}")
        return row

    async def running_tests(self) -> list[dict]:
        """Незавершённые тесты (status='running') в порядке старта."""
        return await self._db.fetchall(
            "SELECT * FROM model_tests WHERE status = 'running' ORDER BY started_at, id"
        )

    async def done_tests(self) -> list[dict]:
        """Завершённые тесты (status='done') в порядке старта."""
        return await self._db.fetchall(
            "SELECT * FROM model_tests WHERE status = 'done' ORDER BY started_at, id"
        )

    async def close_test(
        self,
        test_id: int,
        n: int,
        success_pct: float,
        cost_issued: float,
        cost_total: float,
        efficiency: float | None,
        verdict: str,
        now: datetime,
    ) -> None:
        """Закрыть тест один раз: вердикт и цифры больше не меняются."""
        await self._db.execute(
            "UPDATE model_tests SET status = 'done', finished_at = ?, n = ?, success_pct = ?, "
            "cost_issued_usd = ?, cost_total_usd = ?, efficiency = ?, verdict = ? "
            "WHERE id = ? AND status = 'running'",
            (
                to_iso(now),
                n,
                success_pct,
                cost_issued,
                cost_total,
                efficiency,
                verdict,
                test_id,
            ),
        )

    async def issued_predictions(
        self,
        model: str,
        pair_key: str | None = None,
        prompt_version: str | None = None,
    ) -> list[dict]:
        """Выданные прогнозы модели (для 'consensus' — с pair_key) в порядке создания."""
        sql = (
            "SELECT id, created_at, evaluated_at, is_successful, cluster_id, run_id, ticker, "
            "pair_key FROM predictions WHERE model = ? AND is_issued = 1"
        )
        params: list[object] = [model]
        if pair_key is not None:
            sql += " AND pair_key = ?"
            params.append(pair_key)
        if prompt_version is not None:
            sql += " AND prompt_version = ?"
            params.append(prompt_version)
        sql += " ORDER BY created_at, id"
        return await self._db.fetchall(sql, tuple(params))

    async def runs_cost(
        self,
        model: str,
        cluster_ids: list[int] | None = None,
        only_ok: bool = False,
    ) -> float:
        """Стоимость запросов модели: всех или по указанным кластерам, при only_ok — успешных."""
        if cluster_ids is not None and not cluster_ids:
            return 0.0
        sql = "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs WHERE model = ?"
        params: list[object] = [model]
        if cluster_ids is not None:
            placeholders = ", ".join("?" for _ in cluster_ids)
            sql += f" AND cluster_id IN ({placeholders})"
            params.extend(cluster_ids)
        if only_ok:
            sql += " AND status = 'ok'"
        row = await self._db.fetchone(sql, tuple(params))
        return float(row["total"]) if row is not None else 0.0

    async def distinct_run_cost(self, run_ids: list[int]) -> float:
        """Суммарная стоимость указанных запросов (каждый запрос считается один раз)."""
        if not run_ids:
            return 0.0
        placeholders = ", ".join("?" for _ in run_ids)
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs "
            f"WHERE id IN ({placeholders})",
            tuple(run_ids),
        )
        return float(row["total"]) if row is not None else 0.0

    async def success_counts(
        self,
        model: str,
        ticker: str | None = None,
        pair_key: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[int, int]:
        """Сколько проверенных выданных прогнозов модели и сколько из них сбылось."""
        sql = (
            "SELECT COUNT(*) AS n, COALESCE(SUM(is_successful), 0) AS ok FROM predictions "
            "WHERE model = ? AND is_issued = 1 AND evaluated_at IS NOT NULL "
            "AND is_successful IS NOT NULL"
        )
        params: list[object] = [model]
        if ticker is not None:
            sql += " AND ticker = ?"
            params.append(ticker)
        if pair_key is not None:
            sql += " AND pair_key = ?"
            params.append(pair_key)
        if prompt_version is not None:
            sql += " AND prompt_version = ?"
            params.append(prompt_version)
        row = await self._db.fetchone(sql, tuple(params))
        if row is None:
            return 0, 0
        return int(row["ok"]), int(row["n"])

    async def undelivered_signals(
        self, pair_key: str, prompt_version: str, since: datetime | None = None
    ) -> list[dict]:
        """Сигналы consensus текущей пары без записи в deliveries, в порядке создания."""
        sql = (
            "SELECT p.id, p.ticker, p.direction, p.confidence, p.horizon_days, "
            "p.expected_change_pct, p.reason, p.severity, p.created_at, "
            "c.sources_count AS sources_count, a.url AS url, a.title AS title "
            "FROM predictions p JOIN clusters c ON c.id = p.cluster_id "
            "LEFT JOIN articles a ON a.id = c.main_article_id "
            "WHERE p.model = 'consensus' AND p.is_issued = 1 AND p.pair_key = ? "
            "AND p.prompt_version = ? AND NOT EXISTS "
            "(SELECT 1 FROM deliveries d WHERE d.prediction_id = p.id)"
        )
        params: list[object] = [pair_key, prompt_version]
        if since is not None:
            sql += " AND p.created_at >= ?"
            params.append(to_iso(since))
        sql += " ORDER BY p.created_at, p.id"
        return await self._db.fetchall(sql, tuple(params))

    async def last_delivery_at(self, ticker: str, kinds: tuple[str, ...]) -> str | None:
        """Время последней доставки по тикеру среди указанных видов (None — не было)."""
        if not kinds:
            return None
        placeholders = ", ".join("?" for _ in kinds)
        row = await self._db.fetchone(
            "SELECT MAX(sent_at) AS last FROM deliveries "
            f"WHERE ticker = ? AND kind IN ({placeholders})",
            (ticker, *kinds),
        )
        return row["last"] if row is not None and row["last"] is not None else None

    async def add_delivery(
        self,
        prediction_id: int,
        ticker: str,
        kind: str,
        now: datetime,
        chat_id: str | None = None,
        error: str | None = None,
    ) -> int:
        """Записать доставку сигнала: вид alert|digest|suppressed, время и подробности."""
        return await self._db.execute(
            "INSERT INTO deliveries (prediction_id, ticker, kind, chat_id, sent_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (prediction_id, ticker, kind, chat_id, to_iso(now), error),
        )

    async def delivery_count(self, kind: str, since: datetime, until: datetime) -> int:
        """Сколько доставок указанного вида попало в интервал [since, until)."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM deliveries WHERE kind = ? AND sent_at >= ? AND sent_at < ?",
            (kind, to_iso(since), to_iso(until)),
        )
        return int(row["n"]) if row is not None else 0

    async def last_signals(self, limit: int, pair_key: str, prompt_version: str) -> list[dict]:
        """Последние сигналы consensus текущей пары, свежие первыми."""
        return await self._db.fetchall(
            "SELECT ticker, direction, horizon_days, expected_change_pct, confidence, created_at "
            "FROM predictions WHERE model = 'consensus' AND is_issued = 1 AND pair_key = ? "
            "AND prompt_version = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (pair_key, prompt_version, limit),
        )

    async def pending_cluster_count(self) -> int:
        """Сколько кластеров ещё ждут анализа."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM clusters WHERE status = 'pending'"
        )
        return int(row["n"]) if row is not None else 0

    async def ticker_success_counts(
        self, pair_key: str, prompt_version: str, limit: int
    ) -> list[dict]:
        """Точность consensus по тикерам: самые частые тикеры с проверенными прогнозами."""
        return await self._db.fetchall(
            "SELECT ticker, COUNT(*) AS n, COALESCE(SUM(is_successful), 0) AS ok FROM predictions "
            "WHERE model = 'consensus' AND is_issued = 1 AND evaluated_at IS NOT NULL "
            "AND is_successful IS NOT NULL AND pair_key = ? AND prompt_version = ? "
            "GROUP BY ticker ORDER BY n DESC, ticker LIMIT ?",
            (pair_key, prompt_version, limit),
        )

    async def recent_fatal_run(self, since: datetime) -> dict | None:
        """Последний фатальный сбой LLM (401/402/403) не раньше since."""
        return await self._db.fetchone(
            "SELECT model, created_at FROM model_runs WHERE status = 'fatal' "
            "AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (to_iso(since),),
        )

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

    async def kv_delete(self, key: str) -> None:
        """Удалить ключ из kv (сброс флага состояния); отсутствие ключа не ошибка."""
        await self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    async def spent_since(self, since_utc: datetime) -> float:
        """Сумма model_runs.cost_usd за запросы не раньше since_utc (UTC)."""
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs WHERE created_at >= ?",
            (to_iso(since_utc),),
        )
        return float(row["total"]) if row else 0.0

    async def spent_between(self, since: datetime, until: datetime) -> float:
        """Сумма model_runs.cost_usd за запросы в интервале [since, until)."""
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs "
            "WHERE created_at >= ? AND created_at < ?",
            (to_iso(since), to_iso(until)),
        )
        return float(row["total"]) if row else 0.0

    async def total_spent(self) -> float:
        """Весь расход на вызовы моделей за всё время."""
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM model_runs"
        )
        return float(row["total"]) if row else 0.0

    async def activity_counts(self, since: datetime, until: datetime) -> dict:
        """Счётчики за интервал [since, until): статьи, кластеры, прогнозы, доставки."""
        window = (to_iso(since), to_iso(until))
        articles = await self._db.fetchone(
            "SELECT COUNT(*) AS collected, COALESCE(SUM(status = 'filtered'), 0) AS filtered "
            "FROM articles WHERE fetched_at >= ? AND fetched_at < ?",
            window,
        )
        clusters = await self._db.fetchone(
            "SELECT COUNT(*) AS clusters, COALESCE(SUM(status = 'analyzed'), 0) AS analyzed "
            "FROM clusters WHERE created_at >= ? AND created_at < ?",
            window,
        )
        predictions = await self._db.fetchone(
            "SELECT COALESCE(SUM(model <> 'consensus' AND is_issued = 1), 0) AS issued, "
            "COALESCE(SUM(model = 'consensus'), 0) AS consensus FROM predictions "
            "WHERE created_at >= ? AND created_at < ?",
            window,
        )
        deliveries = await self._db.fetchone(
            "SELECT COALESCE(SUM(kind = 'alert'), 0) AS alerts, "
            "COALESCE(SUM(kind = 'digest'), 0) AS digest FROM deliveries "
            "WHERE sent_at >= ? AND sent_at < ?",
            window,
        )
        counts: dict[str, int] = {}
        for row in (articles, clusters, predictions, deliveries):
            if row is not None:
                counts.update({key: int(value) for key, value in row.items()})
        return counts

    async def prediction_totals(self) -> dict:
        """Число выданных, невыданных и ещё не проверенных прогнозов (охват и ожидание)."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_issued), 0) AS issued, "
            "COALESCE(SUM(CASE WHEN is_issued = 1 AND evaluated_at IS NULL "
            "THEN 1 ELSE 0 END), 0) AS pending FROM predictions"
        )
        if row is None:
            return {"total": 0, "issued": 0, "pending": 0}
        return {
            "total": int(row["total"]),
            "issued": int(row["issued"]),
            "pending": int(row["pending"]),
        }

    async def evaluated_since(self, since: datetime) -> list[dict]:
        """Проверенные выданные прогнозы, проверка которых завершена не раньше since."""
        return await self._db.fetchall(
            "SELECT id, model, ticker, is_successful, target_hit, evaluated_at, created_at "
            "FROM predictions WHERE is_issued = 1 AND evaluated_at IS NOT NULL "
            "AND evaluated_at >= ? ORDER BY evaluated_at, id",
            (to_iso(since),),
        )

