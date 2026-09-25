"""Порты ядра — закрытый перечень из шести контрактов.

Расширение перечня допускается только при появлении второй реализации.
"""

from core.ports.archive import ArchivePort
from core.ports.broker import OrderExecutionPort
from core.ports.clock import ClockPort, FrozenClock, SystemClock
from core.ports.market_data import MarketDataPort
from core.ports.notifier import NotificationPort
from core.ports.persistence import RepositoryPort

__all__ = [
    "ArchivePort",
    "ClockPort",
    "FrozenClock",
    "MarketDataPort",
    "NotificationPort",
    "OrderExecutionPort",
    "RepositoryPort",
    "SystemClock",
]
