"""Тесты единственного экземпляра и запрета сна (STEP 6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_assistant import platform_win
from trading_assistant.platform_win import AlreadyRunning, SingleInstance


def test_second_instance_raises(tmp_path: Path) -> None:
    """Второй экземпляр на том же файле блокировки получает AlreadyRunning."""
    path = tmp_path / "data" / "service.lock"
    with SingleInstance(path):
        second = SingleInstance(path)
        with pytest.raises(AlreadyRunning):
            second.__enter__()


def test_lock_released_after_exit(tmp_path: Path) -> None:
    """После __exit__ блокировка снимается и файл можно занять снова."""
    path = tmp_path / "service.lock"
    with SingleInstance(path):
        pass
    with SingleInstance(path):
        pass


def test_lock_creates_parent_dirs(tmp_path: Path) -> None:
    """Каталог data создаётся самим SingleInstance."""
    path = tmp_path / "nested" / "data" / "service.lock"
    with SingleInstance(path):
        assert path.exists()


def test_lock_file_handles_empty_file(tmp_path: Path) -> None:
    """Пустой файл блокировки тоже занимается и освобождается."""
    path = tmp_path / "service.lock"
    path.touch()
    with SingleInstance(path):
        assert path.stat().st_size == 0


def test_sleep_calls_work_on_windows() -> None:
    """На Windows prevent_sleep и allow_sleep проходят без исключения."""
    if not platform_win.WINDOWS:
        pytest.skip("проверка Windows API")
    platform_win.prevent_sleep()
    platform_win.allow_sleep()


def test_sleep_calls_are_noop_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """На не-Windows вызовы управления сном ничего не делают и не падают."""
    monkeypatch.setattr(platform_win, "WINDOWS", False)
    platform_win.prevent_sleep()
    platform_win.allow_sleep()
