"""Стратегический слой: режим → сетап → тайминг → план."""

from core.strategy.entry_timing import EntryTiming, evaluate_entry_timing
from core.strategy.regime_detector import RegimeState, detect_regime, normalized_slope
from core.strategy.setup_scanner import SetupSignal, scan_setup, scan_universe
from core.strategy.trade_plan_builder import PlanBuildResult, build_trade_plan

__all__ = [
    "EntryTiming",
    "PlanBuildResult",
    "RegimeState",
    "SetupSignal",
    "build_trade_plan",
    "detect_regime",
    "evaluate_entry_timing",
    "normalized_slope",
    "scan_setup",
    "scan_universe",
]
