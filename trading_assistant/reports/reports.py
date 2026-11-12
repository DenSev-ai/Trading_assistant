"""Тексты отчётов и сервис отчётов: ежедневный, еженедельный, события (CONTRACTS §10)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..clock import is_working_day, to_iso
from ..config import AppSettings
from ..evaluation import Evaluator, rank_models
from ..llm.budget import Budget
from ..repository import Repository
from ..stats import (
    CONSENSUS_MODEL,
    DEFAULT_PROJECT_STATE,
    PAIR_ROLE,
    PROJECT_STATE_KEY,
    Stats,
    pair_key_of,
)
from .mailer import SMTP_DISABLED_WARNING, FileMailer, Mailer

logger = logging.getLogger(__name__)

DAILY = "daily"
WEEKLY = "weekly"
DAILY_KEY = "last_daily_report"
WEEKLY_KEY = "last_weekly_report"
PROPOSALS_KEY = "last_proposals"
EVENT_KEY_PREFIX = "event"
ERRORS_LOG_NAME = "errors.log"
ERRORS_TAIL_LINES = 50
WEEK_DAYS = 7
MONEY_DIGITS = 4
PERCENT_DIGITS = 1
EFFICIENCY_DIGITS = 4

EVENT_TITLES = {
    "test_done": "Тест завершён",
    "project_suitable": "Проект пригоден",
    "project_unsuitable": "Проект не пригоден",
    "replace_proposal": "Предложение замены модели",
    "best_pair_proposal": "Предложение лучшей пары",
    "budget_exhausted": "Суточный бюджет исчерпан",
    "llm_fatal": "Сбой LLM",
}

PAYLOAD_LABELS = {
    "date": "Дата",
    "model": "Модель",
    "cluster_id": "Кластер",
    "role": "Роль",
    "status": "Статус",
    "n": "Прогнозов",
    "success_pct": "Сбылось, %",
    "verdict": "Вердикт",
    "efficiency": "Эффективность",
    "cost_issued_usd": "Стоимость выданных, $",
    "cost_total_usd": "Весь расход, $",
    "spent_usd": "Расход за сутки, $",
    "limit_usd": "Суточный лимит, $",
}


def _money(value: float) -> str:
    """Сумма в долларах с фиксированным числом знаков."""
    return f"${value:.{MONEY_DIGITS}f}"


def _percent(value: float | None) -> str:
    """Процент с одним знаком после запятой или прочерк, если значения нет."""
    return "—" if value is None else f"{value:.{PERCENT_DIGITS}f}%"


def _num(value: float | None, digits: int) -> str:
    """Число с фиксированным числом знаков или прочерк, если значения нет."""
    return "—" if value is None else f"{value:.{digits}f}"


def tail_lines(path: Path, limit: int) -> list[str]:
    """Последние limit строк текстового файла; файла нет — пустой список."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-limit:]


