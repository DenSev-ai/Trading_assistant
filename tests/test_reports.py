"""Тесты reports.reports: ежедневная сводка, еженедельный отчёт и события (STEP 4D)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosmtplib
import pytest

from trading_assistant.clock import to_iso
from trading_assistant.config import AppSettings, load_settings
from trading_assistant.db import Database
from trading_assistant.evaluation import Evaluator
from trading_assistant.reports.mailer import (
    SMTP_DISABLED_WARNING,
    FileMailer,
    Mailer,
    SmtpMailer,
)
from trading_assistant.reports.reports import ReportService, build_event
from trading_assistant.repository import Repository
from trading_assistant.stats import Stats

WEDNESDAY = datetime(2026, 11, 11, 6, 46, tzinfo=UTC)  # 09:46 МСК, рабочий день
EARLY = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)  # 09:00 МСК, до daily_time
MONDAY = datetime(2026, 11, 9, 6, 46, tzinfo=UTC)  # 09:46 МСК, weekly_weekday
TUESDAY = datetime(2026, 11, 10, 6, 46, tzinfo=UTC)  # 09:46 МСК, вторник
SATURDAY = datetime(2026, 11, 14, 6, 46, tzinfo=UTC)  # 09:46 МСК, выходной
PREV_DAY = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)  # 12:00 МСК прошлых суток
PAIR_KEY = "anthropic/claude-sonnet-5.5+x-ai/grok-4.7"
PASSWORD = "smtp-secret-password"


class FakeMailer:
    """Поддельный SMTP: записывает письма и умеет падать."""

    def __init__(self, ok: bool = True) -> None:
        self.letters: list[tuple[str, str]] = []
        self._ok = ok

    async def send(self, subject: str, body: str) -> bool:
        self.letters.append((subject, body))
        if not self._ok:
            raise OSError("SMTP недоступен")
        return True


def _settings(tmp_path: Path, **reports) -> AppSettings:
    """Настройки из config/settings.yaml: папка данных во временной, правки секции reports."""
    data = load_settings(Path("config/settings.yaml")).model_dump()
    data["data_dir"] = str(tmp_path)
    data["reports"].update(reports)
    return AppSettings.model_validate(data)


def _service(
    db: Database, cfg: AppSettings, now: datetime, transport: Mailer | None = None
) -> ReportService:
    """Сервис отчётов: файлы во временной папке, письма — поддельный транспорт."""
    repo = Repository(db)
    stats = Stats(repo, cfg.signal, cfg.evaluation, lambda: now)
    evaluator = Evaluator(repo, stats, cfg, lambda: now)
    file_mailer = FileMailer(Path(cfg.data_dir) / "reports", lambda: now)
    return ReportService(
        repo, stats, evaluator, cfg, transport or FakeMailer(), file_mailer, lambda: now
    )


def _file_only_service(db: Database, cfg: AppSettings, now: datetime) -> ReportService:
    """Сервис без SMTP: единственный транспорт — файл в data/reports."""
    file_mailer = FileMailer(Path(cfg.data_dir) / "reports", lambda: now)
    return _service(db, cfg, now, file_mailer)


async def _funnel(db: Database) -> None:
    """Одна статья, один кластер и один consensus-прогноз за прошлые сутки МСК."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, status) "
        "VALUES ('Новость', 'новость', ?, ?, 'analyzed')",
        (to_iso(PREV_DAY), to_iso(PREV_DAY)),
    )
    await db.execute(
        "INSERT INTO articles (url, source, title, title_norm, published_at, fetched_at, status, "
        "cluster_id) VALUES ('https://example.com/1', 'interfax', 'Новость', 'новость', ?, ?, "
        "'filtered', ?)",
        (to_iso(PREV_DAY), to_iso(PREV_DAY), cluster_id),
    )
    await db.execute(
        "INSERT INTO predictions (cluster_id, model, ticker, direction, confidence, severity, "
        "horizon_days, expected_change_pct, reason, created_at, prompt_version, pair_key, "
        "is_issued) VALUES (?, 'consensus', 'SBER', 'up', 90, 8, 30, 20.0, 'причина', ?, 'v2', "
        "?, 1)",
        (cluster_id, to_iso(PREV_DAY), PAIR_KEY),
    )


async def _spent(db: Database, cost: float) -> None:
    """Расход прошлых суток: один успешный запрос модели по кластеру."""
    cluster_id = await db.execute(
        "INSERT INTO clusters (title, title_norm, created_at, updated_at, status) "
        "VALUES ('Старая новость', 'старая новость', ?, ?, 'analyzed')",
        (to_iso(PREV_DAY), to_iso(PREV_DAY)),
    )
    await db.execute(
        "INSERT INTO model_runs (cluster_id, model, prompt_version, status, cost_usd, created_at) "
        "VALUES (?, 'test/model', 'v2', 'ok', ?, ?)",
        (cluster_id, cost, to_iso(PREV_DAY)),
    )


async def test_daily_report_sent_once_per_day(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    assert await service.run(WEDNESDAY) == ["daily"]
    assert len(mailer.letters) == 1
    assert await service.run(WEDNESDAY) == []
    assert len(mailer.letters) == 1
    assert len(list((tmp_path / "reports").glob("*.txt"))) == 1


async def test_daily_report_not_sent_on_weekend(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), SATURDAY, mailer)

    assert await service.run(SATURDAY) == []
    assert mailer.letters == []


