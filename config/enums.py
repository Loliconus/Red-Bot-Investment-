"""Bootstrap-уровневые перечисления.

Важно: ``ExecutionMode`` существует только здесь и используется исключительно в
``application/composition.py`` для выбора адаптеров. Ядро намеренно не знает о
понятии «режим исполнения» — для него любой режим это просто конкретная
реализация порта.
"""

from enum import StrEnum


class ExecutionMode(StrEnum):
    """Контур исполнения ордеров."""

    LIVE = "live"
    SANDBOX = "sandbox"
    BACKTEST = "backtest"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
