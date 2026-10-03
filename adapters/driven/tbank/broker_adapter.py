"""Адаптер исполнения ордеров T-Invest (``OrderExecutionPort``).

Это единственное место в проекте, физически способное отправить ордер на
биржу. Поэтому здесь сосредоточены все предохранители:

1. **Блокировка чужих счетов.** Каждый вызов проверяет ``account_id`` против
   ``managed_account_id`` из конфигурации. Несовпадение — немедленное
   исключение: бот обязан работать ровно с одним счётом.
2. **Идемпотентность.** Ключ ``client_order_id`` (UUID ≤ 36 символов)
   генерируется **до** сетевого вызова и логируется. Повторная отправка того же
   ключа не создаёт вторую сделку.
3. **Только long.** Направление открытия — ``OrderDirection.ORDER_DIRECTION_BUY``;
   продажа возможна только для закрытия уже открытой позиции.
4. **quantity — лоты**, не штуки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import structlog

from adapters.driven.tbank.mappers import (
    instrument_to_domain,
    order_state_to_domain,
    position_to_domain,
    post_order_response_to_domain,
    quotation_to_decimal,
)
from adapters.driven.tbank.retry import classify_error
from core.domain.entities import (
    Instrument,
    OrderResult,
    OrderState,
    PortfolioState,
    Position,
    TradePlan,
)
from core.domain.enums import OrderSide

if TYPE_CHECKING:
    from uuid import UUID

    from adapters.driven.tbank.grpc_client import TInvestChannel

logger = structlog.get_logger(__name__)

MAX_CLIENT_ORDER_ID_LENGTH = 36


class ForeignAccountError(RuntimeError):
    """Попытка работать со счётом, отличным от ``managed_account_id``."""


class OrderSubmissionUncertainError(RuntimeError):
    """API не позволило установить, принята ли отправленная заявка."""


class TBankBrokerAdapter:
    """Реализация ``OrderExecutionPort`` поверх T-Invest API."""

    def __init__(self, channel: TInvestChannel, *, account_id: str) -> None:
        self._channel = channel
        self._account_id = account_id

    @property
    def managed_account_id(self) -> str:
        return self._account_id

    def configure_managed_account(self, account_id: str) -> None:
        account_id = account_id.strip()
        if not account_id:
            raise ValueError("managed_account_id не может быть пустым")
        self._account_id = account_id

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Возвращает счета UsersService без секретных данных."""
        from t_tech.invest.grpc.schemas import GetAccountsRequest

        response = await retry_read_safe(
            lambda: self._channel.services.users.get_accounts(request=GetAccountsRequest())
        )
        return [
            {
                "id": str(account.id),
                "name": str(getattr(account, "name", "") or "Без названия"),
                "status": int(getattr(account, "status", 0)),
                "type": int(getattr(account, "type", 0)),
                "is_current": str(account.id) == self._account_id,
            }
            for account in getattr(response, "accounts", []) or []
        ]

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

    async def _submit_order(self, request: Any) -> Any:
        return await self._orders.post_order(request=request)

    async def _cancel_order_request(self, request: Any) -> Any:
        return await self._orders.cancel_order(request=request)

    async def _get_order_state(self, request: Any) -> Any:
        return await self._orders.get_order_state(request=request)

    async def _submit_order_and_reconcile(
        self,
        request: Any,
        *,
        client_order_id: str,
        operation_name: str,
    ) -> OrderResult:
        """Submit once; on an uncertain transport error, query by request ID.

        Never blindly repost a financial mutation. If the request state cannot
        establish whether it was accepted, bubble up an explicit uncertain result
        so the caller can halt and reconcile instead of creating a duplicate.
        """
        try:
            response = await self._submit_order(request)
        except Exception as submit_error:
            if classify_error(submit_error) == "permanent":
                raise

            from t_tech.invest.grpc.schemas import GetOrderStateRequest, OrderIdType

            state_request = GetOrderStateRequest(
                account_id=self._account_id,
                order_id=client_order_id,
                order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
            )
            try:
                state = await retry_read_safe(lambda: self._get_order_state(state_request))
            except Exception as reconcile_error:
                msg = (
                    f"{operation_name}: исход отправки неизвестен; проверьте заявку "
                    "по клиентскому order_id до повторной отправки"
                )
                raise OrderSubmissionUncertainError(msg) from reconcile_error

            if not getattr(state, "order_id", ""):
                msg = (
                    f"{operation_name}: API не подтвердил результат; проверьте заявку "
                    "по клиентскому order_id до повторной отправки"
                )
                raise OrderSubmissionUncertainError(msg) from submit_error

            mapped = order_state_to_domain(state)
            logger.warning(
                "post_order_reconciled_after_transport_error",
                operation=operation_name,
                client_order_id=client_order_id,
            )
            return OrderResult(
                order_id=mapped.order_id,
                client_order_id=client_order_id,
                status=mapped.status,
                filled_lots=mapped.filled_lots,
                message=mapped.message,
            )

        return post_order_response_to_domain(response, client_order_id=client_order_id)

    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult:
        """Выставляет long-ордер. ``quantity`` — лоты."""
        from t_tech.invest.grpc.schemas import OrderDirection, OrderType, PostOrderRequest

        self._assert_managed_account(self._account_id)
        if quantity <= 0:
            raise ValueError("Количество лотов должно быть больше нуля")

        client_order_id = _client_order_id(plan.id)
        request = PostOrderRequest(
            instrument_id=plan.instrument.uid,
            quantity=quantity,
            price=None,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id=self._account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=client_order_id,
        )

        logger.info(
            "post_order",
            client_order_id=client_order_id,
            plan_id=str(plan.id),
            instrument=plan.instrument.uid,
            lots=quantity,
        )

        return await self._submit_order_and_reconcile(
            request, client_order_id=client_order_id, operation_name="post_order"
        )

    async def cancel_order(self, order_id: str) -> None:
        from t_tech.invest.grpc.schemas import CancelOrderRequest, OrderIdType

        self._assert_managed_account(self._account_id)
        request = CancelOrderRequest(
            account_id=self._account_id,
            order_id=order_id,
            order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
        )
        await self._cancel_order_request(request)

    async def get_order_status(self, order_id: str) -> OrderState:
        from t_tech.invest.grpc.schemas import GetOrderStateRequest, OrderIdType

        self._assert_managed_account(self._account_id)
        request = GetOrderStateRequest(
            account_id=self._account_id,
            order_id=order_id,
            order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
        )
        response = await retry_read_safe(lambda: self._get_order_state(request))
        return order_state_to_domain(response)

    async def close_position(self, position: Position, reason: str) -> OrderResult:
        """Закрывает позицию рыночной продажей (не шорт — закрытие лонга)."""
        from t_tech.invest.grpc.schemas import OrderDirection, OrderType, PostOrderRequest

        self._assert_managed_account(self._account_id)
        client_order_id = _client_order_id(position.linked_plan_id, suffix="close")

        request = PostOrderRequest(
            instrument_id=position.instrument.uid,
            quantity=max(position.lots, 1),
            price=None,
            direction=OrderDirection.ORDER_DIRECTION_SELL,
            account_id=self._account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=client_order_id,
        )
        logger.warning(
            "close_position",
            instrument=position.instrument.ticker,
            lots=position.lots,
            reason=reason,
            client_order_id=client_order_id,
        )
        return await self._submit_order_and_reconcile(
            request, client_order_id=client_order_id, operation_name="close_position"
        )

    async def get_open_positions(self) -> list[Position]:
        from t_tech.invest.grpc.schemas import PortfolioRequest

        self._assert_managed_account(self._account_id)
        request = PortfolioRequest(account_id=self._account_id)
        response = await retry_read_safe(
            lambda: self._channel.services.operations.get_portfolio(request=request)
        )
        return await self._positions_from_response(response)

    async def _positions_from_response(self, response: Any) -> list[Position]:
        from uuid import UUID

        result: list[Position] = []
        raw_positions = getattr(response, "positions", None)
        if raw_positions is None:
            raw_positions = getattr(response, "securities", []) or []
        for raw in raw_positions:
            uid = str(getattr(raw, "instrument_uid", "") or "")
            if not uid:
                continue
            instrument = await self.get_instrument(uid)
            if instrument is None:
                continue
            quantity_value = getattr(raw, "quantity", None)
            if quantity_value is None or quotation_to_decimal(quantity_value) <= 0:
                continue
            result.append(position_to_domain(raw, instrument, plan_id=UUID(int=0)))
        return result

    async def get_instrument(self, uid: str) -> Instrument | None:
        from t_tech.invest.grpc.schemas import (
            InstrumentIdType,
            InstrumentRequest,
        )

        request = InstrumentRequest(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        response = await retry_read_safe(
            lambda: self._channel.services.instruments.get_instrument_by(request=request)
        )
        instrument = getattr(response, "instrument", None)
        if instrument is None:
            return None
        return instrument_to_domain(instrument)

    async def list_instruments(self) -> list[Instrument]:
        """Справочник доступных для торговли акций."""
        from t_tech.invest.grpc.schemas import InstrumentsRequest, InstrumentStatus

        request = InstrumentsRequest(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        response = await retry_read_safe(
            lambda: self._channel.services.instruments.shares(request=request)
        )
        return [instrument_to_domain(i) for i in getattr(response, "instruments", []) or []]

    async def get_portfolio(self) -> PortfolioState | None:
        from t_tech.invest.grpc.schemas import PortfolioRequest

        from adapters.driven.tbank.mappers import portfolio_response_to_domain

        if not self._account_id:
            return None
        self._assert_managed_account(self._account_id)
        request = PortfolioRequest(account_id=self._account_id)
        response = await retry_read_safe(
            lambda: self._channel.services.operations.get_portfolio(request=request)
        )
        return portfolio_response_to_domain(response, account_id=self._account_id)

    async def aclose(self) -> None:
        await self._channel.aclose()


async def retry_read_safe(operation: Any) -> Any:
    """Read-only повтор без ключа идемпотентности."""
    from adapters.driven.tbank.retry import retry_read

    return await retry_read(operation, operation_name="read_call")


def _client_order_id(plan_id: UUID, *, suffix: str = "") -> str:
    """Детерминированный UUID ключа для каждой мутации по торговому плану."""
    if not suffix:
        return str(plan_id)
    return str(uuid5(NAMESPACE_URL, f"red-bot:{plan_id}:{suffix}"))[:MAX_CLIENT_ORDER_ID_LENGTH]


def utcnow() -> datetime:
    """Текущий момент в UTC. Обёртка — чтобы тесты могли подменить."""
    return datetime.now(tz=UTC)


__all__ = ["ForeignAccountError", "OrderSide", "TBankBrokerAdapter"]
