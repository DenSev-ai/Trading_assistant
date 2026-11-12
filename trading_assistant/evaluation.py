"""Выборка теста, вердикт, рейтинг моделей и предложения сервиса (CONTRACTS §9)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .config import AppSettings
from .llm.consensus import make_pair_key
from .repository import Repository
from .stats import CONSENSUS_MODEL, PAIR_SUBJECT_PREFIX, Stats, efficiency

PROJECT_STATE_KEY = "project_state"
PROJECT_STATE_PAIR_TEST = "pair_test"
PROJECT_STATE_CANDIDATES_TEST = "candidates_test"
PROJECT_STATE_PROJECT_FAILED = "project_failed"
PAIR_ROLE = "pair"
PAIR_MODEL_ROLE = "pair_model"
CANDIDATE_ROLE = "candidate"
SUITABLE = "suitable"
UNSUITABLE = "unsuitable"


@dataclass
class Proposal:
    """Предложение пользователю: вид (replace_model|best_pair|project_result) и текст."""

    kind: str
    text: str


def select_sample(rows: list[dict], sample_n: int) -> list[dict] | None:
    """Первые sample_n выданных прогнозов субъекта; None, если выборка ещё не готова."""
    known = [
        row
        for row in rows
        if not (row["evaluated_at"] is not None and row["is_successful"] is None)
    ]
    known.sort(key=lambda row: (row["created_at"], row["id"]))
    if len(known) < sample_n:
        return None
    sample = known[:sample_n]
    if any(row["evaluated_at"] is None for row in sample):
        return None
    return sample


def success_pct(sample: list[dict]) -> float:
    """Процент сбывшихся прогнозов в выборке."""
    return 100.0 * sum(int(row["is_successful"]) for row in sample) / len(sample)


def classify(pct: float, min_success_pct: float) -> str:
    """Вердикт субъекта: 'suitable' при доле не ниже порога, иначе 'unsuitable'."""
    return SUITABLE if pct >= min_success_pct else UNSUITABLE


def rank_models(tests: list[dict]) -> list[dict]:
    """Только suitable-тесты, по эффективности возрастанию; без эффективности — в конец."""
    suitable = [test for test in tests if test["verdict"] == SUITABLE]
    return sorted(
        suitable, key=lambda test: (test["efficiency"] is None, test["efficiency"] or 0.0)
    )


def best_pair(ranked: list[dict]) -> tuple[dict, dict] | None:
    """Две suitable-модели разных разработчиков с минимальной суммой эффективности."""
    best: tuple[dict, dict] | None = None
    best_sum = 0.0
    for position, first in enumerate(ranked):
        for second in ranked[position + 1 :]:
            if _vendor(first["subject"]) == _vendor(second["subject"]):
                continue
            if first["efficiency"] is None or second["efficiency"] is None:
                continue
            total = first["efficiency"] + second["efficiency"]
            if best is None or total < best_sum:
                best = (first, second)
                best_sum = total
    return best


def _vendor(model_id: str) -> str:
    """Разработчик модели: префикс id до символа '/'."""
    return model_id.split("/", 1)[0]


def _num(value: float | None, digits: int = 1) -> str:
    """Число с фиксированным числом знаков или прочерк, если значения нет."""
    return "—" if value is None else f"{value:.{digits}f}"


def _summary(test: dict) -> str:
    """Краткая сводка по тесту для текста предложения."""
    return (
        f"{test['subject']}: сбылось {_num(test['success_pct'])}% (n={test['n']}), "
        f"эффективность {_num(test['efficiency'], 4)}"
    )


class Evaluator:
    """Тесты пары, моделей пары и кандидатов: обновление model_tests и предложения."""

    def __init__(
        self,
        repo: Repository,
        stats: Stats,
        cfg: AppSettings,
        now_fn: Callable[[], datetime],
    ) -> None:
        """Запомнить зависимости: репозиторий, статистику, настройки и часы."""
        self._repo = repo
        self._stats = stats
        self._cfg = cfg
        self._now_fn = now_fn

    async def update(self, now: datetime) -> list[Proposal]:
        """Досоздать записи тестов, закрыть готовые и вернуть новые предложения."""
        version = self._cfg.llm.prompt_version
        pair_key = make_pair_key([model.id for model in self._cfg.llm.pair])
        await self._ensure_tests(version, pair_key, now)
        closed: list[dict] = []
        for test in await self._repo.running_tests():
            if test["prompt_version"] != version:
                continue
            result = await self._close_if_ready(test, now)
            if result is not None:
                closed.append(result)
        proposals = await self._state_proposals(closed, pair_key)
        ranked = rank_models(await self._candidate_tests(version))
        proposals += self._replacement_proposals(closed, ranked)
        proposals += await self._best_pair_proposals(closed, version, ranked)
        return proposals

    async def _ensure_tests(self, version: str, pair_key: str, now: datetime) -> None:
        """Создать записи тестов пары, моделей пары и (при candidates_test) кандидатов."""
        await self._repo.get_or_create_test(
            f"{PAIR_SUBJECT_PREFIX}{pair_key}", PAIR_ROLE, version, now
        )
        for model in self._cfg.llm.pair:
            await self._repo.get_or_create_test(model.id, PAIR_MODEL_ROLE, version, now)
        state = await self._repo.kv_get(PROJECT_STATE_KEY)
        if state == PROJECT_STATE_CANDIDATES_TEST:
            for model in self._cfg.llm.candidates:
                await self._repo.get_or_create_test(model.id, CANDIDATE_ROLE, version, now)

    async def _close_if_ready(self, test: dict, now: datetime) -> dict | None:
        """Закрыть тест, если его выборка полностью проверена; вернуть сводку или None."""
        sample = select_sample(await self._sample_rows(test), self._cfg.evaluation.sample_n)
        if sample is None:
            return None
        percent = success_pct(sample)
        test_verdict = classify(percent, self._cfg.evaluation.min_success_pct)
        ids = [int(row["id"]) for row in sample]
        cost_issued = await self._cost_issued(test, ids)
        cost_total = await self._cost_total(test)
        eff = efficiency(cost_issued, percent) if test_verdict == SUITABLE else None
        await self._repo.close_test(
            int(test["id"]), len(ids), percent, cost_issued, cost_total, eff, test_verdict, now
        )
        return {
            "subject": test["subject"],
            "role": test["role"],
            "n": len(ids),
            "success_pct": percent,
            "verdict": test_verdict,
            "efficiency": eff,
            "cost_issued_usd": cost_issued,
            "cost_total_usd": cost_total,
        }

    async def _sample_rows(self, test: dict) -> list[dict]:
        """Выданные прогнозы субъекта теста при версии промта этого теста."""
        version = test["prompt_version"]
        if test["role"] == PAIR_ROLE:
            key = test["subject"].split(":", 1)[-1]
            return await self._repo.issued_predictions(
                CONSENSUS_MODEL, pair_key=key, prompt_version=version
            )
        return await self._repo.issued_predictions(test["subject"], prompt_version=version)

    async def _cost_issued(self, test: dict, prediction_ids: list[int]) -> float:
        """Стоимость запросов, давших прогнозы выборки: для пары — обеих моделей."""
        model = CONSENSUS_MODEL if test["role"] == PAIR_ROLE else test["subject"]
        return await self._stats.cost_of_issued(model, prediction_ids)

    async def _cost_total(self, test: dict) -> float:
        """Весь расход теста: для пары — расход обеих моделей пары."""
        if test["role"] != PAIR_ROLE:
            return await self._stats.total_cost(test["subject"])
        total = 0.0
        for model in self._cfg.llm.pair:
            total += await self._stats.total_cost(model.id)
        return total

    async def _candidate_tests(self, version: str) -> list[dict]:
        """Закрытые тесты кандидатов для этой версии промта."""
        done = await self._repo.done_tests()
        return [
            test
            for test in done
            if test["role"] == CANDIDATE_ROLE and test["prompt_version"] == version
        ]

    async def _state_proposals(self, closed: list[dict], pair_key: str) -> list[Proposal]:
        """Сменить состояние проекта по закрытому тесту пары; proposal выдаётся один раз."""
        pair_test = next((test for test in closed if test["role"] == PAIR_ROLE), None)
        if pair_test is None:
            return []
        state = await self._repo.kv_get(PROJECT_STATE_KEY)
        if state not in (None, PROJECT_STATE_PAIR_TEST):
            return []
        percent = _num(pair_test["success_pct"])
        min_pct = _num(self._cfg.evaluation.min_success_pct, 0)
        if pair_test["verdict"] == SUITABLE:
            await self._repo.kv_set(PROJECT_STATE_KEY, PROJECT_STATE_CANDIDATES_TEST)
            text = (
                f"ПРОЕКТ ПРИГОДЕН: пара {pair_key} сбылась в {percent}% "
                f"(n={pair_test['n']}, порог {min_pct}%). Начинаем тесты кандидатов."
            )
            return [Proposal("project_result", text)]
        await self._repo.kv_set(PROJECT_STATE_KEY, PROJECT_STATE_PROJECT_FAILED)
        text = (
            f"ПРОЕКТ НЕ ПРИГОДЕН: пара {pair_key} сбылась в {percent}% "
            f"(n={pair_test['n']}, нужно не менее {min_pct}%). Кандидаты не запускаются: "
            "пересмотрите условия, пороги, промт или пару."
        )
        return [Proposal("project_result", text)]

    def _replacement_proposals(self, closed: list[dict], ranked: list[dict]) -> list[Proposal]:
        """Предложить замену unsuitable-модели пары лучшим suitable-кандидатом."""
        if not ranked:
            return []
        best = ranked[0]
        proposals: list[Proposal] = []
        for test in closed:
            if test["role"] != PAIR_MODEL_ROLE or test["verdict"] != UNSUITABLE:
                continue
            text = (
                f"Модель {test['subject']} сбылась в {_num(test['success_pct'])}% "
                f"(n={test['n']}) — предлагаем заменить на {best['subject']}: "
                f"{_summary(best)}. Пару меняет пользователь правкой llm.pair."
            )
            proposals.append(Proposal("replace_model", text))
        return proposals

    async def _best_pair_proposals(
        self, closed: list[dict], version: str, ranked: list[dict]
    ) -> list[Proposal]:
        """Предложить лучшую пару, когда закрыты тесты всех кандидатов."""
        if not any(test["role"] == CANDIDATE_ROLE for test in closed):
            return []
        done = {test["subject"] for test in await self._candidate_tests(version)}
        if any(model.id not in done for model in self._cfg.llm.candidates):
            return []
        choice = best_pair(ranked)
        if choice is None:
            return []
        first, second = choice
        total = (first["efficiency"] or 0.0) + (second["efficiency"] or 0.0)
        text = (
            f"Лучшая пара по эффективности: {first['subject']} + {second['subject']} "
            f"(сумма эффективности {_num(total, 4)}). Смену пары выполняет пользователь "
            "правкой llm.pair."
        )
        return [Proposal("best_pair", text)]
