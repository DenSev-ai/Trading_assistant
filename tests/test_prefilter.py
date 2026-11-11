"""Тесты prefilter.py: normalize_title и passes_prefilter."""
from __future__ import annotations

import pytest

from trading_assistant.config import Keywords
from trading_assistant.news.prefilter import normalize_title, passes_prefilter

KW = Keywords(include=["ставк", "сбер"], exclude=["футбол"])


def test_normalize_title() -> None:
    assert normalize_title("ЦБ повысил КЛЮЧЕВУЮ ставку!") == "цб повысил ключевую ставку"
    assert normalize_title("Ёлка и ель") == "елка и ель"
    assert normalize_title("  Множественные   пробелы  ") == "множественные пробелы"


@pytest.mark.parametrize(
    "title,summary,expected",
    [
        ("ЦБ повысил ключевую ставку", "", True),
        ("Футбол: Сбербанк Арена", "", False),
        ("Погода в Москве", "", False),
        ("Погода", "Сбербанк объявил дивиденды", True),
        ("СБЕР вырос", "", True),
    ],
)
def test_passes_prefilter(title: str, summary: str, expected: bool) -> None:
    assert passes_prefilter(title, summary, KW) is expected


def test_passes_prefilter_no_include() -> None:
    kw = Keywords(include=[], exclude=["футбол"])
    assert passes_prefilter("Любая новость", "", kw) is True


def test_passes_prefilter_exclude_wins() -> None:
    kw = Keywords(include=["ставк"], exclude=["ставк"])
    # exclude overrides include
    assert passes_prefilter("ставка ЦБ", "", kw) is False
