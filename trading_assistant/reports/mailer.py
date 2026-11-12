"""Транспорты отчётов: файл в data/reports и письмо по SMTP (CONTRACTS §10)."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import aiosmtplib

from ..config import AppSettings, EnvSettings

logger = logging.getLogger(__name__)

REPORTS_DIR_NAME = "reports"
TIME_FORMAT = "%Y-%m-%d_%H%M"
SLUG_MAX_CHARS = 40
SLUG_FALLBACK = "report"
SLUG_SEPARATOR = "-"
SMTP_DISABLED_WARNING = (
    "E-mail отчёты не настроены: SMTP_* и REPORT_EMAIL_TO пусты; "
    "отчёты сохраняются в data/reports/"
)


class Mailer(Protocol):
    """Транспорт отчёта: отправить тему и текст, вернуть True при успехе."""

    async def send(self, subject: str, body: str) -> bool:
        ...


def slugify(text: str, limit: int = SLUG_MAX_CHARS) -> str:
    """Слаг для имени файла: строчные буквы и цифры через дефис, не длиннее limit."""
    dashed = re.sub(r"[\W_]+", SLUG_SEPARATOR, text.lower()).strip(SLUG_SEPARATOR)
    if len(dashed) <= limit:
        return dashed or SLUG_FALLBACK
    parts: list[str] = []
    length = 0
    for part in dashed.split(SLUG_SEPARATOR):
        extra = len(part) + (1 if parts else 0)
        if length + extra > limit:
            break
        parts.append(part)
        length += extra
    return SLUG_SEPARATOR.join(parts) or dashed[:limit].strip(SLUG_SEPARATOR) or SLUG_FALLBACK


def reports_dir(cfg: AppSettings) -> Path:
    """Папка файловых отчётов: data/reports."""
    return Path(cfg.data_dir) / REPORTS_DIR_NAME


def _write_text(path: Path, body: str) -> None:
    """Создать папку отчётов и записать текст отчёта."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


class FileMailer:
    """Отчёт в файл data/reports/YYYY-MM-DD_HHMM_<тема-слаг>.txt."""

    def __init__(
        self,
        directory: Path,
        now_fn: Callable[[], datetime],
        kind_fn: Callable[[str], str] | None = None,
    ) -> None:
        """Запомнить папку отчётов, часы и функцию имени файла (по умолчанию слаг темы)."""
        self._directory = Path(directory)
        self._now_fn = now_fn
        self._name_fn = kind_fn or slugify

    async def send(self, subject: str, body: str) -> bool:
        """Записать отчёт в файл; True, если файл создан."""
        stamp = self._now_fn().strftime(TIME_FORMAT)
        path = self._directory / f"{stamp}_{self._name_fn(subject)}.txt"
        await asyncio.to_thread(_write_text, path, body)
        logger.info("отчёт сохранён в файл: %s", path)
        return True


class SmtpMailer:
    """Письмо по SMTP с SSL/TLS; ошибка отправки логируется и не роняет сервис."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        to: str,
        timeout: float,
    ) -> None:
        """Запомнить параметры SMTP-сервера, адресата и таймаут."""
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = to
        self._timeout = timeout

    async def send(self, subject: str, body: str) -> bool:
        """Отправить письмо; False при любой ошибке (пароль в лог не попадает)."""
        message = EmailMessage()
        message["From"] = self._user
        message["To"] = self._to
        message["Subject"] = subject
        message.set_content(body)
        try:
            await aiosmtplib.send(
                message,
                hostname=self._host,
                port=self._port,
                username=self._user,
                password=self._password,
                timeout=self._timeout,
                use_tls=True,
            )
        except Exception as exc:  # любая ошибка SMTP: отчёт уже сохранён в файл
            logger.error("не отправить письмо «%s»: %s", subject, exc)
            return False
        logger.info("письмо отправлено: %s", subject)
        return True


def file_mailer(cfg: AppSettings, now_fn: Callable[[], datetime]) -> FileMailer:
    """Файловый транспорт отчётов: папка data/reports, время в поясе cfg.timezone."""
    tz = ZoneInfo(cfg.timezone)
    return FileMailer(reports_dir(cfg), lambda: now_fn().astimezone(tz))


def smtp_configured(env: EnvSettings) -> bool:
    """True, если заданы SMTP_HOST, SMTP_USER, SMTP_PASSWORD и REPORT_EMAIL_TO."""
    return bool(env.SMTP_HOST and env.SMTP_USER and env.SMTP_PASSWORD and env.REPORT_EMAIL_TO)


def make_mailer(env: EnvSettings, cfg: AppSettings, now_fn: Callable[[], datetime]) -> Mailer:
    """SMTP, если настроен, иначе файл; отсутствие SMTP — предупреждение, не ошибка."""
    if not smtp_configured(env):
        logger.warning(SMTP_DISABLED_WARNING)
        return file_mailer(cfg, now_fn)
    return SmtpMailer(
        host=env.SMTP_HOST or "",
        port=env.SMTP_PORT,
        user=env.SMTP_USER or "",
        password=env.SMTP_PASSWORD or "",
        to=env.REPORT_EMAIL_TO or "",
        timeout=float(cfg.polling.http_timeout_sec),
    )
