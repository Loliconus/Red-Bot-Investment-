"""DTO — плоские структуры для обмена между слоями и GUI.

Ядро эти типы не знает: они живут в ``application`` и используются адаптерами
(FastAPI-схемы формируются отдельно, в ``adapters/driving/web/schemas.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.domain.enums import (
    DecisionType,
    HypothesisStatus,
    MarketRegime,
    TradePlanStatus,
    TradeVerdict,
)
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.journal.trade_review import TradeReview
from core.risk.cost_model import CostFilterResult
from core.risk.position_sizing import SizingResult


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketSnapshotView:
    """Снапшот в «плоском» виде для GUI."""

    id: UUID
    instrument_uid: str
    captured_at: datetime
    last_price: Decimal | None
    regime: dict[str, MarketRegime]
    indicators: dict[str, dict[str, Decimal]]
    signals: dict[str, dict[str, str]]
    orderbook_imbalance: Decimal | None

    @classmethod
    def from_domain(cls, snapshot: MarketSnapshot) -> MarketSnapshotView:
        return cls(
            id=snapshot.id,
            instrument_uid=snapshot.instrument_uid,
            captured_at=snapshot.captured_at,
            last_price=snapshot.last_price(),
            regime={tf.value: r for tf, r in snapshot.market_regime.items()},
            indicators={tf.value: dict(v) for tf, v in snapshot.indicators.items()},
            signals={tf.value: dict(v) for tf, v in snapshot.signals.items()},
            orderbook_imbalance=snapshot.orderbook.imbalance if snapshot.orderbook else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionView:
    id: UUID
    decision: DecisionType
    confluence_score: Decimal
    reasoning: tuple[tuple[str, str, Decimal], ...]  # (module, signal, weight)
    risk_check_passed: bool
    risk_check_reason: str | None
    thought_text: str
    created_at: datetime
    plan_id: UUID | None

    @classmethod
    def from_domain(cls, snapshot: DecisionSnapshot) -> DecisionView:
        return cls(
            id=snapshot.id,
            decision=snapshot.decision,
            confluence_score=snapshot.confluence_score,
            reasoning=tuple(
                (step.module, step.signal, step.weight) for step in snapshot.reasoning_chain
            ),
            risk_check_passed=snapshot.risk_check_passed,
            risk_check_reason=snapshot.risk_check_reason,
            thought_text=snapshot.thought_text,
            created_at=snapshot.created_at,
            plan_id=snapshot.trade_plan_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlanView:
    id: UUID
    instrument_uid: str
    ticker: str
    status: TradePlanStatus
    entry_price: Decimal
    hard_stop_price: Decimal
    target_price: Decimal
    risk_reward: Decimal
    confluence_score: Decimal
    created_at: datetime
    expires_at: datetime
    quantity_lots: int
    rejection_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeReviewView:
    trade_plan_id: UUID
    verdict: TradeVerdict
    entry_price: Decimal
    exit_price: Decimal
    mfe: Decimal
    mae: Decimal
    exit_efficiency: Decimal
    post_exit_drift_pct: Decimal
    realized_pnl: Decimal
    closed_at: datetime

    @classmethod
    def from_domain(cls, review: TradeReview) -> TradeReviewView:
        return cls(
            trade_plan_id=review.trade_plan_id,
            verdict=review.verdict,
            entry_price=review.entry_price,
            exit_price=review.exit_price,
            mfe=review.mfe,
            mae=review.mae,
            exit_efficiency=review.exit_efficiency,
            post_exit_drift_pct=review.post_exit_drift_pct,
            realized_pnl=review.realized_pnl,
            closed_at=review.closed_at,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class HypothesisView:
    id: UUID
    text: str
    suggested_action: str
    status: HypothesisStatus
    confidence: Decimal
    sample_size: int
    walk_forward_efficiency: Decimal | None
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskCheckSummary:
    """Сводка риск-проверок для GUI и лога."""

    passed: bool
    reasons: tuple[str, ...]
    cost_filter: CostFilterResult | None = None
    sizing: SizingResult | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyReport:
    date: datetime
    trades_closed: int
    win_rate: Decimal
    total_pnl: Decimal
    best_trade: Decimal
    worst_trade: Decimal
    avg_exit_efficiency: Decimal
    advice: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthStatus:
    execution_mode: str
    started_at: datetime
    uptime_seconds: int
    kill_switch_engaged: bool
    open_plans: int
    open_positions: int
    storage_usage_bytes: int
    streams_alive: bool
