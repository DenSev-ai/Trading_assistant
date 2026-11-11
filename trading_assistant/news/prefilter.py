"""Предфильтр новостей по ключевым словам (CONTRACTS §2)."""
from __future__ import annotations

import re

from ..config import Keywords


def normalize_title(title: str) -> str:
    """Привести заголовок к норме: lower, ё->е, только буквы/цифры/пробел, схлопнуть пробелы."""
    title = title.lower().replace("ё", "е")
    title = re.sub(r"[^a-zа-я0-9 ]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def passes_prefilter(title: str, summary: str, kw: Keywords) -> bool:
    """True, если есть include-подстрока в title/summary и нет exclude-подстроки."""
    text = f"{title} {summary}".lower()
    if kw.include and not any(sub.lower() in text for sub in kw.include):
        return False
    if any(sub.lower() in text for sub in kw.exclude):
        return False
    return True