async def test_daily_report_not_sent_before_daily_time(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), EARLY, mailer)

    assert await service.run(EARLY) == []
    assert mailer.letters == []


async def test_weekly_report_once_per_iso_week(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), MONDAY, mailer)

    assert "weekly" in await service.run(MONDAY)
    assert sum(1 for subject, _ in mailer.letters if "Еженедельный" in subject) == 1

    assert "weekly" not in await service.run(TUESDAY)
    assert sum(1 for subject, _ in mailer.letters if "Еженедельный" in subject) == 1


async def test_smtp_failure_keeps_file_and_does_not_raise(db, tmp_path: Path) -> None:
    mailer = FakeMailer(ok=False)
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    assert await service.run(WEDNESDAY) == ["daily"]
    assert len(list((tmp_path / "reports").glob("*.txt"))) == 1


async def test_daily_report_warns_without_smtp(db, tmp_path: Path) -> None:
    service = _file_only_service(db, _settings(tmp_path), WEDNESDAY)

    await service.run(WEDNESDAY)

    written = next(iter((tmp_path / "reports").glob("*.txt"))).read_text(encoding="utf-8")
    assert SMTP_DISABLED_WARNING in written


async def test_smtp_password_not_in_report_or_log(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_send(message, **kwargs):
        raise OSError("аутентификация отклонена")

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    smtp = SmtpMailer("smtp.example.com", 465, "u@example.com", PASSWORD, "to@example.com", 20.0)
    service = _service(db, _settings(tmp_path), WEDNESDAY, smtp)

    with caplog.at_level(logging.DEBUG):
        assert await service.run(WEDNESDAY) == ["daily"]

    written = next(iter((tmp_path / "reports").glob("*.txt"))).read_text(encoding="utf-8")
    assert PASSWORD not in written
    assert PASSWORD not in caplog.text


async def test_event_sent_once_per_key(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    assert await service.event("test_done", "7", {"text": "тест завершён"}) is True
    assert await service.event("test_done", "7", {"text": "тест завершён"}) is False
    assert len(mailer.letters) == 1
    assert "Тест завершён" in mailer.letters[0][0]
    assert len(list((tmp_path / "reports").glob("*.txt"))) == 1


async def test_reports_disabled_sends_nothing(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path, enabled=False), WEDNESDAY, mailer)

    assert await service.run(WEDNESDAY) == []
    assert await service.event("test_done", "1", {"text": "тест"}) is False
    assert mailer.letters == []
    assert list((tmp_path / "reports").glob("*.txt")) == []


async def test_daily_report_contains_state_and_spend(db, tmp_path: Path) -> None:
    await Repository(db).kv_set("project_state", "candidates_test")
    await _spent(db, cost=0.05)
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    await service.run(WEDNESDAY)

    subject, body = mailer.letters[0]
    assert "candidates_test" in subject
    assert "candidates_test" in body
    assert "$0.0500" in subject
    assert "$0.0500" in body


async def test_daily_report_counts_previous_day_funnel(db, tmp_path: Path) -> None:
    await _funnel(db)
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    await service.run(WEDNESDAY)

    body = mailer.letters[0][1]
    assert "собрано 1, отфильтровано 1, кластеров 1, проанализировано 1" in body
    assert "согласовано парой 1" in body
    assert "Ждут проверки: 1 прогнозов" in body


async def test_daily_report_includes_errors_log_tail(db, tmp_path: Path) -> None:
    (tmp_path / "errors.log").write_text("ERROR первая\nERROR вторая\n", encoding="utf-8")
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), WEDNESDAY, mailer)

    await service.run(WEDNESDAY)

    body = mailer.letters[0][1]
    assert "ERROR вторая" in body
    assert "Сбои (последние 2 строк errors.log)" in body


async def test_weekly_report_contains_progress_and_spend(db, tmp_path: Path) -> None:
    repo = Repository(db)
    await repo.get_or_create_test(f"pair:{PAIR_KEY}", "pair", "v2", MONDAY)
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), MONDAY, mailer)

    sent = await service.run(MONDAY)

    assert "weekly" in sent
    subject, body = next(letter for letter in mailer.letters if "Еженедельный" in letter[0])
    assert "2026-W46" in subject
    assert "Состояние проекта: pair_test" in body
    assert "проверено 0 из 100" in body
    assert "Расход за неделю" in body
    assert "Невыданных прогнозов (охват)" in body


async def test_forced_report_ignores_schedule(db, tmp_path: Path) -> None:
    mailer = FakeMailer()
    service = _service(db, _settings(tmp_path), SATURDAY, mailer)

    assert await service.run(SATURDAY, force="daily") == ["daily"]
    assert await service.run(SATURDAY, force="weekly") == ["weekly"]
    assert len({subject for subject, _ in mailer.letters}) == 2


def test_build_event_subject_and_body() -> None:
    subject, body = build_event(
        "budget_exhausted", {"text": "Анализ остановлен", "date": "2026-11-11"}
    )

    assert subject == "Суточный бюджет исчерпан"
    assert body.splitlines()[0] == "Суточный бюджет исчерпан"
    assert "Анализ остановлен" in body
    assert "Дата: 2026-11-11" in body
