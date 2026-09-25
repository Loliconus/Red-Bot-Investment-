"""Снапшоты: event sourcing для дата-майнинга.

Каждый цикл анализа — даже «ничего не делать» — фиксируется связкой
``MarketSnapshot`` (всё, что бот видел) + ``DecisionSnapshot`` (что решено
и почему). Именно это превращает журнал «мыслей» в структурированные данные,
пригодные для SQL-запросов по паттернам, а не просто текстовый лог.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from core.domain.entities import ReasoningStep
from core.domain.enums import DecisionType, MarketRegime, Timeframe
from core.domain.value_objects import OHLCV, CandleSeries, OrderbookSnapshot


@dataclass(slots=True, kw_only=True)
class MarketSnapshot:
    """Полное состояние рынка в момент анализа.

    Неизменяемый снимок: всё, что бот видел, сохраняется целиком, чтобы позже
    можно было ответить на вопрос «почему он решил именно так».
    """

    id: UUID
    instrument_uid: str
    captured_at: datetime
    ohlcv: dict[Timeframe, OHLCV]
    candles: dict[Timeframe, CandleSeries] = field(default_factory=dict)
    indicators: dict[Timeframe, dict[str, Decimal]] = field(default_factory=dict)
    #: Строковые сигналы модулей: ``{"h1": {"fibonacci": "in_golden_zone"}}``.
    signals: dict[Timeframe, dict[str, str]] = field(default_factory=dict)
    orderbook: OrderbookSnapshot | None = None
    imoex_correlation: Decimal | None = None
    market_regime: dict[Timeframe, MarketRegime] = field(default_factory=dict)
    benchmark_snapshot: MarketSnapshot | None = None

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None:
            msg = "captured_at обязан быть tz-aware (UTC)"
            raise ValueError(msg)

    @classmethod
    def create(
        cls,
        *,
        instrument_uid: str,
        captured_at: datetime,
        ohlcv: dict[Timeframe, OHLCV] | None = None,
        **kwargs: object,
    ) -> MarketSnapshot:
        """Фабрика: UUID создаётся в ядре, до персистентности."""
        return cls(
            id=uuid4(),
            instrument_uid=instrument_uid,
            captured_at=captured_at,
            ohlcv=ohlcv or {},
            **kwargs,  # type: ignore[arg-type]
        )

    def indicator(self, timeframe: Timeframe, name: str) -> Decimal | None:
        return self.indicators.get(timeframe, {}).get(name)

    def signal_of(self, module: str, timeframe: Timeframe | None = None) -> str:
        """Строковый сигнал модуля. Если таймфрейм не указан, ищет по всем."""
        if timeframe is not None:
            return self.signals.get(timeframe, {}).get(module, "")
        for mapping in self.signals.values():
            if module in mapping:
                return mapping[module]
        return ""

    def set_signal(self, module: str, value: str, *, timeframe: Timeframe) -> None:
        self.signals.setdefault(timeframe, {})[module] = value

    def regime(self, timeframe: Timeframe) -> MarketRegime | None:
        return self.market_regime.get(timeframe)

    def last_price(self, timeframe: Timeframe = Timeframe.M1) -> Decimal | None:
        candle = self.ohlcv.get(timeframe)
        return candle.close if candle else None


@dataclass(slots=True, kw_only=True)
class DecisionSnapshot:
    """Принятое решение с полной цепочкой обоснования."""

    id: UUID
    market_snapshot_id: UUID
    trade_plan_id: UUID | None
    decision: DecisionType
    reasoning_chain: tuple[ReasoningStep, ...]
    confluence_score: Decimal
    risk_check_passed: bool
    risk_check_reason: str | None
    thought_text: str  # человекочитаемая мысль для GUI
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        market_snapshot_id: UUID,
        decision: DecisionType,
        reasoning_chain: tuple[ReasoningStep, ...],
        confluence_score: Decimal,
        risk_check_passed: bool,
        thought_text: str,
        created_at: datetime,
        trade_plan_id: UUID | None = None,
        risk_check_reason: str | None = None,
    ) -> DecisionSnapshot:
        return cls(
            id=uuid4(),
            market_snapshot_id=market_snapshot_id,
            trade_plan_id=trade_plan_id,
            decision=decision,
            reasoning_chain=reasoning_chain,
            confluence_score=confluence_score,
            risk_check_passed=risk_check_passed,
            risk_check_reason=risk_check_reason,
            thought_text=thought_text,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Thought:
    """Человекочитаемая «мысль» бота для ленты в GUI."""

    text: str
    module: str
    created_at: datetime
    confluence_score: Decimal | None = None
