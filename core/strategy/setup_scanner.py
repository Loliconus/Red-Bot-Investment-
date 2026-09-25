"""Сканер торговых сетапов.

Работает по готовому ``MarketSnapshot``: все индикаторы к этому моменту уже
посчитаны (API-индикаторы — адаптером, собственные — реестром ``core/analysis``).
Сканер не делает I/O, не знает про время и поэтому детерминирован.

Набор факторов confluence:
1. соответствие рыночного режима ( ``regime_alignment`` );
2. тренд на дневке ( ``trend_d1`` );
3. сетап на часовике ( ``setup_h1`` ): RSI + MACD + Bollinger;
4. уровень Фибоначчи ( ``fibonacci`` );
5. объём ( ``volume`` ): OBV + VWAP;
6. относительная сила против IMOEX ( ``relative_strength`` );
7. стакан ( ``orderbook`` ).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.analysis.confluence_scorer import (
    ConfluenceFactor,
    dominant_negative,
    is_actionable,
    score_confluence,
)
from core.domain.entities import ReasoningStep, StrategyConfig
from core.domain.enums import MarketRegime, Timeframe, Trend
from core.journal.snapshots import MarketSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")

#: Порог RSI, ниже которого на часовике ищем откат (зона перепроданности).
RSI_OVERSOLD = Decimal("40")
#: Порог RSI, выше которого вход в откате не рассматривается.
RSI_OVERBOUGHT = Decimal("70")


@dataclass(frozen=True, slots=True, kw_only=True)
class SetupSignal:
    """Результат сканирования одного инструмента."""

    instrument_uid: str
    factors: tuple[ConfluenceFactor, ...]
    score: Decimal
    bias: dict[Timeframe, Trend]
    summary: str
    actionable: bool
    blocking_reason: str | None

    def factor(self, module: str) -> ConfluenceFactor | None:
        for f in self.factors:
            if f.module == module:
                return f
        return None

    def factors_to_steps(self) -> tuple[ReasoningStep, ...]:
        """Цепочка обоснования для ``TradeThesis`` и ``DecisionSnapshot``."""
        return tuple(f.to_reasoning_step() for f in self.factors)


def _regime_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    regime_d1 = snapshot.market_regime.get(Timeframe.D1)
    if regime_d1 is None:
        return ConfluenceFactor(
            module="regime_alignment",
            signal="no_regime_data",
            score=ZERO,
            weight=config.weight_for("regime_alignment"),
            comment="нет данных о режиме",
        )

    favorable = regime_d1 is MarketRegime.TRENDING
    score = (
        ONE
        if favorable
        else (Decimal("-1") if regime_d1 is MarketRegime.HIGH_VOLATILITY else Decimal("-0.3"))
    )
    return ConfluenceFactor(
        module="regime_alignment",
        signal=f"regime_{regime_d1.value}",
        score=score,
        weight=config.weight_for("regime_alignment"),
        comment=f"режим на дневке: {regime_d1.value}",
    )


def _trend_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    indicators = snapshot.indicators.get(Timeframe.D1, {})
    close = snapshot.ohlcv.get(Timeframe.D1)
    ema = indicators.get("ema")
    sma = indicators.get("sma")

    if close is None or ema is None:
        return ConfluenceFactor(
            module="trend_d1",
            signal="no_trend_data",
            score=ZERO,
            weight=config.weight_for("trend_d1"),
        )

    above_ema = close.close > ema
    score = ONE if above_ema else Decimal("-1")
    comment = f"close {close.close} vs EMA {ema}"
    if sma is not None:
        above_sma = close.close > sma
        score = ONE if (above_ema and above_sma) else (Decimal("-1") if not above_ema else ZERO)
        comment += f" и SMA {sma}"

    return ConfluenceFactor(
        module="trend_d1",
        signal="above_ema" if above_ema else "below_ema",
        score=score,
        weight=config.weight_for("trend_d1"),
        raw_value=ema,
        comment=comment,
    )


def _setup_h1_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    indicators = snapshot.indicators.get(Timeframe.H1, {})
    rsi = indicators.get("rsi")
    macd = indicators.get("macd")
    signal = indicators.get("signal")
    bb_lower = indicators.get("bb_lower")
    close = snapshot.ohlcv.get(Timeframe.H1)

    if rsi is None:
        return ConfluenceFactor(
            module="setup_h1",
            signal="no_setup_data",
            score=ZERO,
            weight=config.weight_for("setup_h1"),
        )

    score = ZERO
    signals: list[str] = []

    if rsi <= RSI_OVERSOLD:
        score += Decimal("0.6")
        signals.append("rsi_oversold")
    elif rsi >= RSI_OVERBOUGHT:
        score -= Decimal("0.6")
        signals.append("rsi_overbought")

    if macd is not None and signal is not None:
        if macd > signal:
            score += Decimal("0.4")
            signals.append("macd_bullish")
        else:
            score -= Decimal("0.4")
            signals.append("macd_bearish")

    if bb_lower is not None and close is not None and close.close <= bb_lower:
        score += Decimal("0.3")
        signals.append("near_lower_band")

    return ConfluenceFactor(
        module="setup_h1",
        signal="_".join(signals) or "neutral",
        score=_clamp(score),
        weight=config.weight_for("setup_h1"),
        raw_value=rsi,
        comment=f"RSI={rsi}",
    )


def _fibonacci_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    raw = snapshot.indicators.get(Timeframe.H1, {})
    distance = raw.get("distance_pct")
    signal_name = snapshot.signal_of("fibonacci", Timeframe.H1)

    if distance is None:
        return ConfluenceFactor(
            module="fibonacci",
            signal="no_fib_data",
            score=ZERO,
            weight=config.weight_for("fibonacci"),
        )

    score = ZERO
    if signal_name == "in_golden_zone":
        score = ONE
    elif signal_name == "near_fib_level":
        score = Decimal("0.5")
    elif signal_name in {"no_fib_confluence", "no_swing"}:
        score = ZERO

    return ConfluenceFactor(
        module="fibonacci",
        signal=signal_name,
        score=score,
        weight=config.weight_for("fibonacci"),
        raw_value=distance,
        comment=f"расстояние до уровня {distance:.4f}",
    )


def _volume_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    raw = snapshot.indicators.get(Timeframe.M1, {})
    obv_slope = raw.get("obv_slope")
    deviation = raw.get("deviation")

    if obv_slope is None:
        return ConfluenceFactor(
            module="volume",
            signal="no_volume_data",
            score=ZERO,
            weight=config.weight_for("volume"),
        )

    score = ZERO
    comment = f"OBV наклон {obv_slope}"
    if obv_slope > ZERO:
        score += Decimal("0.6")
    elif obv_slope < ZERO:
        score -= Decimal("0.6")

    if deviation is not None:
        if deviation > ZERO:
            score += Decimal("0.4")
            comment += ", цена выше VWAP"
        else:
            score -= Decimal("0.4")
            comment += ", цена ниже VWAP"

    return ConfluenceFactor(
        module="volume",
        signal="volume_supportive" if score > ZERO else "volume_against",
        score=_clamp(score),
        weight=config.weight_for("volume"),
        raw_value=obv_slope,
        comment=comment,
    )


def _relative_strength_factor(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
) -> ConfluenceFactor:
    if snapshot.benchmark_snapshot is None:
        return ConfluenceFactor(
            module="relative_strength",
            signal="no_benchmark",
            score=ZERO,
            weight=config.weight_for("relative_strength"),
            comment="бенчмарк IMOEX не загружен",
        )

    raw = snapshot.indicators.get(Timeframe.D1, {})
    rs = raw.get("relative_strength")
    corr = raw.get("correlation")
    if rs is None:
        return ConfluenceFactor(
            module="relative_strength",
            signal="no_rs_data",
            score=ZERO,
            weight=config.weight_for("relative_strength"),
        )

    score = ONE if rs > ZERO else Decimal("-1")
    if corr is not None and corr >= Decimal("0.9") and rs <= ZERO:
        score = Decimal("-1")

    return ConfluenceFactor(
        module="relative_strength",
        signal="outperforming" if rs > ZERO else "lagging",
        score=score,
        weight=config.weight_for("relative_strength"),
        raw_value=rs,
        comment=f"RS vs IMOEX = {rs:.4f}, corr = {corr}",
    )


def _orderbook_factor(snapshot: MarketSnapshot, config: StrategyConfig) -> ConfluenceFactor:
    if snapshot.orderbook is None:
        return ConfluenceFactor(
            module="orderbook",
            signal="no_orderbook",
            score=ZERO,
            weight=config.weight_for("orderbook"),
        )

    raw = snapshot.indicators.get(Timeframe.M1, {})
    imbalance = raw.get("imbalance")
    spread_pct = raw.get("spread_pct")
    if imbalance is None:
        return ConfluenceFactor(
            module="orderbook",
            signal="no_orderbook_data",
            score=ZERO,
            weight=config.weight_for("orderbook"),
        )

    score = imbalance
    if spread_pct is not None and spread_pct > Decimal("0.005"):
        score -= Decimal("0.5")

    return ConfluenceFactor(
        module="orderbook",
        signal="bid_heavy" if imbalance > ZERO else "ask_heavy",
        score=_clamp(score),
        weight=config.weight_for("orderbook"),
        raw_value=imbalance,
        comment=f"дисбаланс {imbalance:.3f}, спред {spread_pct}",
    )


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(ONE, value))


def collect_factors(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
) -> tuple[ConfluenceFactor, ...]:
    """Собирает все факторы confluence по снапшоту."""
    return (
        _regime_factor(snapshot, config),
        _trend_factor(snapshot, config),
        _setup_h1_factor(snapshot, config),
        _fibonacci_factor(snapshot, config),
        _volume_factor(snapshot, config),
        _relative_strength_factor(snapshot, config),
        _orderbook_factor(snapshot, config),
    )


def scan_setup(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
) -> SetupSignal:
    """Сканирует один инструмент: считает confluence и пригодность сетапа.

    Блокирующие условия (при них сетап не actionable даже при высоком скоре):
    * бумага отстаёт от IMOEX — ``allow_counter_trend`` выключен;
    * режим высоковолатильный.
    """
    factors = collect_factors(snapshot, config)
    score = score_confluence(factors, weights=config.confluence_weights)

    bias = {
        Timeframe.D1: _bias_from_factor(factors, "trend_d1"),
        Timeframe.H1: _bias_from_factor(factors, "setup_h1"),
        Timeframe.M1: _bias_from_factor(factors, "volume"),
    }

    blocking: str | None = None
    rs_factor = next((f for f in factors if f.module == "relative_strength"), None)
    if rs_factor is not None and rs_factor.score < ZERO and not config.allow_counter_trend:
        blocking = "бумага слабее IMOEX — вход запрещён конфигом"

    regime_factor = next((f for f in factors if f.module == "regime_alignment"), None)
    if (
        blocking is None
        and regime_factor is not None
        and regime_factor.signal == "regime_high_volatility"
    ):
        blocking = "режим высокой волатильности — стопы выбиваются шумом"

    actionable = is_actionable(score, config.confluence_threshold) and blocking is None

    if blocking is None and not is_actionable(score, config.confluence_threshold):
        negative = dominant_negative(factors)
        blocking = f"confluence {score:.3f} ниже порога {config.confluence_threshold:.3f}" + (
            f" (мешает {negative.module}: {negative.signal})" if negative else ""
        )

    return SetupSignal(
        instrument_uid=snapshot.instrument_uid,
        factors=factors,
        score=score,
        bias=bias,
        summary=_summarize(factors, score),
        actionable=actionable,
        blocking_reason=blocking,
    )


def scan_universe(
    snapshots: list[MarketSnapshot],
    config: StrategyConfig,
) -> list[SetupSignal]:
    """Сканирует всю корзину и сортирует по убыванию скора."""
    signals = [scan_setup(s, config) for s in snapshots]
    return sorted(signals, key=lambda s: s.score, reverse=True)


def _bias_from_factor(factors: tuple[ConfluenceFactor, ...], module: str) -> Trend:
    for factor in factors:
        if factor.module == module:
            if factor.score > ZERO:
                return Trend.UP
            if factor.score < ZERO:
                return Trend.DOWN
            return Trend.FLAT
    return Trend.FLAT


def _summarize(factors: tuple[ConfluenceFactor, ...], score: Decimal) -> str:
    positive = [f.signal for f in factors if f.score > ZERO]
    negative = [f.signal for f in factors if f.score < ZERO]
    return (
        f"confluence {score:.3f}; за: {', '.join(positive) or 'нет'}; "
        f"против: {', '.join(negative) or 'нет'}"
    )
