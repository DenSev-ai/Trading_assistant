"""Настройка логирования: консоль + data/app.log + data/errors.log."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(data_dir: str | Path, level: int = logging.INFO) -> None:
    """Консоль, data/app.log (ротация 1 МБ × 3) и data/errors.log (только ERROR). Создаёт папку."""
    log_dir = Path(data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    app_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    errors_handler = RotatingFileHandler(
        log_dir / "errors.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    errors_handler.setLevel(logging.ERROR)
    errors_handler.setFormatter(formatter)
    root.addHandler(errors_handler)
