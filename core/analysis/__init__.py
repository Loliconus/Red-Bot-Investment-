"""Аналитический слой ядра: индикаторы, confluence, корреляция, стакан."""

from core.analysis.atr import ATRIndicator, atr_pct, stop_distance, true_range, wilder_atr
from core.analysis.confluence_scorer import (
    ConfluenceFactor,
    build_thesis,
    dominant_negative,
    is_actionable,
    score_confluence,
)
from core.analysis.fibonacci import (
    FibonacciIndicator,
    fibonacci_levels,
    find_swing,
    is_near_level,
    nearest_fib_level,
)
from core.analysis.market_correlation import (
    MarketCorrelationIndicator,
    correlation,
    is_market_driven,
    price_returns,
    relative_strength,
)
from core.analysis.orderbook_analysis import (
    OrderbookIndicator,
    find_walls,
    near_walls,
    orderbook_penalty,
)
from core.analysis.protocols import Indicator, IndicatorResult
from core.analysis.registry import IndicatorRegistry, build_default_registry
from core.analysis.volume_indicators import (
    OBVIndicator,
    VWAPIndicator,
    anchored_vwap,
    obv_slope,
    on_balance_volume,
    price_vs_vwap,
    volume_trend,
    vwap,
)

__all__ = [
    "ATRIndicator",
    "ConfluenceFactor",
    "FibonacciIndicator",
    "Indicator",
    "IndicatorRegistry",
    "IndicatorResult",
    "MarketCorrelationIndicator",
    "OBVIndicator",
    "OrderbookIndicator",
    "VWAPIndicator",
    "anchored_vwap",
    "atr_pct",
    "build_default_registry",
    "build_thesis",
    "correlation",
    "dominant_negative",
    "fibonacci_levels",
    "find_swing",
    "find_walls",
    "is_actionable",
    "is_market_driven",
    "is_near_level",
    "near_walls",
    "nearest_fib_level",
    "obv_slope",
    "on_balance_volume",
    "orderbook_penalty",
    "price_returns",
    "price_vs_vwap",
    "relative_strength",
    "score_confluence",
    "stop_distance",
    "true_range",
    "volume_trend",
    "vwap",
    "wilder_atr",
]
