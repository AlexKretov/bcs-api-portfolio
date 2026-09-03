#!/usr/bin/env python3
"""Точка входа «на один запуск»: портфель и история сделок по БКС Торговому API.

Скрипт можно запустить как ``python bcs_portfolio.py portfolio`` (все команды
пакета доступны и здесь), так и просто ``python bcs_portfolio.py`` — тогда
будет выведен портфель за последние 30 дней по сделкам.

Требуется только ``requests``. Refresh-токен берётся из переменной окружения
``BCS_REFRESH_TOKEN`` или файла ``bcs-config.json``.

Полная документация — в README.md и в https://trade-api.bcs.ru
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bcs_api.cli import main  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) == 1:  # «просто запустил» → самый частый сценарий
        # Портфель + лимиты + сделки за последние 30 дней, одним прогоном.
        sys.argv[1:] = ["report", "--days", "30"]
    raise SystemExit(main())