def _msk_midnight(day: date, tz: ZoneInfo) -> datetime:
    """Полночь суток day по поясу tz, переведённая в UTC."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def _iso_week(moment: datetime) -> str:
    """Метка ISO-недели вида '2026-W45' для момента времени."""
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def build_daily(data: dict) -> tuple[str, str]:
    """Тема и текст ежедневной сводки за прошлые сутки (данные — dict от сервиса)."""
    state = data["state"]
    spent = float(data["spent_day"])
    subject = f"Ежедневная сводка {data['date']}: {state}, расход {_money(spent)}"
    lines = [
        f"Ежедневная сводка за {data['date']} (МСК)",
        f"Состояние проекта: {state}",
        "Воронка: "
        f"собрано {data['collected']}, отфильтровано {data['filtered']}, "
        f"кластеров {data['clusters']}, проанализировано {data['analyzed']}, "
        f"выдано моделями {data['issued']}, согласовано парой {data['consensus']}",
        f"Отправлено: алертов {data['alerts']}, в дайджесте {data['digest']}",
        f"Расход за сутки: {_money(spent)} из {_money(float(data['budget']))}, "
        f"остаток на сегодня {_money(float(data['budget_left']))}",
        f"Ждут проверки: {data['pending_checks']} прогнозов",
    ]
    lines.extend(f"Предупреждение: {warning}" for warning in data["warnings"])
    errors = list(data["errors"])
    if errors:
        lines.append(f"Сбои (последние {len(errors)} строк errors.log):")
        lines.extend(errors)
    else:
        lines.append("Сбоев в errors.log нет")
    return subject, "\n".join(lines)


def build_weekly(data: dict) -> tuple[str, str]:
    """Тема и текст еженедельного отчёта (данные — dict от сервиса)."""
    subject = f"Еженедельный отчёт {data['date']} ({data['week']})"
    lines = [
        f"Еженедельный отчёт за неделю {data['week']} (на {data['date']})",
        "",
        str(data["report"]),
        "",
        f"Проверено за 7 дней: {data['checked']}, сбылось {data['successful']}, "
        f"target_hit у {data['target_hit']}",
        f"Расход за неделю: {_money(float(data['spent_week']))}, "
        f"всего: {_money(float(data['spent_total']))}",
        f"Невыданных прогнозов (охват): {data['not_issued']} из {data['predictions']}",
    ]
    progress = list(data["progress"])
    if progress:
        lines.append("")
        lines.append("Прогресс тестов:")
        lines.extend(
            f"{item['subject']} ({item['role']}): проверено {item['checked']} из "
            f"{data['sample_n']} — {item['verdict'] or item['status']}"
            for item in progress
        )
    ranking = list(data["ranking"])
    lines.append("")
    if ranking:
        lines.append("Рейтинг эффективности (suitable):")
        lines.extend(ranking)
    else:
        lines.append("Рейтинг эффективности: подходящих моделей пока нет")
    proposals = list(data["proposals"])
    lines.append("")
    if proposals:
        lines.append("Рекомендации сервиса:")
        lines.extend(f"- {proposal}" for proposal in proposals)
    else:
        lines.append("Рекомендаций сервиса нет")
    return subject, "\n".join(lines)


def build_event(kind: str, payload: dict) -> tuple[str, str]:
    """Тема и текст событийного письма по виду события и его данным."""
    title = EVENT_TITLES.get(kind, f"Событие {kind}")
    subject = f"{title}: {payload['subject']}" if payload.get("subject") else title
    lines = [title]
    text = payload.get("text")
    if text:
        lines.append(str(text))
    lines.extend(
        f"{PAYLOAD_LABELS.get(key, key)}: {value}"
        for key, value in payload.items()
        if key not in ("subject", "text")
    )
    return subject, "\n".join(lines)


def checked_in_window(rows: list[dict], sample_n: int) -> int:
    """Сколько прогнозов окна теста (первые sample_n) уже проверено."""
    known = [
        row
        for row in rows
        if not (row["evaluated_at"] is not None and row["is_successful"] is None)
    ]
    known.sort(key=lambda row: (row["created_at"], row["id"]))
    return sum(1 for row in known[:sample_n] if row["evaluated_at"] is not None)


def _ranking_line(test: dict) -> str:
    """Строка рейтинга по завершённому тесту: точность, эффективность, размер выборки."""
    return (
        f"{test['subject']} ({test['role']}): сбылось {_percent(test['success_pct'])}, "
        f"эффективность {_num(test['efficiency'], EFFICIENCY_DIGITS)}, n={test['n']}"
    )


class ReportService:
    """Ежедневный, еженедельный и событийные отчёты; ошибки отправки не роняют сервис."""

    def __init__(
        self,
        repo: Repository,
        stats: Stats,
        evaluator: Evaluator,
        cfg: AppSettings,
        mailer: Mailer,
        file_mailer: FileMailer,
        now_fn: Callable[[], datetime],
    ) -> None:
        """Запомнить зависимости: БД, статистику, оценки тестов, настройки, транспорты и часы."""
        self._repo = repo
        self._stats = stats
        self._evaluator = evaluator
        self._cfg = cfg
        self._mailer = mailer
        self._file_mailer = file_mailer
        self._now_fn = now_fn
        self._tz = ZoneInfo(cfg.timezone)

    async def run(self, now: datetime, force: str | None = None) -> list[str]:
        """Отправить отчёты, время которых наступило; force='daily'|'weekly' — принудительно."""
        if not self._cfg.reports.enabled:
            return []
        sent: list[str] = []
        daily_forced = force == DAILY
        weekly_forced = force == WEEKLY
        if (force is None or daily_forced) and await self._due_daily(now, daily_forced):
            if await self._send_daily(now, mark=force is None):
                sent.append(DAILY)
        if (force is None or weekly_forced) and await self._due_weekly(now, weekly_forced):
            if await self._send_weekly(now, mark=force is None):
                sent.append(WEEKLY)
        return sent

    async def event(self, kind: str, key: str, payload: dict) -> bool:
        """Отправить событийное письмо один раз на key; True, если письмо отправлено впервые."""
        if not self._cfg.reports.enabled or not self._cfg.reports.event_reports:
            return False
        kv_key = f"{EVENT_KEY_PREFIX}:{kind}:{key}"
        if await self._repo.kv_get(kv_key) is not None:
            return False
        subject, body = build_event(kind, payload)
        try:
            await self._deliver(subject, body)
        except Exception as exc:  # отчёт не должен останавливать конвейер
            logger.error("не отправить событийное письмо «%s»: %s", subject, exc)
            return False
        await self._repo.kv_set(kv_key, to_iso(self._now_fn()))
        return True

    async def _due_daily(self, now: datetime, force: bool) -> bool:
        """Пора ли отправить ежедневную сводку: рабочий день, время, один раз за дату МСК."""
        local = self._local_moment(now)
        if not force:
            if not is_working_day(now, self._cfg.schedule, self._tz):
                return False
            if local.time() < time.fromisoformat(self._cfg.reports.daily_time):
                return False
        return await self._repo.kv_get(DAILY_KEY) != local.date().isoformat()

    async def _due_weekly(self, now: datetime, force: bool) -> bool:
        """Пора ли отправить еженедельный отчёт: день недели, время, один раз за ISO-неделю."""
        local = self._local_moment(now)
        if not force:
            if local.weekday() != self._cfg.reports.weekly_weekday:
                return False
            if local.time() < time.fromisoformat(self._cfg.reports.weekly_time):
                return False
        return await self._repo.kv_get(WEEKLY_KEY) != _iso_week(local)

    async def _send_daily(self, now: datetime, mark: bool = True) -> bool:
        """Сохранить ежедневную сводку в файл, отправить письмом и запомнить дату."""
        try:
            subject, body = build_daily(await self._daily_data(now))
            await self._deliver(subject, body)
        except Exception as exc:
            logger.error("не подготовить ежедневную сводку: %s", exc)
            return False
        if mark:
            await self._repo.kv_set(DAILY_KEY, self._local_date(now).isoformat())
        return True

    async def _send_weekly(self, now: datetime, mark: bool = True) -> bool:
        """Сохранить еженедельный отчёт в файл, отправить письмом и запомнить ISO-неделю."""
        try:
            subject, body = build_weekly(await self._weekly_data(now))
            await self._deliver(subject, body)
        except Exception as exc:
            logger.error("не подготовить еженедельный отчёт: %s", exc)
            return False
        if mark:
            await self._repo.kv_set(WEEKLY_KEY, _iso_week(self._local_moment(now)))
        return True

    async def _deliver(self, subject: str, body: str) -> bool:
        """Отчёт всегда сохраняется в data/reports, затем уходит письмом, если SMTP настроен."""
        await self._file_mailer.send(subject, body)
        if isinstance(self._mailer, FileMailer):
            return True  # SMTP не настроен: отчёт остался файлом
        try:
            return await self._mailer.send(subject, body)
        except Exception as exc:
            logger.error("не отправить письмо «%s»: %s", subject, exc)
            return False

    def _local_moment(self, now: datetime) -> datetime:
        """Момент времени в поясе сервиса (МСК)."""
        return now.astimezone(self._tz)

    def _local_date(self, now: datetime) -> date:
        """Дата по поясу сервиса (МСК)."""
        return self._local_moment(now).date()

    async def _daily_data(self, now: datetime) -> dict:
        """Данные ежедневной сводки: воронка за прошлые сутки МСК, расход, ожидание, сбои."""
        today = self._local_date(now)
        start = _msk_midnight(today - timedelta(days=1), self._tz)
        end = _msk_midnight(today, self._tz)
        counts = await self._repo.activity_counts(start, end)
        totals = await self._repo.prediction_totals()
        budget = Budget(self._repo, self._cfg.llm.daily_budget_usd, self._tz)
        spent_today = await budget.spent_today(now)
        return {
            "date": (today - timedelta(days=1)).isoformat(),
            "state": await self._project_state(),
            "collected": int(counts.get("collected", 0)),
            "filtered": int(counts.get("filtered", 0)),
            "clusters": int(counts.get("clusters", 0)),
            "analyzed": int(counts.get("analyzed", 0)),
            "issued": int(counts.get("issued", 0)),
            "consensus": int(counts.get("consensus", 0)),
            "alerts": int(counts.get("alerts", 0)),
            "digest": int(counts.get("digest", 0)),
            "spent_day": await self._repo.spent_between(start, end),
            "budget": self._cfg.llm.daily_budget_usd,
            "budget_left": max(0.0, self._cfg.llm.daily_budget_usd - spent_today),
            "pending_checks": int(totals["pending"]),
            "errors": await self._errors_tail(),
            "warnings": self._warnings(),
        }

    async def _weekly_data(self, now: datetime) -> dict:
        """Данные еженедельного отчёта: таблица --report, проверки, расход, тесты, рекомендации."""
        since = now - timedelta(days=WEEK_DAYS)
        checked = await self._repo.evaluated_since(since)
        totals = await self._repo.prediction_totals()
        tests = await self._current_tests()
        done = [test for test in tests if test["status"] == "done"]
        return {
            "date": self._local_date(now).isoformat(),
            "week": _iso_week(self._local_moment(now)),
            "report": await self._stats.report(now),
            "checked": len(checked),
            "successful": sum(1 for row in checked if row["is_successful"]),
            "target_hit": sum(1 for row in checked if row["target_hit"]),
            "spent_week": await self._repo.spent_since(since),
            "spent_total": await self._repo.total_spent(),
            "issued": int(totals["issued"]),
            "predictions": int(totals["total"]),
            "not_issued": int(totals["total"]) - int(totals["issued"]),
            "sample_n": self._cfg.evaluation.sample_n,
            "progress": [await self._progress(test) for test in tests],
            "ranking": self._ranking(done),
            "proposals": await self._proposals(),
        }

    async def _project_state(self) -> str:
        """Состояние проекта из kv (по умолчанию боевая пара набирает выборку)."""
        return await self._repo.kv_get(PROJECT_STATE_KEY) or DEFAULT_PROJECT_STATE

    async def _errors_tail(self) -> list[str]:
        """Последние строки data/errors.log; файла нет — пустой список."""
        path = Path(self._cfg.data_dir) / ERRORS_LOG_NAME
        return await asyncio.to_thread(tail_lines, path, ERRORS_TAIL_LINES)

    def _warnings(self) -> list[str]:
        """Предупреждения сводки: без SMTP отчёты только сохраняются в файл."""
        if isinstance(self._mailer, FileMailer):
            return [SMTP_DISABLED_WARNING]
        return []

    async def _current_tests(self) -> list[dict]:
        """Тесты последней версии промта в порядке старта (старые версии не смешиваются)."""
        tests = await self._repo.done_tests()
        tests += await self._repo.running_tests()
        if not tests:
            return []
        tests.sort(key=lambda test: (test["started_at"], test["id"]))
        version = tests[-1]["prompt_version"]
        return [test for test in tests if test["prompt_version"] == version]

    async def _sample_rows(self, test: dict) -> list[dict]:
        """Выданные прогнозы субъекта теста при версии промта этого теста."""
        version = test["prompt_version"]
        if test["role"] == PAIR_ROLE:
            return await self._repo.issued_predictions(
                CONSENSUS_MODEL, pair_key=pair_key_of(test["subject"]), prompt_version=version
            )
        return await self._repo.issued_predictions(test["subject"], prompt_version=version)

    async def _progress(self, test: dict) -> dict:
        """Прогресс теста: сколько прогнозов его окна уже проверено."""
        rows = await self._sample_rows(test)
        return {
            "subject": test["subject"],
            "role": test["role"],
            "checked": checked_in_window(rows, self._cfg.evaluation.sample_n),
            "status": test["status"],
            "verdict": test["verdict"],
        }

    def _ranking(self, tests: list[dict]) -> list[str]:
        """Строки рейтинга эффективности suitable-тестов (меньше — лучше)."""
        return [_ranking_line(test) for test in rank_models(tests)]

    async def _proposals(self) -> list[str]:
        """Последние предложения сервиса (тексты Proposal, записанные конвейером)."""
        stored = await self._repo.kv_get(PROPOSALS_KEY)
        return stored.splitlines() if stored else []
