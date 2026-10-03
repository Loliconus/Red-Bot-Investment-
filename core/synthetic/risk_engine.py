"""Слой риск-менеджмента «Синтетического трейдера» (раздел 6 ТЗ).

Модель не принимает торговых решений напрямую — её выходы ``P(trend)``, ``P(up|trend)``
и ``P(break within H)`` проходят через жёсткий детерминированный слой правил:
1. Вход разрешён только при ``P(trend) > a`` (в боковике не торгуем).
2. Направление определяется ``P(up | trend) >= b`` (Long) или ``<= 1 - b`` (Short).
3. Размер позиции масштабируется обратно пропорционально волатильности (volatility targeting)
   и уменьшается пропорционально ``(1 - P(break within H))``.
4. Четыре внешних защитных контура:
   - лимит дневной просадки по портфелю и аварийный Kill Switch;
   - контроль корреляции между открытыми позициями (запрет одновременного набора
     односекторной макро-ставки вроде ``GAZP + TATN + ROSN`` при ``corr > max_corr``);
   - запрет торговли при обнаружении HMM опасного режима ``PANIC``;
   - динамический ATR-трейлинг-стоп и досрочный выход при скачке ``P(break within H)``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from enum import StrEnum

from core.synthetic.catboost_model import ProbabilisticTriadPrediction
from core.synthetic.hmm_regime import HMMRegimePosterior, SyntheticMarketRegime


class SyntheticDirection(StrEnum):
    """Направление позиции вероятностного движка «Синтетический трейдер»."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"

#: Кластеры высококоррелированных бумаг МосБиржи для защиты от скрытой концентрации.
MOEX_SECTOR_CLUSTERS: dict[str, str] = {
    "GAZP": "OIL_GAS",
    "ROSN": "OIL_GAS",
    "TATN": "OIL_GAS",
    "LKOH": "OIL_GAS",
    "NVTK": "OIL_GAS",
    "SIBN": "OIL_GAS",
    "SNGS": "OIL_GAS",
    "SBER": "FINANCE",
    "VTBR": "FINANCE",
    "TCSG": "FINANCE",
    "T": "FINANCE",
    "MOEX": "FINANCE",
    "GMKN": "METALS",
    "NLMK": "METALS",
    "CHMF": "METALS",
    "MAGN": "METALS",
    "PLZL": "METALS",
    "RUAL": "METALS",
    "YDEX": "TECH",
    "OZON": "TECH",
    "POSI": "TECH",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticRiskConfig:
    """Параметры риск-слоя «Синтетического трейдера» (раздел 6 ТЗ)."""

    trend_threshold_a: Decimal = Decimal("0.55")
    direction_threshold_b: Decimal = Decimal("0.55")
    break_exit_threshold: Decimal = Decimal("0.68")
    target_volatility_pct: Decimal = Decimal("0.012")
    base_position_fraction: Decimal = Decimal("0.15")
    max_position_fraction: Decimal = Decimal("0.25")
    max_daily_drawdown_pct: Decimal = Decimal("0.025")
    max_pairwise_correlation: Decimal = Decimal("0.72")
    max_positions_per_sector: int = 1
    trailing_atr_multiple: Decimal = Decimal("2.0")
    stop_loss_atr_multiple: Decimal = Decimal("1.4")
    take_profit_atr_multiple: Decimal = Decimal("2.4")
    allow_short: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticPositionDecision:
    """Решение риск-движка по открытию/удержанию/закрытию позиции (все суммы в Decimal)."""

    direction: SyntheticDirection
    allow_entry: bool
    should_exit_existing: bool
    recommended_lots: int
    position_fraction: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    trailing_stop_price: Decimal
    volatility_scalar: Decimal
    break_penalty_scalar: Decimal
    blocked_reason: str | None = None


def compute_return_correlation(
    closes_a: Sequence[Decimal],
    closes_b: Sequence[Decimal],
    *,
    window: int = 20,
) -> Decimal:
    """Вычисляет выборочную корреляцию лог-доходностей двух инструментов в ``Decimal``."""
    m = min(len(closes_a), len(closes_b), window + 1)
    if m < 5:
        return Decimal("0")
    ra = [
        math.log(max(float(closes_a[-m + i + 1]), 1e-9) / max(float(closes_a[-m + i]), 1e-9))
        for i in range(m - 1)
    ]
    rb = [
        math.log(max(float(closes_b[-m + i + 1]), 1e-9) / max(float(closes_b[-m + i]), 1e-9))
        for i in range(m - 1)
    ]
    k = len(ra)
    ma = sum(ra) / k
    mb = sum(rb) / k
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(k))
    va = sum((ra[i] - ma) ** 2 for i in range(k))
    vb = sum((rb[i] - mb) ** 2 for i in range(k))
    denom = math.sqrt(va * vb)
    corr = cov / denom if denom > 1e-12 else 0.0
    return Decimal(f"{corr:.4f}")


