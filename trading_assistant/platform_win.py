"""Управление сном Windows и блокировка единственного экземпляра."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

WINDOWS = sys.platform == "win32"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep() -> None:
    """Запретить автоматический сон на время работы текущего потока."""
    if WINDOWS:
        import ctypes

        if not ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            raise OSError("Не удалось запретить сон системы")


def allow_sleep() -> None:
    """Снять запрет автоматического сна текущего потока."""
    if WINDOWS:
        import ctypes

        if not ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS):
            raise OSError("Не удалось снять запрет сна системы")


class AlreadyRunning(Exception):
    """Другой экземпляр сервиса уже удерживает блокировку."""


class SingleInstance:
    """Удерживать файловую блокировку до выхода из контекста."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            if WINDOWS:
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            if exc.errno in (None, 11, 13, 35, 36) or getattr(exc, "winerror", None) in (33, 36):
                raise AlreadyRunning(f"Сервис уже запущен: {self.path}") from exc
            raise
        self._file = file
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        file, self._file = self._file, None
        if file is None:
            return
        try:
            if WINDOWS:
                import msvcrt

                file.seek(0, os.SEEK_SET)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
