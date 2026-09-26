"""Управление торговым счётом и администрирование счетов песочницы."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import structlog

from application.composition import AppContext
from config.enums import ExecutionMode
from core.domain.entities import PortfolioState

logger = structlog.get_logger(__name__)
ZERO = Decimal("0")


def _mask(value: str) -> str:
    return ("•" * max(len(value) - 4, 4) + value[-4:]) if value else "Не задан"


async def get_account_overview(context: AppContext) -> dict[str, Any]:
    """Возвращает детальную информацию о текущем счёте и доступных счетах."""
    mode = context.execution_mode
    is_sandbox = mode is ExecutionMode.SANDBOX
    is_live = mode is ExecutionMode.LIVE
    is_backtest = mode is ExecutionMode.BACKTEST

    portfolio = context.portfolio
    if portfolio is None:
        portfolio = await refresh_portfolio(context)

    balance = portfolio.total_value if portfolio else ZERO
    cash = portfolio.available_cash if portfolio else ZERO
    positions_value = portfolio.positions_value if portfolio else ZERO

    # Получаем список счетов (для песочницы)
    accounts: list[dict[str, Any]] = []
    if hasattr(context.broker, "get_sandbox_accounts"):
        try:
            accounts = await context.broker.get_sandbox_accounts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_sandbox_accounts_failed", error=str(exc))

    if not accounts and context.active_account_id:
        accounts = [
            {
                "id": context.active_account_id,
                "name": "Текущий счёт",
                "status": 1,
                "type": 1,
                "is_current": True,
            }
        ]

    return {
        "account_id": context.active_account_id,
        "masked_account_id": _mask(context.active_account_id)
        if is_live
        else (context.active_account_id or "Не задан"),
        "mode": mode.value,
        "is_sandbox": is_sandbox,
        "is_live": is_live,
        "is_backtest": is_backtest,
        "total_value": balance,
        "available_cash": cash,
        "positions_value": positions_value,
        "accounts": accounts,
        "restart_required": context.restart_required,
    }


async def topup_sandbox(context: AppContext, amount: Decimal) -> Decimal:
    """Пополняет баланс счёта в песочнице виртуальными средствами."""
    if amount <= ZERO:
        raise ValueError("Сумма пополнения должна быть больше 0")

    account_id = context.active_account_id
    if not account_id:
        account_id = await create_sandbox_account(context, "Основной счёт")

    # Вызываем метод брокера песочницы при наличии
    if hasattr(context.broker, "sandbox_pay_in"):
        try:
            await context.broker.sandbox_pay_in(account_id, amount)
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker_sandbox_pay_in_failed", error=str(exc))

    # Обновляем портфель
    if context.portfolio is not None:
        new_total = context.portfolio.total_value + amount
        new_cash = context.portfolio.available_cash + amount
        context.portfolio = PortfolioState(
            account_id=account_id,
            total_value=new_total,
            available_cash=new_cash,
            positions_value=context.portfolio.positions_value,
            updated_at=context.clock.now(),
        )
    else:
        context.portfolio = PortfolioState(
            account_id=account_id,
            total_value=amount,
            available_cash=amount,
            positions_value=ZERO,
            updated_at=context.clock.now(),
        )

    # Сохраняем в БД
    await _persist_portfolio(context, context.portfolio)
    logger.info("sandbox_topup_successful", account_id=account_id, amount=str(amount))
    return context.portfolio.total_value


async def create_sandbox_account(context: AppContext, name: str = "Red-Bot Sandbox") -> str:
    """Создаёт новый виртуальный счёт в песочнице и делает его активным."""
    new_id = ""
    if hasattr(context.broker, "open_sandbox_account"):
        try:
            new_id = await context.broker.open_sandbox_account(name=name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker_open_sandbox_account_failed", error=str(exc))

    if not new_id:
        new_id = f"sb-{int(context.clock.now().timestamp())}"

    # Привязываем новый счёт
    await switch_sandbox_account(context, new_id)

    # Стартовое пополнение на 1 000 000 руб
    await topup_sandbox(context, Decimal("1000000"))
    logger.info("sandbox_account_created_and_activated", account_id=new_id)
    return new_id


async def switch_sandbox_account(context: AppContext, account_id: str) -> None:
    """Переключает активный счёт в песочнице на лету."""
    account_id = account_id.strip()
    if not account_id:
        raise ValueError("Идентификатор счёта не может быть пустым")

    context.managed_account_id = account_id
    broker_obj: Any = context.broker
    if hasattr(broker_obj, "_account_id"):
        broker_obj._account_id = account_id  # noqa: SLF001
    if hasattr(broker_obj, "account_id"):
        broker_obj.account_id = account_id

    await context.repository.set_operational_value("managed_account_id", account_id)
    await refresh_portfolio(context)
    logger.info("sandbox_account_switched", account_id=account_id)


async def close_sandbox_account(context: AppContext, account_id: str) -> None:
    """Закрывает счёт в песочнице."""
    account_id = account_id.strip()
    if not account_id:
        raise ValueError("Идентификатор счёта не указан")

    if hasattr(context.broker, "close_sandbox_account"):
        await context.broker.close_sandbox_account(account_id)

    # Если закрыли текущий счёт — переключаемся на другой или создаём новый
    if account_id == context.active_account_id:
        accounts: list[dict[str, Any]] = []
        if hasattr(context.broker, "get_sandbox_accounts"):
            accounts = await context.broker.get_sandbox_accounts()
        other = next((a["id"] for a in accounts if a["id"] != account_id), None)
        if other:
            await switch_sandbox_account(context, other)
        else:
            await create_sandbox_account(context, "Основной счёт")
    logger.info("sandbox_account_closed", account_id=account_id)


async def refresh_portfolio(context: AppContext) -> PortfolioState:
    """Запрашивает актуальный снимок портфеля у брокера или из БД."""
    portfolio = None
    if hasattr(context.broker, "get_portfolio"):
        try:
            portfolio = await context.broker.get_portfolio()
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker_get_portfolio_failed", error=str(exc))

    if portfolio is None:
        try:
            portfolio = await context.repository.get_latest_portfolio_state()
        except Exception:  # noqa: BLE001
            portfolio = None

    if portfolio is None:
        portfolio = PortfolioState(
            account_id=context.active_account_id or "sandbox-01",
            total_value=Decimal("1000000"),
            available_cash=Decimal("1000000"),
            positions_value=ZERO,
            updated_at=context.clock.now(),
        )

    context.portfolio = portfolio
    await _persist_portfolio(context, portfolio)
    return portfolio


async def _persist_portfolio(context: AppContext, portfolio: PortfolioState) -> None:
    try:
        payload = json.dumps(
            {
                "account_id": portfolio.account_id,
                "total_value": str(portfolio.total_value),
                "available_cash": str(portfolio.available_cash),
                "positions_value": str(portfolio.positions_value),
                "updated_at": portfolio.updated_at.isoformat(),
            }
        )
        await context.repository.save_portfolio_state(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_portfolio_state_failed", error=str(exc))
