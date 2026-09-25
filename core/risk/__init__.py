"""Риск-модуль: издержки, сайзинг и три обязательных механизма выхода."""

from core.risk.cost_model import (
    CostEstimate,
    CostFilterResult,
    estimate_costs,
    is_target_viable,
    min_viable_target_pct,
    round_trip_commission_pct,
    viability_margin,
)
from core.risk.hard_stop import (
    StopCheckResult,
    check_hard_stop,
    distance_in_atr,
    hard_stop_price,
    is_hard_stop_triggered,
    structural_stop_from_series,
    trailing_stop_price,
)
from core.risk.position_sizing import (
    SizingResult,
    calculate_position_size,
    risk_per_unit,
    size_from_budget,
)
from core.risk.thesis_invalidation import (
    NoOpInvalidation,
    build_default_rules,
    combine_rules,
    confluence_score_rule,
    explain,
    find_invalidated,
    market_driven_rule,
    relative_strength_rule,
    trend_break_rule,
)
from core.risk.time_exit import (
    TimeExitResult,
    check_time_exit,
    held_for,
    is_expired,
    remaining_time,
)

__all__ = [
    "CostEstimate",
    "CostFilterResult",
    "NoOpInvalidation",
    "SizingResult",
    "StopCheckResult",
    "TimeExitResult",
    "build_default_rules",
    "calculate_position_size",
    "check_hard_stop",
    "check_time_exit",
    "combine_rules",
    "confluence_score_rule",
    "distance_in_atr",
    "estimate_costs",
    "explain",
    "find_invalidated",
    "hard_stop_price",
    "held_for",
    "is_expired",
    "is_hard_stop_triggered",
    "is_target_viable",
    "market_driven_rule",
    "min_viable_target_pct",
    "relative_strength_rule",
    "remaining_time",
    "risk_per_unit",
    "round_trip_commission_pct",
    "size_from_budget",
    "structural_stop_from_series",
    "trailing_stop_price",
    "trend_break_rule",
    "viability_margin",
]