def evaluate_probabilistic_risk_gate(
    *,
    ticker: str,
    current_price: Decimal,
    lot_size: int,
    atr: Decimal,
    realized_volatility_pct: Decimal,
    portfolio_equity: Decimal,
    daily_drawdown_pct: Decimal,
    triad: ProbabilisticTriadPrediction,
    hmm_state: HMMRegimePosterior,
    active_tickers: Sequence[str] = (),
    pairwise_correlations: Mapping[str, Decimal] | None = None,
    existing_position_direction: SyntheticDirection = SyntheticDirection.HOLD,
    existing_extreme_price: Decimal | None = None,
    config: SyntheticRiskConfig | None = None,
) -> SyntheticPositionDecision:
    """Применяет все правила раздела 6 ТЗ для расчёта размера позиции и защитных стопов."""
    cfg = config or SyntheticRiskConfig()
    p_trend = Decimal(f"{triad.p_trend:.6f}")
    p_up = Decimal(f"{triad.p_up_given_trend:.6f}")
    p_break = Decimal(f"{triad.p_break_within_h:.6f}")

    eff_atr = max(atr, current_price * Decimal("0.002"))
    trailing_dist = cfg.trailing_atr_multiple * eff_atr

    # Вычисляем уровень трейлинг-стопа для уже открытой позиции
    anchor_extreme = existing_extreme_price if existing_extreme_price is not None else current_price
    if existing_position_direction is SyntheticDirection.SELL:
        trail_stop = anchor_extreme + trailing_dist
    else:
        trail_stop = max(Decimal("0.01"), anchor_extreme - trailing_dist)

    # Проверяем условия экстренного выхода из открытой позиции (раздел 6.2)
    should_exit = False
    exit_reason: str | None = None
    if existing_position_direction is not SyntheticDirection.HOLD:
        if daily_drawdown_pct >= cfg.max_daily_drawdown_pct:
            should_exit = True
            exit_reason = "KILL_SWITCH_DAILY_DRAWDOWN"
        elif (
            hmm_state.is_trading_banned
            or hmm_state.dominant_regime is SyntheticMarketRegime.PANIC
        ):
            should_exit = True
            exit_reason = "HMM_PANIC_REGIME_EXIT"
        elif p_break >= cfg.break_exit_threshold:
            should_exit = True
            exit_reason = "HIGH_BREAK_PROBABILITY_EXIT"
        elif existing_position_direction is SyntheticDirection.BUY and (
            current_price <= trail_stop or p_up < Decimal("0.38")
        ):
            should_exit = True
            exit_reason = "TRAILING_OR_REVERSAL_EXIT"
        elif existing_position_direction is SyntheticDirection.SELL and (
            current_price >= trail_stop or p_up > Decimal("0.62")
        ):
            should_exit = True
            exit_reason = "TRAILING_OR_REVERSAL_EXIT"

    # 1. Защитный контур 1: лимит дневной просадки и Kill Switch
    if daily_drawdown_pct >= cfg.max_daily_drawdown_pct:
        return SyntheticPositionDecision(
            direction=SyntheticDirection.HOLD,
            allow_entry=False,
            should_exit_existing=should_exit,
            recommended_lots=0,
            position_fraction=Decimal("0"),
            stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
            take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
            trailing_stop_price=trail_stop,
            volatility_scalar=Decimal("0"),
            break_penalty_scalar=Decimal("0"),
            blocked_reason="KILL_SWITCH_DAILY_DRAWDOWN",
        )

    # 2. Защитный контур 3: запрет торговли при обнаружении HMM опасного режима PANIC
    if hmm_state.is_trading_banned or hmm_state.dominant_regime is SyntheticMarketRegime.PANIC:
        return SyntheticPositionDecision(
            direction=SyntheticDirection.HOLD,
            allow_entry=False,
            should_exit_existing=should_exit,
            recommended_lots=0,
            position_fraction=Decimal("0"),
            stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
            take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
            trailing_stop_price=trail_stop,
            volatility_scalar=Decimal("0"),
            break_penalty_scalar=Decimal("0"),
            blocked_reason="HMM_PANIC_REGIME_BAN",
        )

    # 3. Защитный контур 2: контроль корреляции и отраслевой концентрации (напр. GAZP+TATN+ROSN)
    upper_ticker = ticker.upper()
    target_sector = MOEX_SECTOR_CLUSTERS.get(upper_ticker)
    if target_sector is not None:
        same_sector_active = [
            t
            for t in active_tickers
            if t.upper() != upper_ticker and MOEX_SECTOR_CLUSTERS.get(t.upper()) == target_sector
        ]
        if len(same_sector_active) >= cfg.max_positions_per_sector:
            return SyntheticPositionDecision(
                direction=SyntheticDirection.HOLD,
                allow_entry=False,
                should_exit_existing=should_exit,
                recommended_lots=0,
                position_fraction=Decimal("0"),
                stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
                take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
                trailing_stop_price=trail_stop,
                volatility_scalar=Decimal("0"),
                break_penalty_scalar=Decimal("0"),
                blocked_reason=(
                    f"SECTOR_CONCENTRATION_LIMIT:{target_sector}({same_sector_active[0]})"
                ),
            )

    if pairwise_correlations:
        for peer, corr_val in pairwise_correlations.items():
            if peer.upper() != upper_ticker and corr_val >= cfg.max_pairwise_correlation:
                return SyntheticPositionDecision(
                    direction=SyntheticDirection.HOLD,
                    allow_entry=False,
                    should_exit_existing=should_exit,
                    recommended_lots=0,
                    position_fraction=Decimal("0"),
                    stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
                    take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
                    trailing_stop_price=trail_stop,
                    volatility_scalar=Decimal("0"),
                    break_penalty_scalar=Decimal("0"),
                    blocked_reason=f"CORRELATION_LIMIT_EXCEEDED:{peer}({corr_val:.2f})",
                )

    # 4. Правило входа 6.1: торгуем только когда P(trend) > a
    if p_trend <= cfg.trend_threshold_a:
        return SyntheticPositionDecision(
            direction=SyntheticDirection.HOLD,
            allow_entry=False,
            should_exit_existing=should_exit,
            recommended_lots=0,
            position_fraction=Decimal("0"),
            stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
            take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
            trailing_stop_price=trail_stop,
            volatility_scalar=Decimal("1"),
            break_penalty_scalar=Decimal("1") - p_break,
            blocked_reason=exit_reason or "SIDEWAYS_NOISE_P_TREND_BELOW_A",
        )

    # 5. Направление сделки по P(up | trend)
    direction = SyntheticDirection.HOLD
    if p_up >= cfg.direction_threshold_b:
        direction = SyntheticDirection.BUY
    elif cfg.allow_short and p_up <= (Decimal("1") - cfg.direction_threshold_b):
        direction = SyntheticDirection.SELL

    if direction is SyntheticDirection.HOLD:
        return SyntheticPositionDecision(
            direction=SyntheticDirection.HOLD,
            allow_entry=False,
            should_exit_existing=should_exit,
            recommended_lots=0,
            position_fraction=Decimal("0"),
            stop_loss_price=current_price - cfg.stop_loss_atr_multiple * eff_atr,
            take_profit_price=current_price + cfg.take_profit_atr_multiple * eff_atr,
            trailing_stop_price=trail_stop,
            volatility_scalar=Decimal("1"),
            break_penalty_scalar=Decimal("1") - p_break,
            blocked_reason="DIRECTIONAL_CONVICTION_BELOW_B",
        )

    # 6. Масштабирование позиции по P(break within H) и волатильности (volatility targeting)
    eff_vol = max(realized_volatility_pct, Decimal("0.003"))
    vol_scalar = min(
        Decimal("2.0"),
        max(Decimal("0.20"), cfg.target_volatility_pct / eff_vol),
    )
    break_scalar = max(Decimal("0.05"), Decimal("1") - p_break)
    trend_excess = (p_trend - cfg.trend_threshold_a) / max(
        Decimal("0.05"), Decimal("1") - cfg.trend_threshold_a
    )
    dir_strength = abs(Decimal("2") * p_up - Decimal("1"))

    # Интегральный множитель убеждённости в [0.25, 1.25]
    conviction_mult = min(
        Decimal("1.25"),
        max(Decimal("0.25"), Decimal("0.50") + Decimal("0.50") * trend_excess + dir_strength),
    )

    raw_fraction = cfg.base_position_fraction * vol_scalar * break_scalar * conviction_mult
    capped_fraction = min(cfg.max_position_fraction, max(Decimal("0"), raw_fraction))

    lot_cost = current_price * Decimal(max(lot_size, 1))
    alloc_rub = portfolio_equity * capped_fraction
    lots = int((alloc_rub / lot_cost).to_integral_value(rounding=ROUND_DOWN)) if lot_cost > 0 else 0

    if direction is SyntheticDirection.BUY:
        sl_price = max(Decimal("0.01"), current_price - cfg.stop_loss_atr_multiple * eff_atr)
        tp_price = current_price + cfg.take_profit_atr_multiple * eff_atr
    else:
        sl_price = current_price + cfg.stop_loss_atr_multiple * eff_atr
        tp_price = max(Decimal("0.01"), current_price - cfg.take_profit_atr_multiple * eff_atr)

    allow_entry = lots >= 1 and p_break < cfg.break_exit_threshold
    return SyntheticPositionDecision(
        direction=direction if allow_entry else SyntheticDirection.HOLD,
        allow_entry=allow_entry,
        should_exit_existing=should_exit,
        recommended_lots=lots if allow_entry else 0,
        position_fraction=capped_fraction.quantize(Decimal("0.0001")),
        stop_loss_price=sl_price.quantize(Decimal("0.0001")),
        take_profit_price=tp_price.quantize(Decimal("0.0001")),
        trailing_stop_price=trail_stop.quantize(Decimal("0.0001")),
        volatility_scalar=vol_scalar.quantize(Decimal("0.0001")),
        break_penalty_scalar=break_scalar.quantize(Decimal("0.0001")),
        blocked_reason=None if allow_entry else "INSUFFICIENT_LOTS_OR_HIGH_BREAK_RISK",
    )
