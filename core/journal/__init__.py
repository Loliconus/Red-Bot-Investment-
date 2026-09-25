"""Журнал самоанализа: снапшоты, разбор сделок, гипотезы, советы."""

from core.journal.advisory import Advice, build_advice, has_actionable
from core.journal.hypothesis_engine import (
    DEFAULT_MIN_SAMPLE_SIZE,
    DEFAULT_WALK_FORWARD_THRESHOLD,
    OVERFIT_THRESHOLD,
    Hypothesis,
    propose_efficiency_hypotheses,
    walk_forward_efficiency,
    walk_forward_split,
)
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot, Thought
from core.journal.trade_review import (
    TradeExcursion,
    TradeReview,
    classify_verdict,
    compute_excursion,
    compute_exit_efficiency,
    compute_post_exit_drift,
)

__all__ = [
    "DEFAULT_MIN_SAMPLE_SIZE",
    "DEFAULT_WALK_FORWARD_THRESHOLD",
    "OVERFIT_THRESHOLD",
    "Advice",
    "DecisionSnapshot",
    "Hypothesis",
    "MarketSnapshot",
    "Thought",
    "TradeExcursion",
    "TradeReview",
    "build_advice",
    "classify_verdict",
    "compute_excursion",
    "compute_exit_efficiency",
    "compute_post_exit_drift",
    "has_actionable",
    "propose_efficiency_hypotheses",
    "walk_forward_efficiency",
    "walk_forward_split",
]
