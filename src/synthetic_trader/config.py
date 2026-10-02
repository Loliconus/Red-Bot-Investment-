"""Версионируемый контракт эксперимента (Python 3.14 / Pydantic 2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.domain.probability import ProbabilityRiskPolicy

FEATURE_VERSION = "pit-ohlcv-v1"
LABEL_VERSION = "next-open-atr-barriers-v1"


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    trend_threshold: Decimal = Field(default=Decimal("0.60"), gt=0, lt=1)
    direction_threshold: Decimal = Field(default=Decimal("0.58"), gt=Decimal("0.5"), lt=1)
    max_break_probability: Decimal = Field(default=Decimal("0.65"), gt=0, lt=1)
    risk_per_trade: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.05"))
    target_bar_volatility: Decimal = Field(default=Decimal("0.006"), gt=0, le=Decimal("0.1"))
    max_position_weight: Decimal = Field(default=Decimal("0.20"), gt=0, le=Decimal("0.5"))
    max_gross_weight: Decimal = Field(default=Decimal("0.90"), gt=0, le=1)
    max_sector_weight: Decimal = Field(default=Decimal("0.35"), gt=0, le=1)
    max_correlation: Decimal = Field(default=Decimal("0.80"), gt=0, le=1)
    max_daily_drawdown: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.2"))
    commission_bps: Decimal = Field(default=Decimal("10"), ge=0, le=500)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, le=500)
    stop_atr: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    take_atr: Decimal = Field(default=Decimal("3"), gt=0, le=20)
    trailing_atr: Decimal = Field(default=Decimal("2.5"), gt=0, le=20)
    max_positions: int = Field(default=4, ge=1, le=15)
    min_net_reward_risk: Decimal = Field(default=Decimal("1"), ge=1, le=5)

    def policy(self, horizon: int) -> ProbabilityRiskPolicy:
        return ProbabilityRiskPolicy(**self.model_dump(), max_holding_bars=horizon)


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["demo", "moex"] = "demo"
    symbols: tuple[str, ...] = ("SBER", "GAZP", "LKOH")
    start: date = date(2016, 1, 1)
    end: date = date(2026, 9, 30)
    interval: Literal["10m", "1h", "1d"] = "1h"
    horizon: int = Field(default=12, ge=2, le=96)
    atr_period: int = Field(default=14, ge=5, le=60)
    upper_atr: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    lower_atr: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    break_atr: Decimal = Field(default=Decimal("1.5"), gt=0, le=10)
    freeze_months: int = Field(default=6, ge=6, le=12)
    folds: int = Field(default=3, ge=2, le=6)
    cpcv: bool = True
    cpcv_groups: Literal[4, 6] = 4
    embargo_bars: int = Field(default=3, ge=1, le=96)
    layer_b: bool = True
    iterations: int = Field(default=240, ge=40, le=2000)
    depth: int = Field(default=5, ge=4, le=6)
    learning_rate: float = Field(default=0.035, ge=0.01, le=0.05)
    l2_leaf_reg: float = Field(default=8.0, ge=1, le=100)
    early_stopping_rounds: int = Field(default=60, ge=10, le=100)
    max_features: int = Field(default=100, ge=8, le=300)
    stability_threshold: float = Field(default=0.6, ge=0.5, le=1)
    initial_capital: Decimal = Field(default=Decimal("1000000"), ge=10000, le=1000000000)
    risk: RiskConfig = RiskConfig()
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    bootstrap_reps: int = Field(default=500, ge=100, le=5000)
    additional_trials: int = Field(default=0, ge=0, le=1000000)
    l1_auxiliary: bool = False
    mlflow_tracking: bool = False
    dataset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        symbols = tuple(v.strip().upper() for v in values)
        if not 1 <= len(symbols) <= 15 or len(set(symbols)) != len(symbols):
            raise ValueError("Корзина: 1–15 различных тикеров; рекомендуются 10–15 после пилота")
        if "IMOEX" in symbols or any(not re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", s) for s in symbols):
            raise ValueError("IMOEX — только якорь; некорректный тикер")
        return symbols

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.start >= self.end or self.end > datetime.now(UTC).date():
            raise ValueError("История должна быть в прошлом, начало раньше конца")
        if (self.end - self.start).days < 730:
            raise ValueError("Нужно минимум 2 года с отдельными 6–12 месяцами final OOS")
        return self
