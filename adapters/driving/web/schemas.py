"""Pydantic-схемы Web GUI: граница валидации входящих данных.

Правила:
* ``Decimal`` — единственный допустимый тип для денег и цен;
* ``datetime`` — всегда tz-aware (pydantic это проверяет);
* конфиг стратегии принимается **только** явно перечисленными полями:
  ``model_config = ConfigDict(extra="forbid")`` защищает от опечаток в GUI.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime


class StatusResponse(BaseModel):
    execution_mode: str
    started_at: datetime | None
    kill_switch_engaged: bool
    open_plans: int
    open_positions: int
    instruments: list[str]
    config_version: int


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engaged: bool
    reason: str = Field(default="ручное управление из GUI", max_length=256)


class TradePlanResponse(BaseModel):
    id: UUID
    instrument: str
    ticker: str
    status: str
    entry_price: Decimal
    hard_stop_price: Decimal
    target_price: Decimal
    risk_reward: Decimal
    confluence_score: Decimal
    created_at: datetime
    expires_at: datetime
    quantity_lots: int
    rejection_reason: str | None = None


class PositionResponse(BaseModel):
    instrument: str
    ticker: str
    quantity: int
    lots: int
    average_entry: Decimal
    unrealized_pnl: Decimal | None = None


class DecisionResponse(BaseModel):
    id: UUID
    decision: str
    confluence_score: Decimal
    reasoning: list[dict[str, Any]]
    risk_check_passed: bool
    risk_check_reason: str | None
    thought_text: str
    created_at: datetime


class TradeReviewResponse(BaseModel):
    trade_plan_id: UUID
    verdict: str
    entry_price: Decimal
    exit_price: Decimal
    mfe: Decimal
    mae: Decimal
    exit_efficiency: Decimal
    post_exit_drift_pct: Decimal
    realized_pnl: Decimal
    closed_at: datetime


class HypothesisResponse(BaseModel):
    id: UUID
    text: str
    suggested_action: str
    status: str
    confidence: Decimal
    sample_size: int
    walk_forward_efficiency: Decimal | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class HypothesisApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: UUID
    confirmed_by_user: bool = Field(
        default=False,
        description="Обязательно True: автоприменение гипотез запрещено",
    )


class StrategyConfigUpdate(BaseModel):
    """Частичное обновление конфига. ``None`` = не менять."""

    model_config = ConfigDict(extra="forbid")

    risk_per_trade_pct: Decimal | None = Field(default=None, gt=0, le=Decimal("0.05"))
    min_viable_target_multiplier: Decimal | None = Field(default=None, ge=1)
    commission_rate: Decimal | None = Field(default=None, ge=0, le=Decimal("0.1"))
    max_holding_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    max_position_notional: Decimal | None = Field(default=None, gt=0)
    confluence_threshold: Decimal | None = Field(default=None, ge=-1, le=1)
    confluence_weights: dict[str, Decimal] | None = None
    allow_counter_trend: bool | None = None
    daily_loss_limit_pct: Decimal | None = Field(default=None, gt=0, le=Decimal("0.5"))


class StrategyConfigResponse(BaseModel):
    version: int
    risk_per_trade_pct: Decimal
    min_viable_target_multiplier: Decimal
    commission_rate: Decimal
    max_holding_hours: int
    max_position_notional: Decimal
    confluence_threshold: Decimal
    confluence_weights: dict[str, Decimal]
    allow_counter_trend: bool
    daily_loss_limit_pct: Decimal


class SqlConsoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    row_limit: int = Field(default=200, ge=1, le=5000)


class SqlConsoleResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class StorageResponse(BaseModel):
    usage_by_layer: dict[str, int]
    total_bytes: int
    table_sizes: dict[str, int]


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    older_than_days: int = Field(default=180, ge=1, le=3650)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_uid: str | None = None


class AnalyzeResponse(BaseModel):
    results: list[dict[str, Any]]


class OkResponse(BaseModel):
    ok: bool
    detail: str = ""
