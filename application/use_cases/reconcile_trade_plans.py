"""Сверка торговых планов и идентификаторов инструментов с реальными данными.

Зачем этот модуль существует. План ссылается на инструмент по
``instrument_uid``, а инструмент живёт в корзине (таблица ``instruments``).
Если инструмент из корзины удалён, а план остался открытым, план становится
неисполнимым: нет UID для заявки и для свечей. Раньше чтение такого плана
бросало ``ValueError``, и одна «сирота» роняла и мониторинг позиций, и
дашборд (HTTP 500 на `/`).

Откуда берутся сироты на практике:

* инструмент удалили из корзины в GUI, не закрыв открытый план;
* план создан в другом контуре: бэктест резолвил инструмент локально и мог
  сохранить план с не-UID идентификатором (например, FIGI);
* БД досталась от старой версии, где разрешение инструмента шло через
  захардкоженный справочник.

Поэтому здесь две операции: починка открытых планов без инструмента и
диагностика подозрительных идентификаторов в корзине.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from core.domain.entities import Instrument

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)

#: Причина закрытия плана, которую увидит пользователь в журнале.
ORPHANED_PLAN_REASON = "инструмент отсутствует в корзине"

#: ``instrument_uid`` из T-Invest — UUID. FIGI (``BBG...``) и ``instrument_id``
#: (тикер) в это поле попадать не должны: идентификаторы не взаимозаменяемы.
_UID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_instrument_uid(value: str) -> bool:
    """Похож ли идентификатор на ``instrument_uid`` (UUID), а не на FIGI/тикер."""
    return bool(_UID_PATTERN.fullmatch(value.strip()))


def suspicious_instruments(instruments: list[Instrument]) -> list[Instrument]:
    """Инструменты корзины с идентификатором не-UUID формата."""
    return [i for i in instruments if not looks_like_instrument_uid(i.uid)]


async def reconcile_orphaned_trade_plans(ctx: AppContext) -> tuple[str, ...]:
    """Закрывает открытые планы, чей инструмент исчез из корзины.

    Возвращает id закрытых планов. Намеренно ничего не делает, если корзина
    пуста: при сбое загрузки все планы выглядели бы «сиротами», и массовое
    закрытие реальных позиций было бы катастрофой.
    """
    if not ctx.instruments:
        logger.warning(
            "orphaned_trade_plans_skipped",
            reason="корзина инструментов пуста: сверка невозможна",
        )
        return ()

    closed = await ctx.repository.close_orphaned_trade_plans(ORPHANED_PLAN_REASON)
    if closed:
        logger.warning(
            "orphaned_trade_plans_closed",
            count=len(closed),
            plan_ids=list(closed),
            reason=ORPHANED_PLAN_REASON,
        )
    return closed


async def log_suspicious_instrument_uids(ctx: AppContext) -> list[Instrument]:
    """Сообщает об инструментах корзины с не-UUID идентификатором.

    Такая запись означает, что инструмент попал в корзину не через
    ``resolve_instrument`` (старый справочник подставлял FIGI вместо UID).
    Торговать такими инструментами нельзя: заявка уйдёт с неверным id.
    """
    suspicious = suspicious_instruments(list(ctx.instruments))
    for instrument in suspicious:
        logger.warning(
            "instrument_uid_not_uuid",
            ticker=instrument.ticker,
            class_code=instrument.class_code,
            uid=instrument.uid,
            action="удалите инструмент из корзины и добавьте заново через тикер",
        )
    return suspicious
