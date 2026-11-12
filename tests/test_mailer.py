"""Тесты reports.mailer: файловый транспорт, SMTP и выбор транспорта (STEP 4D)."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import aiosmtplib
import pytest

from trading_assistant.config import AppSettings, EnvSettings, load_settings
from trading_assistant.reports.mailer import (
    SMTP_DISABLED_WARNING,
    FileMailer,
    SmtpMailer,
    file_mailer,
    make_mailer,
    slugify,
)

NOW = datetime(2026, 11, 9, 6, 46)  # 09:46 МСК; FileMailer печатает время как есть
FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_.+\.txt$")
PASSWORD = "smtp-secret-password"


def _settings(tmp_path: Path) -> AppSettings:
    """Настройки сервиса с папкой данных во временной директории."""
    data = load_settings(Path("config/settings.yaml")).model_dump()
    data["data_dir"] = str(tmp_path)
    return AppSettings.model_validate(data)


def _env(**overrides: str) -> EnvSettings:
    """Переменные .env без чтения файла .env: все значения задаются в тесте."""
    values = {
        "SMTP_HOST": "",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "REPORT_EMAIL_TO": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_IDS": "",
    }
    values.update(overrides)
    return EnvSettings(_env_file=None, **values)


def _smtp_env() -> EnvSettings:
    """Полностью настроенный SMTP."""
    return _env(
        SMTP_HOST="smtp.example.com",
        SMTP_PORT="465",
        SMTP_USER="user@example.com",
        SMTP_PASSWORD=PASSWORD,
        REPORT_EMAIL_TO="report@example.com",
    )


def test_slugify_keeps_letters_and_digits() -> None:
    assert slugify("Ежедневная сводка: 12 новостей!") == "ежедневная-сводка-12-новостей"
    assert slugify("   ...   ") == "report"
    assert len(slugify("а" * 200)) == 40


async def test_file_mailer_writes_report(tmp_path: Path) -> None:
    mailer = FileMailer(tmp_path / "reports", lambda: NOW)

    assert await mailer.send("Ежедневная сводка", "текст отчёта") is True

    files = list((tmp_path / "reports").glob("*.txt"))
    assert len(files) == 1
    assert FILE_NAME.match(files[0].name)
    assert files[0].name.startswith("2026-11-09_0646_")
    assert files[0].read_text(encoding="utf-8") == "текст отчёта"


async def test_file_mailer_uses_kind_fn_for_name(tmp_path: Path) -> None:
    mailer = FileMailer(tmp_path, lambda: NOW, kind_fn=lambda subject: "daily")

    await mailer.send("Ежедневная сводка", "текст")

    assert (tmp_path / "2026-11-09_0646_daily.txt").exists()


async def test_file_mailer_uses_service_timezone(tmp_path: Path) -> None:
    mailer = file_mailer(_settings(tmp_path), lambda: datetime(2026, 11, 9, 6, 46, tzinfo=UTC))

    await mailer.send("Сводка", "текст")

    assert (tmp_path / "reports" / "2026-11-09_0946_сводка.txt").exists()


async def test_smtp_mailer_sends_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_send(message, **kwargs):
        calls.append({"message": message, **kwargs})
        return {}, ""

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    mailer = SmtpMailer("smtp.example.com", 465, "u@example.com", PASSWORD, "to@example.com", 20.0)

    assert await mailer.send("Тема", "Текст") is True

    assert len(calls) == 1
    call = calls[0]
    assert call["hostname"] == "smtp.example.com"
    assert call["port"] == 465
    assert call["use_tls"] is True
    assert call["timeout"] == 20.0
    assert call["password"] == PASSWORD
    assert call["message"]["Subject"] == "Тема"
    assert call["message"].get_content().strip() == "Текст"


async def test_smtp_mailer_error_logs_without_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_send(message, **kwargs):
        raise OSError("соединение отклонено")

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    mailer = SmtpMailer("smtp.example.com", 465, "u@example.com", PASSWORD, "to@example.com", 20.0)

    with caplog.at_level(logging.ERROR):
        assert await mailer.send("Тема", "Текст") is False

    assert "не отправить письмо" in caplog.text
    assert PASSWORD not in caplog.text


def test_make_mailer_without_smtp_returns_file_mailer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        mailer = make_mailer(_env(), _settings(tmp_path), lambda: NOW)

    assert isinstance(mailer, FileMailer)
    assert SMTP_DISABLED_WARNING in caplog.text


def test_make_mailer_without_report_email_returns_file_mailer(tmp_path: Path) -> None:
    env = _smtp_env()
    env.REPORT_EMAIL_TO = None

    assert isinstance(make_mailer(env, _settings(tmp_path), lambda: NOW), FileMailer)


def test_make_mailer_with_smtp_returns_smtp_mailer(tmp_path: Path) -> None:
    mailer = make_mailer(_smtp_env(), _settings(tmp_path), lambda: NOW)

    assert isinstance(mailer, SmtpMailer)
