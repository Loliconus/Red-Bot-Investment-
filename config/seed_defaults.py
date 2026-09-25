"""Значения для первичного заполнения операционного конфига.

Используются **только** при первом запуске на пустой БД юзкейсом
``application/use_cases/bootstrap_database.py``. Дальнейшая жизнь этих
параметров происходит в БД (таблица ``strategy_configs``) через GUI.
"""

from __future__ import annotations

from decimal import Decimal

from core.domain.enums import Timeframe

DEFAULT_INSTRUMENTS: tuple[dict[str, object], ...] = (
    {"ticker": "SBER", "class_code": "TQBR", "lot_size": 10, "is_benchmark": False},
    {"ticker": "GAZP", "class_code": "TQBR", "lot_size": 10, "is_benchmark": False},
    {"ticker": "IMOEX", "class_code": "SPBFUT", "lot_size": 1, "is_benchmark": True},
)

#: Параметры индикаторов раздельно по таймфрейму ( seeded-версия 1 ).
DEFAULT_TA_PARAMS: dict[Timeframe, dict[str, dict[str, object]]] = {
    Timeframe.D1: {
        "rsi": {"period": 14},
        "ema": {"period": 50},
        "sma": {"period": 200},
        "atr": {"period": 14},
    },
    Timeframe.H1: {
        "rsi": {"period": 9},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "bollinger": {"period": 20, "deviation": 2.0},
        "atr": {"period": 14},
    },
    Timeframe.M1: {
        "vwap": {},
        "obv": {},
        "atr": {"period": 14},
    },
}

#: Веса confluence-скоринга по умолчанию. Меняются из GUI, не хардкодятся в core.
DEFAULT_CONFLUENCE_WEIGHTS: dict[str, Decimal] = {
    "regime_alignment": Decimal("0.25"),
    "trend_d1": Decimal("0.20"),
    "setup_h1": Decimal("0.20"),
    "fibonacci": Decimal("0.10"),
    "volume": Decimal("0.10"),
    "relative_strength": Decimal("0.10"),
    "orderbook": Decimal("0.05"),
}

DEFAULT_TIMEFRAME_BIAS: dict[Timeframe, str] = {
    Timeframe.D1: "flat",
    Timeframe.H1: "flat",
    Timeframe.M1: "flat",
}
