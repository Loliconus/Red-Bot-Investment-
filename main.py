"""Точка входа Red-Bot.

Запуск::

    python main.py run --mode sandbox
    python main.py run --mode backtest --no-gui
    python main.py db bootstrap
    python main.py secrets set-token

Режим ``live`` требует явного подтверждения и прохождения всех проверок
(sandbox-эксплуатация, walk-forward, рабочий kill switch) — см. чек-лист
в разделе XII ТЗ.
"""

from __future__ import annotations

from adapters.driving.cli.main import app_cli

if __name__ == "__main__":
    app_cli()
