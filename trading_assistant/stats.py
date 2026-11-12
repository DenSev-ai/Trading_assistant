"""Статистика точности, стоимости тестов и эффективности моделей (CONTRACTS §8)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .config import EvaluationSettings, SignalSettings
from .repository import Repository

CONSENSUS_MODEL = "consensus"
PROJECT_STATE_KEY = "project_state"
DEFAULT_PROJECT_STATE = "pair_test"
PAIR_ROLE = "pair"
PAIR_SUBJECT_PREFIX = "pair:"
REPORT_HEADER = (
    "модель | роль | n | сбылось % | стоимость выданных | весь расход | эффективность | вердикт"
)
NO_DATA_TEXT = "Нет проверенных прогнозов"
MONEY_DIGITS = 4
PERCENT_DIGITS = 1
EFFICIENCY_DIGITS = 4


def _num(value: float | None, digits: int) -> str:
    """Число с фиксированным числом знаков или прочерк, если значения нет."""
    return "—" if value is None else f"{value:.{digits}f}"


def pair_key_of(subject: str) -> str | None:
    """Ключ пары из subject вида 'pair:idA+idB'."""
    return subject.split(":", 1)[1] if ":" in subject else None


def efficiency(cost_issued: float, success_pct: float | None) -> float | None:
    """Стоимость выданных прогнозов на процент сбывшихся; None при нулевой точности."""
    if success_pct is None or success_pct <= 0:
        return None
    return cost_issued / success_pct


def verdict(percent: float | None, n: int, sample_n: int, min_success_pct: float) -> str:
    """Вердикт проекта по накопленной выборке (CONTRACTS §8)."""
    if n >= sample_n and percent is not None:
        if percent >= min_success_pct:
            return "ПРОЕКТ ПРИГОДЕН"
        return "ПРОЕКТ НЕ ПРИГОДЕН"
    return f"НАКОПЛЕНИЕ ДАННЫХ (n={n} из {sample_n})"


class Stats:
    """Точность, стоимость выданных прогнозов и текст отчёта --report."""

    def __init__(
        self,
        repo: Repository,
        signal: SignalSettings,
        evaluation: EvaluationSettings,
        now_fn: Callable[[], datetime],
    ) -> None:
        """Запомнить зависимости: репозиторий (весь SQL), пороги сигнала, тест, часы."""
        self._repo = repo
        self._signal = signal
        self._evaluation = evaluation
        self._now_fn = now_fn

    async def accuracy(
        self,
        model: str,
        ticker: str | None = None,
        pair_key: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[float | None, int]:
        """Доля сбывшихся выданных проверенных прогнозов: (процент | None, n)."""
        ok, n = await self._repo.success_counts(
            model, ticker=ticker, pair_key=pair_key, prompt_version=prompt_version
        )
        if n == 0:
            return None, 0
        return 100.0 * ok / n, n

    async def cost_of_issued(self, model: str, prediction_ids: list[int] | None = None) -> float:
        """Стоимость запросов, давших выданные прогнозы (каждый запрос — один раз)."""
        rows = await self._repo.issued_predictions(model)
        if prediction_ids is not None:
            wanted = set(prediction_ids)
            rows = [row for row in rows if int(row["id"]) in wanted]
        if not rows:
            return 0.0
        if model == CONSENSUS_MODEL:
            return await self._pair_cost(rows)
        run_ids = sorted({int(row["run_id"]) for row in rows if row["run_id"] is not None})
        return await self._repo.distinct_run_cost(run_ids)

    async def total_cost(self, model: str) -> float:
        """Весь расход модели: все запросы, включая повторы и вызовы без прогноза."""
        return await self._repo.runs_cost(model)

    async def report(self, now: datetime) -> str:
        """Текст отчёта --report: состояние проекта, таблица тестов, прогресс и вердикт."""
        state = await self._repo.kv_get(PROJECT_STATE_KEY) or DEFAULT_PROJECT_STATE
        lines = [f"Состояние проекта: {state}"]
        tests = await self._current_tests()
        if not tests:
            lines.append(NO_DATA_TEXT)
            return "\n".join(lines)
        lines.append(REPORT_HEADER)
        lines.extend(self._table_line(test) for test in tests)
        pair_test = next((test for test in tests if test["role"] == PAIR_ROLE), None)
        if pair_test is None:
            return "\n".join(lines)
        sample_n = self._evaluation.sample_n
        lines.append(await self._pair_line(pair_test))
        for test in tests:
            if test["status"] == "running":
                checked = await self._checked(test)
                progress = f"{test['subject']} ({test['role']}): проверено {checked} из {sample_n}"
                lines.append(progress)
        lines.append(await self._project_verdict(pair_test, sample_n))
        return "\n".join(lines)

    async def _pair_cost(self, rows: list[dict]) -> float:
        """Стоимость пары по выборке: успешные запросы обеих моделей на кластерах выборки."""
        cluster_ids = sorted({int(row["cluster_id"]) for row in rows})
        pair_ids: set[str] = set()
        for row in rows:
            pair_ids.update(part for part in (row["pair_key"] or "").split("+") if part)
        total = 0.0
        for model_id in sorted(pair_ids):
            total += await self._repo.runs_cost(model_id, cluster_ids=cluster_ids, only_ok=True)
        return total

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
        """Выданные прогнозы субъекта теста в порядке создания."""
        version = test["prompt_version"]
        if test["role"] == PAIR_ROLE:
            return await self._repo.issued_predictions(
                CONSENSUS_MODEL, pair_key=pair_key_of(test["subject"]), prompt_version=version
            )
        return await self._repo.issued_predictions(test["subject"], prompt_version=version)

    async def _checked(self, test: dict) -> int:
        """Сколько прогнозов из окна теста уже проверено (строка «проверено X из N»)."""
        rows = [
            row
            for row in await self._sample_rows(test)
            if not (row["evaluated_at"] is not None and row["is_successful"] is None)
        ]
        rows.sort(key=lambda row: (row["created_at"], row["id"]))
        window = rows[: self._evaluation.sample_n]
        return sum(1 for row in window if row["evaluated_at"] is not None)

    async def _pair_line(self, pair_test: dict) -> str:
        """Строка о паре consensus: её ключ и тикеры выданных прогнозов."""
        key = pair_key_of(pair_test["subject"]) or "—"
        rows = await self._repo.issued_predictions(
            CONSENSUS_MODEL, pair_key=key, prompt_version=pair_test["prompt_version"]
        )
        tickers = sorted({row["ticker"] for row in rows if row["ticker"]})
        listing = ", ".join(tickers) if tickers else "нет прогнозов"
        return f"Пара consensus {key}: {listing}"

    async def _project_verdict(self, pair_test: dict, sample_n: int) -> str:
        """Строка критерия проекта: по закрытому тесту пары или по проверенным прогнозам."""
        if pair_test["status"] == "done":
            percent = pair_test["success_pct"]
            n = int(pair_test["n"] or 0)
        else:
            percent, n = await self.accuracy(
                CONSENSUS_MODEL,
                pair_key=pair_key_of(pair_test["subject"]),
                prompt_version=pair_test["prompt_version"],
            )
        return verdict(percent, n, sample_n, self._evaluation.min_success_pct)

    def _table_line(self, test: dict) -> str:
        """Строка таблицы отчёта по одной записи model_tests."""
        cells = [
            str(test["subject"]),
            str(test["role"]),
            str(test["n"] if test["n"] is not None else 0),
            _num(test["success_pct"], PERCENT_DIGITS),
            _num(test["cost_issued_usd"], MONEY_DIGITS),
            _num(test["cost_total_usd"], MONEY_DIGITS),
            _num(test["efficiency"], EFFICIENCY_DIGITS),
            test["verdict"] or test["status"],
        ]
        return " | ".join(cells)
