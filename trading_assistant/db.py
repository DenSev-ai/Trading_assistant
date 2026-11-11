"""Работа с SQLite через aiosqlite (CONTRACTS §1)."""
from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite

from .clock import to_iso

DDL = """
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sources_count INTEGER NOT NULL DEFAULT 1,
  main_article_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  summary TEXT,
  published_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  fulltext TEXT,
  status TEXT NOT NULL,
  cluster_id INTEGER REFERENCES clusters(id)
);
CREATE INDEX IF NOT EXISTS ix_articles_fetched ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS ix_clusters_created ON clusters(created_at);
CREATE TABLE IF NOT EXISTS model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL,
  raw_response TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_created ON model_runs(created_at);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  model TEXT NOT NULL,
  ticker TEXT NOT NULL,
  direction TEXT NOT NULL,
  confidence INTEGER NOT NULL,
  severity INTEGER NOT NULL,
  horizon_days INTEGER NOT NULL,
  expected_change_pct REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  pair_key TEXT,
  is_issued INTEGER NOT NULL DEFAULT 0,
  run_id INTEGER REFERENCES model_runs(id),
  base_price REAL,
  check_date TEXT,
  check_price REAL,
  max_move_pct REAL,
  target_hit INTEGER,
  is_successful INTEGER,
  evaluated_at TEXT,
  UNIQUE(cluster_id, model, ticker)
);
CREATE TABLE IF NOT EXISTS model_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  success_pct REAL NOT NULL DEFAULT 0,
  cost_issued_usd REAL NOT NULL DEFAULT 0,
  verdict TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER REFERENCES predictions(id),
  chat_id TEXT NOT NULL,
  status TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

TABLES = ("clusters", "articles", "model_runs", "predictions", "model_tests", "deliveries", "kv")


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(DDL)
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        assert self._conn is not None, "Database.init() не вызван"
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.lastrowid

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._conn is not None, "Database.init() не вызван"
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in rows]

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        assert self._conn is not None, "Database.init() не вызван"
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        return dict(zip(columns, row, strict=False))

    async def cleanup(self, now: datetime, days: int = 30) -> None:
        """CONTRACTS §1 «Очистка»: статьи и ответы старше days дней -> текст обнуляется."""
        assert self._conn is not None, "Database.init() не вызван"
        cutoff = to_iso(now - timedelta(days=days))
        await self._conn.execute(
            "UPDATE articles SET fulltext = NULL WHERE fulltext IS NOT NULL AND fetched_at < ?",
            (cutoff,),
        )
        await self._conn.execute(
            "UPDATE model_runs SET raw_response = NULL "
            "WHERE raw_response IS NOT NULL AND created_at < ?",
            (cutoff,),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
