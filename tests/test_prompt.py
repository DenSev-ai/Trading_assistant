"""Тесты промпта: build_system_prompt и build_user_message (CONTRACTS §3)."""
from __future__ import annotations

import re
from pathlib import Path

from trading_assistant.config import SignalSettings
from trading_assistant.llm.analyzer import build_system_prompt, build_user_message

TEMPLATE = Path("prompts/analyze_news.txt").read_text(encoding="utf-8")


def signal(**kw: object) -> SignalSettings:
    """Пороги из config/settings.yaml; аргументы переопределяют отдельные поля."""
    base: dict[str, object] = {
        "min_confidence": 85,
        "min_severity": 6,
        "min_move_pct": 15.0,
        "max_horizon_days": 180,
        "magnitude_tolerance": 0.20,
        "priority_move_pct": 25,
        "priority_fast_days": 120,
    }
    base.update(kw)
    return SignalSettings(**base)  # type: ignore[arg-type]


def test_system_prompt_has_no_placeholders(registry) -> None:
    prompt = build_system_prompt(TEMPLATE, registry, signal())
    # Незаполненных плейсхолдеров вида {min_move} нет; { в ответе — только JSON-примеры.
    assert not re.search(r"\{[A-Za-z_]", prompt)


def test_system_prompt_contains_thresholds_and_instruments(registry) -> None:
    prompt = build_system_prompt(TEMPLATE, registry, signal())
    assert "15%" in prompt
    assert "180 календарных" in prompt
    assert "SBER — Сбербанк" in prompt
    # Выключенный инструмент в блок ИНСТРУМЕНТЫ не попадает (упоминание в правилах — часть шаблона).
    assert "IMOEX — " not in prompt


def test_system_prompt_min_move_without_trailing_zero(registry) -> None:
    prompt = build_system_prompt(TEMPLATE, registry, signal(min_move_pct=15.5))
    assert "15.5%" in prompt


def test_user_message_exact_format() -> None:
    msg = build_user_message(
        title="ЦБ повысил ставку",
        text="Текст новости",
        sources_count=3,
        max_chars=100,
        published_msk="06.11.2026 18:00",
        source="interfax",
    )
    assert msg == (
        "Дата публикации (МСК): 06.11.2026 18:00\n"
        "Источник: interfax\n"
        "Источников в кластере: 3\n"
        "<<<NEWS_DATA>>>\n"
        "ЦБ повысил ставку\n"
        "\n"
        "Текст новости\n"
        "<<<END_NEWS_DATA>>>"
    )


def test_user_message_truncates_text() -> None:
    msg = build_user_message("t", "x" * 50, 1, 10, "06.11.2026 18:00", "interfax")
    assert "<<<NEWS_DATA>>>" in msg
    assert "<<<END_NEWS_DATA>>>" in msg
    assert "x" * 10 + "\n" in msg
    assert "x" * 11 not in msg
