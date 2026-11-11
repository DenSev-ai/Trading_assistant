"""Точка входа: `python -m trading_assistant --check-config`."""
from __future__ import annotations

import argparse
import sys

from .config import EnvSettings, env_checklist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trading_assistant",
        description="Trading assistant — сервис прогнозов российского фондового рынка",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="проверить .env и настройки, распечатать чек-лист",
    )
    args = parser.parse_args(argv)

    if args.check_config:
        env = EnvSettings()  # читает .env в текущей папке
        lines, ok = env_checklist(env)
        for line in lines:
            print(line)
        return 0 if ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
