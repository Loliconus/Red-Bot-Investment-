"""Доменные сущности с идентичностью и жизненным циклом.

Соглашения:
* сущности — ``slots=True``, но **без** ``frozen`` (состояние меняется);
* валидация и вычисляемые поля — только в ``__post_init__``, без I/O;
* ``*_id`` — ``UUID``, генерируемый в момент создания сущности в ``core``,
  а не в БД через AUTOINCREMENT: идентификатор должен существовать и быть
  стабилен ещё до первой записи в репозиторий.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from core.domain.enums import OrderStatus, Timeframe, TradePlanStatus, Trend
from core.domain.value_objects import ONE, ZERO

if TYPE_CHECKING:  # только для аннотаций — ядро не импортирует адаптеры в рантайме
    from core.journal.snapshots import MarketSnapshot

MIN_HOLDING_TIME = timedelta(minutes=1)


@dataclass(slots=True, kw_only=True)
class Instrument:
    """Торгуемый или эталонный инструмент."""

    uid: str  # instrument_uid из API — первичный ключ везде
    ticker: str
    lot_size: int
    class_code: str = "TQBR"
    is_benchmark: bool = False  # True только для IMOEX
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            msg = f"lot_size должен быть положительным, получен {self.lot_size}"
            raise ValueError(msg)
        if not self.uid:
            msg = "instrument_uid не может быть пустым"
            raise ValueError(msg)

    def notional(self, price: Decimal, lots: int) -> Decimal:
        """Стоимость позиции. Цена в API — за одну бумагу, поэтому умножаем на лот."""
        return price * Decimal(self.lot_size) * Decimal(lots)

    def units_for_lots(self, lots: int) -> int:
        return lots * self.lot_size


@dataclass(slots=True, kw_only=True)
class ReasoningStep:
    """Вклад одного модуля анализа в итоговое решение."""

    module: str  # "regime_detector", "fibonacci", "orderbook_analysis"
    signal: str  # "trend_up", "near_618_retracement", "bid_heavy"
    weight: Decimal  # вклад в confluence_score, может быть отрицательным
    raw_value: Decimal | None = None
    comment: str = ""


@dataclass(slots=True, kw_only=True)
class TradeThesis:
    """Формализованная гипотеза входа."""

    reasoning_chain: tuple[ReasoningStep, ...]
    confluence_score: Decimal
    timeframe_bias: dict[Timeframe, Trend]
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.reasoning_chain:
            msg = "Тезис без цепочки обоснования не имеет смысла"
            raise ValueError(msg)

    @property
    def total_weight(self) -> Decimal:
        return sum((step.weight for step in self.reasoning_chain), ZERO)


@dataclass(slots=True, kw_only=True)
class InvalidationRule:
    """Условие смерти гипотезы — НЕ по цене.

    ``check`` возвращает True, если тезис инвалидирован.
    """

    description: str  # человекочитаемое условие для GUI
    check: Callable[[MarketSnapshot], bool]
    code: str = "custom"  # машинный код правила для дата-майнинга


@dataclass(slots=True, kw_only=True)
class TradePlan:
    """Гипотеза сделки — не эфемерный сигнал, а сущность с жизненным циклом.

    Именно к ``TradePlan`` привязываются MFE/MAE при последующем разборе.
    """

    id: UUID
    instrument: Instrument
    entry_price: Decimal
    hard_stop_price: Decimal
    target_price: Decimal
    thesis: TradeThesis
    thesis_invalidation: InvalidationRule
    max_holding_time: timedelta
    created_at: datetime
    status: TradePlanStatus = TradePlanStatus.PROPOSED
    quantity_lots: int = 0
    closed_at: datetime | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.hard_stop_price >= self.entry_price:
            msg = (
                f"hard stop ({self.hard_stop_price}) обязан быть ниже входа "
                f"({self.entry_price}) для long-only стратегии"
            )
            raise ValueError(msg)
        if self.target_price <= self.entry_price:
            msg = f"target ({self.target_price}) обязан быть выше входа ({self.entry_price})"
            raise ValueError(msg)
        if self.max_holding_time < MIN_HOLDING_TIME:
            msg = f"max_holding_time слишком мал: {self.max_holding_time}"
            raise ValueError(msg)

    @classmethod
    def create(
        cls,
        *,
        instrument: Instrument,
        entry_price: Decimal,
        hard_stop_price: Decimal,
        target_price: Decimal,
        thesis: TradeThesis,
        thesis_invalidation: InvalidationRule,
        max_holding_time: timedelta,
        created_at: datetime,
        quantity_lots: int = 0,
    ) -> TradePlan:
        """Фабрика: UUID создаётся в ядре, до персистентности."""
        return cls(
            id=uuid4(),
            instrument=instrument,
            entry_price=entry_price,
            hard_stop_price=hard_stop_price,
            target_price=target_price,
            thesis=thesis,
            thesis_invalidation=thesis_invalidation,
            max_holding_time=max_holding_time,
            created_at=created_at,
            quantity_lots=quantity_lots,
        )

    @property
    def risk_per_unit(self) -> Decimal:
        return self.entry_price - self.hard_stop_price

    @property
    def reward_per_unit(self) -> Decimal:
        return self.target_price - self.entry_price

    @property
    def risk_reward_ratio(self) -> Decimal:
        if self.risk_per_unit == ZERO:
            return ZERO
        return self.reward_per_unit / self.risk_per_unit

    @property
    def expected_return_pct(self) -> Decimal:
        if self.entry_price == ZERO:
            return ZERO
        return self.reward_per_unit / self.entry_price

    @property
    def risk_pct(self) -> Decimal:
        if self.entry_price == ZERO:
            return ZERO
        return self.risk_per_unit / self.entry_price

    @property
    def is_open(self) -> bool:
        return self.status in {
            TradePlanStatus.PROPOSED,
            TradePlanStatus.PENDING,
            TradePlanStatus.ACTIVE,
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            TradePlanStatus.CLOSED_TARGET,
            TradePlanStatus.CLOSED_HARD_STOP,
            TradePlanStatus.CLOSED_INVALIDATION,
            TradePlanStatus.CLOSED_TIME_EXIT,
            TradePlanStatus.CLOSED_MANUAL,
            TradePlanStatus.REJECTED,
            TradePlanStatus.CANCELLED,
        }

    def expires_at(self) -> datetime:
        return self.created_at + self.max_holding_time

    def activate(self) -> None:
        if self.status is not TradePlanStatus.PENDING:
            msg = f"Активировать можно только PENDING-план, текущий: {self.status}"
            raise ValueError(msg)
        self.status = TradePlanStatus.ACTIVE

    def mark_pending(self) -> None:
        if self.status is not TradePlanStatus.PROPOSED:
            msg = f"В PENDING можно перевести только PROPOSED-план, текущий: {self.status}"
            raise ValueError(msg)
        self.status = TradePlanStatus.PENDING

    def reject(self, reason: str, *, closed_at: datetime) -> None:
        self.status = TradePlanStatus.REJECTED
        self.rejection_reason = reason
        self.closed_at = closed_at

    def close(self, status: TradePlanStatus, *, closed_at: datetime) -> None:
        if status not in {
            TradePlanStatus.CLOSED_TARGET,
            TradePlanStatus.CLOSED_HARD_STOP,
            TradePlanStatus.CLOSED_INVALIDATION,
            TradePlanStatus.CLOSED_TIME_EXIT,
            TradePlanStatus.CLOSED_MANUAL,
        }:
            msg = f"Недопустимый терминальный статус: {status}"
            raise ValueError(msg)
        self.status = status
        self.closed_at = closed_at


@dataclass(slots=True, kw_only=True)
class Position:
    """Открытая long-позиция."""

    instrument: Instrument
    quantity: int  # в штуках, не лотах
    average_entry: Decimal
    opened_at: datetime
    linked_plan_id: UUID

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            msg = f"Количество должно быть положительным, получено {self.quantity}"
            raise ValueError(msg)

    @property
    def lots(self) -> int:
        return self.quantity // self.instrument.lot_size

    def market_value(self, price: Decimal) -> Decimal:
        return price * Decimal(self.quantity)

    def unrealized_pnl_pct(self, price: Decimal) -> Decimal:
        if self.average_entry == ZERO:
            return ZERO
        return (price - self.average_entry) / self.average_entry

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        return (price - self.average_entry) * Decimal(self.quantity)


@dataclass(slots=True, kw_only=True)
class OrderResult:
    """Результат выставления ордера (возвращается адаптером исполнения)."""

    order_id: str  # биржевой идентификатор
    client_order_id: str  # ключ идемпотентности, который мы отправили
    status: OrderStatus
    filled_lots: int = 0
    filled_price: Decimal | None = None
    message: str = ""
    raw_response: str = ""


@dataclass(slots=True, kw_only=True)
class OrderState:
    order_id: str
    status: OrderStatus
    filled_lots: int = 0
    filled_price: Decimal | None = None
    message: str = ""


@dataclass(slots=True, kw_only=True)
class StrategyConfig:
    """Версионируемый операционный конфиг стратегии (живёт в БД, правится из GUI)."""

    version: int
    risk_per_trade_pct: Decimal
    min_viable_target_multiplier: Decimal
    commission_rate: Decimal
    max_holding_hours: int
    max_position_notional: Decimal
    confluence_threshold: Decimal
    confluence_weights: dict[str, Decimal] = field(default_factory=dict)
    allow_counter_trend: bool = False
    daily_loss_limit_pct: Decimal = Decimal("0.03")

    def __post_init__(self) -> None:
        if self.version < 1:
            msg = f"Версия конфига обязана быть >= 1, получена {self.version}"
            raise ValueError(msg)

    def weight_for(self, module: str) -> Decimal:
        return self.confluence_weights.get(module, ONE)


@dataclass(slots=True, kw_only=True)
class PortfolioState:
    """Перезаписываемый снимок состояния счёта."""

    account_id: str
    total_value: Decimal
    available_cash: Decimal
    positions_value: Decimal
    updated_at: datetime
    daily_pnl: Decimal = ZERO

    @property
    def risk_budget(self) -> Decimal:
        """Бюджет риска на одну сделку при 1% депозита."""
        return self.total_value * Decimal("0.01")
