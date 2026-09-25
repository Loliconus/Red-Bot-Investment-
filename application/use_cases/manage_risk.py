"""Операционные настройки риска и staged смена управляемого счёта."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from application.composition import AppContext
from application.use_cases.update_strategy_config import update_strategy_config
from core.risk.cost_model import estimate_costs, min_viable_target_pct

ACCOUNT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")
COUNTERTREND_PHRASE = "РАЗРЕШИТЬ КОНТРТРЕНД"


def mask_account(value: str) -> str:
    return ("•" * max(len(value) - 4, 4) + value[-4:]) if value else "Не задан"


@dataclass(frozen=True, slots=True)
class RiskState:
    masked_account_id: str
    risk_per_trade_pct: Decimal
    max_loss_rub: Decimal | None
    multiplier: Decimal
    required_target_pct: Decimal
    allow_counter_trend: bool
    max_holding_hours: int
    balance_rub: Decimal | None
    config_version: int


def get_risk_state(context: AppContext) -> RiskState:
    config = context.config
    balance = context.portfolio.total_value if context.portfolio is not None else None
    # Превью при нулевом спреде (он неизвестен до появления котировки).
    costs = estimate_costs(notional=Decimal("1"), commission_rate=config.commission_rate)
    return RiskState(
        masked_account_id=mask_account(context.active_account_id),
        risk_per_trade_pct=config.risk_per_trade_pct,
        max_loss_rub=(balance * config.risk_per_trade_pct if balance is not None else None),
        multiplier=config.min_viable_target_multiplier,
        required_target_pct=min_viable_target_pct(
            costs.total_pct.value, multiplier=config.min_viable_target_multiplier
        )
        * Decimal("100"),
        allow_counter_trend=config.allow_counter_trend,
        max_holding_hours=config.max_holding_hours,
        balance_rub=balance,
        config_version=config.version,
    )


async def update_risk(
    context: AppContext,
    *,
    multiplier: Decimal | None = None,
    risk_per_trade_pct: Decimal | None = None,
    allow_counter_trend: bool | None = None,
    max_holding_hours: int | None = None,
) -> RiskState:
    if multiplier is not None and (
        not Decimal("1") <= multiplier <= Decimal("5")
        or multiplier * 10 != (multiplier * 10).to_integral_value()
    ):
        raise ValueError("Множитель: от 1.0 до 5.0 с шагом 0.1")
    if risk_per_trade_pct is not None and not Decimal("0") < risk_per_trade_pct <= Decimal("0.05"):
        raise ValueError("Риск на сделку: от 0 до 5%")
    if max_holding_hours is not None and not 1 <= max_holding_hours <= 720:
        raise ValueError("TTL: от 1 до 720 часов")
    await update_strategy_config(
        context,
        min_viable_target_multiplier=multiplier,
        risk_per_trade_pct=risk_per_trade_pct,
        allow_counter_trend=allow_counter_trend,
        max_holding_hours=max_holding_hours,
    )
    return get_risk_state(context)


def validate_account_change(context: AppContext, new_account_id: str) -> None:
    if context.scheduler is not None and context.scheduler.is_active:
        raise PermissionError("Для смены счёта сначала полностью остановите торговый процесс")
    if context.restart_required:
        raise ValueError("Перезапустите приложение перед следующим изменением счёта")
    if not ACCOUNT_PATTERN.fullmatch(new_account_id) or new_account_id == context.active_account_id:
        raise ValueError("Введите новый ID счёта: 4–64 буквы/цифры, дефис или подчёркивание")


async def change_managed_account(context: AppContext, new_account_id: str) -> None:
    """Только staging для следующего полного запуска; брокер НЕ меняется на лету."""
    validate_account_change(context, new_account_id)
    await context.repository.set_operational_value("managed_account_id", new_account_id)
    context.restart_required = True
