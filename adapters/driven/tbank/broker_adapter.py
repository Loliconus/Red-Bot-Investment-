"""Адаптер исполнения ордеров T-Invest (``OrderExecutionPort``).

Это единственное место в проекте, физически способное отправить ордер на
биржу. Поэтому здесь сосредоточены все предохранители:

1. **Блокировка чужих счетов.** Каждый вызов проверяет ``account_id`` против
   ``managed_account_id`` из конфигурации. Несовпадение — немедленное
   исключение: бот обязан работать ровно с одним счётом.
2. **Идемпотентность.** Ключ ``client_order_id`` (UUID ≤ 36 символов)
   генерируется **до** сетевого вызова и логируется. Повторная отправка того же
   ключа не создаёт вторую сделку.
3. **Только long.** Направление захардкожено в ``ORDER_DIRECTION_BUY``; шорты
   удалены из домена полностью.
4. **quantity — лоты**, не штуки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from adapters.driven.tbank.mappers import (
    instrument_to_domain,
    order_state_to_domain,
    position_to_domain,
    post_order_response_to_domain,
)
from adapters.driven.tbank.retry import retry_async
from core.domain.entities import Instrument, OrderResult, OrderState, Position, TradePlan
from core.domain.enums import OrderSide

if TYPE_CHECKING:
    from uuid import UUID

    from adapters.driven.tbank.grpc_client import TInvestChannel

logger = structlog.get_logger(__name__)

#: Направление «покупка» в API.
ORDER_DIRECTION_BUY = 1
#: Тип заявки «рыночная». Лимитные заявки в MVP не используются.
ORDER_TYPE_MARKET = 1

MAX_CLIENT_ORDER_ID_LENGTH = 36


class ForeignAccountError(RuntimeError):
    """Попытка работать со счётом, отличным от ``managed_account_id``."""


class TBankBrokerAdapter:
    """Реализация ``OrderExecutionPort`` поверх T-Invest API."""

    def __init__(self, channel: TInvestChannel, *, account_id: str) -> None:
        self._channel = channel
        self._account_id = account_id

    @property
    def managed_account_id(self) -> str:
        return self._account_id

    def _assert_managed_account(self, account_id: str) -> None:
        if not self._account_id:
            msg = "managed_account_id не задан: работа со счетами запрещена"
            raise ForeignAccountError(msg)
        if account_id != self._account_id:
            msg = f"Попытка операции со счётом {account_id}, разрешён только {self._account_id}"
            raise ForeignAccountError(msg)

    @property
    def _orders(self) -> Any:
        return self._channel.services.orders

    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult:
        """Выставляет long-ордер. ``quantity`` — лоты."""
        from t_tech.invest.grpc.schemas import PostOrderRequest

        self._assert_managed_account(self._account_id)

        client_order_id = _client_order_id(plan.id)
        request = PostOrderRequest(
            instrument_id=plan.instrument.uid,
            quantity=quantity,
            price=None,
            direction=ORDER_DIRECTION_BUY,
            account_id=self._account_id,
            order_type=ORDER_TYPE_MARKET,
            order_id=client_order_id,
        )

        logger.info(
            "post_order",
            client_order_id=client_order_id,
            plan_id=str(plan.id),
            instrument=plan.instrument.uid,
            lots=quantity,
        )

        response = await retry_async(
            lambda: self._orders.post_order(request=request),
            idempotency_key=client_order_id,
            operation_name="post_order",
        )
        return post_order_response_to_domain(response, client_order_id=client_order_id)

    async def cancel_order(self, order_id: str) -> None:
        from t_tech.invest.grpc.schemas import CancelOrderRequest

        self._assert_managed_account(self._account_id)
        request = CancelOrderRequest(account_id=self._account_id, order_id=order_id)
        await retry_async(
            lambda: self._orders.cancel_order(request=request),
            idempotency_key=order_id,
            operation_name="cancel_order",
        )

    async def get_order_status(self, order_id: str) -> OrderState:
        from t_tech.invest.grpc.schemas import GetOrderStateRequest

        self._assert_managed_account(self._account_id)
        request = GetOrderStateRequest(account_id=self._account_id, order_id=order_id)
        response = await retry_read_safe(lambda: self._orders.get_order_state(request=request))
        return order_state_to_domain(response)

    async def close_position(self, position: Position, reason: str) -> OrderResult:
        """Закрывает позицию рыночной продажей (не шорт — закрытие лонга)."""
        from t_tech.invest.grpc.schemas import PostOrderRequest

        self._assert_managed_account(self._account_id)
        client_order_id = _client_order_id(position.linked_plan_id, suffix="close")
        sell_direction = 2  # ORDER_DIRECTION_SELL

        request = PostOrderRequest(
            instrument_id=position.instrument.uid,
            quantity=max(position.lots, 1),
            price=None,
            direction=sell_direction,
            account_id=self._account_id,
            order_type=ORDER_TYPE_MARKET,
            order_id=client_order_id,
        )
        logger.warning(
            "close_position",
            instrument=position.instrument.ticker,
            lots=position.lots,
            reason=reason,
            client_order_id=client_order_id,
        )
        response = await retry_async(
            lambda: self._orders.post_order(request=request),
            idempotency_key=client_order_id,
            operation_name="close_position",
        )
        return post_order_response_to_domain(response, client_order_id=client_order_id)

    async def get_open_positions(self) -> list[Position]:
        from t_tech.invest.grpc.schemas import PositionsRequest

        self._assert_managed_account(self._account_id)
        request = PositionsRequest(account_id=self._account_id)
        response = await retry_read_safe(
            lambda: self._channel.services.operations.get_positions(request=request)
        )
        return await self._positions_from_response(response)

    async def _positions_from_response(self, response: Any) -> list[Position]:
        from uuid import UUID

        result: list[Position] = []
        for raw in getattr(response, "securities", []) or []:
            uid = str(getattr(raw, "instrument_uid", "") or "")
            instrument = await self.get_instrument(uid)
            if instrument is None:
                continue
            quantity = int(float(getattr(raw, "balance", 0) or 0))
            if quantity <= 0:
                continue
            result.append(position_to_domain(raw, instrument, plan_id=UUID(int=0)))
        return result

    async def get_instrument(self, uid: str) -> Instrument | None:
        from t_tech.invest.grpc.schemas import (
            InstrumentIdType,
            InstrumentRequest,
        )

        request = InstrumentRequest(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        try:
            response = await retry_read_safe(
                lambda: self._channel.services.instruments.get_instrument_by(request=request)
            )
        except Exception:  # noqa: BLE001 - справочник может быть недоступен
            logger.warning("instrument_lookup_failed", uid=uid)
            return None

        instrument = getattr(response, "instrument", None)
        if instrument is None:
            return None
        return instrument_to_domain(instrument)

    async def list_instruments(self) -> list[Instrument]:
        """Справочник акций: нужен для первичного заполнения БД."""
        from t_tech.invest.grpc.schemas import InstrumentsRequest

        request = InstrumentsRequest(instrument_status=1)  # INSTRUMENT_STATUS_BASE
        response = await retry_read_safe(
            lambda: self._channel.services.instruments.shares(request=request)
        )
        return [instrument_to_domain(i) for i in getattr(response, "instruments", []) or []]

    async def aclose(self) -> None:
        await self._channel.aclose()


async def retry_read_safe(operation: Any) -> Any:
    """Read-only повтор без ключа идемпотентности."""
    from adapters.driven.tbank.retry import retry_read

    return await retry_read(operation, operation_name="read_call")


def _client_order_id(plan_id: UUID, *, suffix: str = "") -> str:
    """Ключ идемпотентности: UUID плана (+ суффикс), не длиннее 36 символов."""
    raw = f"{plan_id}{'-' + suffix if suffix else ''}"
    return raw[:MAX_CLIENT_ORDER_ID_LENGTH]


def utcnow() -> datetime:
    """Текущий момент в UTC. Обёртка — чтобы тесты могли подменить."""
    return datetime.now(tz=UTC)


__all__ = ["ForeignAccountError", "OrderSide", "TBankBrokerAdapter"]
