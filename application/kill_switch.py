"""Kill switch — аварийная остановка торговли.

Смысл в том, что решение о остановке принимается **не** торговой логикой:
флаг выставляется извне (GUI, оператор, превышение дневного лимита убытка),
после чего любые попытки выставить ордер блокируются на уровне юзкейса
``execute_order``.

Состояние持久化 флагом в памяти процесса и публикуется через шину событий,
чтобы GUI и нотификатор отреагировали немедленно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from core.domain.events import KillSwitchEngaged

if TYPE_CHECKING:
    from application.events import EventBus
    from core.ports.clock import ClockPort

logger = structlog.get_logger(__name__)

ZERO = Decimal("0")


@dataclass(slots=True)
class KillSwitch:
    """Простой, но обязательный предохранитель."""

    clock: ClockPort
    event_bus: EventBus
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    _engaged: bool = False
    _reason: str = ""
    _engaged_at: datetime | None = None
    _initiated_by: str = ""
    _day_start_equity: Decimal | None = None
    _day_key: str = field(default="")

    @property
    def is_engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def engaged_at(self) -> datetime | None:
        return self._engaged_at

    async def engage(self, reason: str, *, initiated_by: str = "system") -> None:
        """Включает блокировку."""
        if self._engaged:
            return
        self._engaged = True
        self._reason = reason
        self._engaged_at = self.clock.now()
        self._initiated_by = initiated_by
        logger.error("kill_switch_engaged", reason=reason, by=initiated_by)
        await self.event_bus.publish(
            KillSwitchEngaged(
                occurred_at=self._engaged_at,
                reason=reason,
                initiated_by=initiated_by,
            )
        )

    def release(self) -> None:
        """Снимает блокировку. Только ручное действие оператора."""
        self._engaged = False
        self._reason = ""
        self._engaged_at = None
        self._initiated_by = ""
        logger.warning("kill_switch_released")

    def reset_day(self, equity: Decimal) -> None:
        """Фиксирует equity на начало дня — база для лимита дневного убытка."""
        self._day_start_equity = equity
        self._day_key = self.clock.now().date().isoformat()

    def check_daily_loss(self, current_equity: Decimal) -> bool:
        """Проверяет дневной лимит убытка. True — лимит превышен."""
        if self._day_start_equity is None or self._day_start_equity == ZERO:
            return False
        loss_pct = (self._day_start_equity - current_equity) / self._day_start_equity
        return loss_pct >= self.daily_loss_limit_pct

    async def update_equity(self, equity: Decimal) -> None:
        """Обновляет equity и при превышении лимита включает блокировку."""
        now_key = self.clock.now().date().isoformat()
        if now_key != self._day_key:
            self.reset_day(equity)
            return
        if self.check_daily_loss(equity):
            await self.engage(
                f"дневной лимит убытка {self.daily_loss_limit_pct * 100:.1f}% превышен",
                initiated_by="risk",
            )
