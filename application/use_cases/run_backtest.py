"""Use-case запуска вероятностного бэктеста и валидации «Синтетический трейдер»."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV
from core.synthetic import (
    CatBoostHyperparameters,
    SyntheticRiskConfig,
    SyntheticTraderEngine,
    SyntheticTraderEvaluationReport,
    TripleBarrierConfig,
)

if TYPE_CHECKING:
    from application.composition import AppContext


@dataclass(frozen=True, slots=True, kw_only=True)
class BacktestRunParameters:
    """Входные параметры интерактивного бэктеста «Синтетического трейдера»."""

    instrument_uid: str = "uid-sber"
    ticker: str = "SBER"
    timeframe: str = "1h"
    bar_count: int = 96
    initial_capital: Decimal = Decimal("1000000")
    commission_bps: Decimal = Decimal("5.0")
    slippage_bps: Decimal = Decimal("5.0")
    horizon_bars: int = 8
    k_tp: Decimal = Decimal("1.8")
    k_sl: Decimal = Decimal("1.2")
    trend_threshold_a: Decimal = Decimal("0.52")
    direction_threshold_b: Decimal = Decimal("0.53")
    break_exit_threshold: Decimal = Decimal("0.72")
    target_volatility_pct: Decimal = Decimal("0.015")
    catboost_depth: int = 4
    catboost_iterations: int = 32
    catboost_l2_leaf_reg: float = 6.0


def generate_reference_moex_series(
    *,
    ticker: str = "SBER",
    bar_count: int = 96,
    base_price: Decimal = Decimal("280.00"),
    domain_tf: Timeframe = Timeframe.H1,
    end_time: datetime | None = None,
) -> tuple[tuple[OHLCV, ...], tuple[OHLCV, ...]]:
    """Генерирует детерминированную многорежимную серию свечей актива и индекса IMOEX.

    Используется в автономном контуре (или при пустом локальном кэше свечей), чтобы
    интерактивный бэктест в GUI и CLI всегда имел полноценный ряд с фазами:
    1) направленного тренда (TREND),
    2) боковой пилы (CHOP),
    3) волатильного импульса и разворота (PANIC / BREAK),
    4) восстановления.
    """
    n = max(40, min(bar_count, 500))
    anchor_end = (end_time or datetime(2026, 10, 1, 18, 0, tzinfo=UTC)).astimezone(UTC)
    step = timedelta(days=1) if domain_tf is Timeframe.D1 else timedelta(hours=1)
    start_ts = anchor_end - step * n

    seed_shift = sum(ord(ch) for ch in ticker.upper()) % 17
    asset_bars: list[OHLCV] = []
    imoex_bars: list[OHLCV] = []

    p_asset = float(base_price)
    p_imoex = 3100.0

    for i in range(n):
        ts = start_ts + step * i
        phase = i / n
        if phase < 0.35:
            # Фаза устойчивого восходящего тренда
            drift_a = 0.0045 + 0.0015 * math.sin((i + seed_shift) * 0.35)
            drift_m = 0.0032 + 0.0010 * math.sin(i * 0.35)
            vol_mult = 1.0
        elif phase < 0.60:
            # Фаза боковика («пилы»)
            drift_a = 0.0022 * math.sin((i + seed_shift) * 1.3)
            drift_m = 0.0018 * math.cos(i * 1.2)
            vol_mult = 0.75
        elif phase < 0.75:
            # Фаза повышенной волатильности / коррекции
            drift_a = -0.0050 + 0.0035 * math.cos((i + seed_shift) * 0.8)
            drift_m = -0.0040 + 0.0025 * math.cos(i * 0.8)
            vol_mult = 1.9
        else:
            # Финальный импульсный тренд (Frozen Holdout)
            drift_a = 0.0052 + 0.0012 * math.sin((i + seed_shift) * 0.4)
            drift_m = 0.0038 + 0.0010 * math.sin(i * 0.4)
            vol_mult = 1.1

        open_a = p_asset
        close_a = max(10.0, open_a * (1.0 + drift_a))
        wick_a = open_a * 0.0045 * vol_mult
        high_a = max(open_a, close_a) + wick_a
        low_a = max(1.0, min(open_a, close_a) - wick_a * 0.85)
        vol_a = int(15_000 * vol_mult + (i % 9) * 2_100 + seed_shift * 300)

        open_m = p_imoex
        close_m = max(500.0, open_m * (1.0 + drift_m))
        wick_m = open_m * 0.0035 * vol_mult
        high_m = max(open_m, close_m) + wick_m
        low_m = max(100.0, min(open_m, close_m) - wick_m * 0.85)
        vol_m = int(95_000 * vol_mult + (i % 11) * 5_000)

        asset_bars.append(
            OHLCV(
                open=Decimal(f"{open_a:.2f}"),
                high=Decimal(f"{high_a:.2f}"),
                low=Decimal(f"{low_a:.2f}"),
                close=Decimal(f"{close_a:.2f}"),
                volume=vol_a,
                timestamp=ts,
                timeframe=domain_tf,
            )
        )
        imoex_bars.append(
            OHLCV(
                open=Decimal(f"{open_m:.2f}"),
                high=Decimal(f"{high_m:.2f}"),
                low=Decimal(f"{low_m:.2f}"),
                close=Decimal(f"{close_m:.2f}"),
                volume=vol_m,
                timestamp=ts,
                timeframe=domain_tf,
            )
        )
        p_asset = close_a
        p_imoex = close_m

    return tuple(asset_bars), tuple(imoex_bars)


async def run_synthetic_backtest(
    context: AppContext,
    params: BacktestRunParameters | None = None,
) -> SyntheticTraderEvaluationReport:
    """Выполняет полный бэктест и статистическую валидацию «Синтетического трейдера»."""
    cfg = params or BacktestRunParameters()

    # Определяем инструмент из корзины контекста или создаём доменный объект
    matched_inst: Instrument | None = None
    for inst in context.instruments:
        if inst.uid == cfg.instrument_uid or inst.ticker.upper() == cfg.ticker.upper():
            matched_inst = inst
            break

    if matched_inst is None:
        matched_inst = Instrument(
            uid=cfg.instrument_uid,
            ticker=cfg.ticker.upper(),
            class_code="TQBR",
            lot_size=10,
            currency="RUB",
        )

    domain_tf = Timeframe.D1 if cfg.timeframe.lower() == "1d" else Timeframe.H1
    now_utc = context.clock.now()
    lookback = timedelta(days=365) if domain_tf is Timeframe.D1 else timedelta(days=30)

    try:
        loaded_candles = await context.market_data.get_candles(
            matched_inst,
            domain_tf,
            now_utc - lookback,
            now_utc,
        )
    except Exception:  # noqa: BLE001
        loaded_candles = []

    if len(loaded_candles) >= 35:
        asset_candles: tuple[OHLCV, ...] = tuple(loaded_candles[-cfg.bar_count :])
        try:
            imoex_loaded = await context.market_data.get_candles(
                context.benchmark,
                domain_tf,
                now_utc - lookback,
                now_utc,
            )
        except Exception:  # noqa: BLE001
            imoex_loaded = []
        imoex_candles: tuple[OHLCV, ...] = (
            tuple(imoex_loaded[-len(asset_candles) :]) if len(imoex_loaded) >= 10 else asset_candles
        )
    else:
        asset_candles, imoex_candles = generate_reference_moex_series(
            ticker=matched_inst.ticker,
            bar_count=cfg.bar_count,
            domain_tf=domain_tf,
            end_time=now_utc,
        )

    barrier_cfg = TripleBarrierConfig(
        horizon_bars=max(2, cfg.horizon_bars),
        k_tp=cfg.k_tp,
        k_sl=cfg.k_sl,
    )
    cb_params = CatBoostHyperparameters(
        depth=max(3, min(cfg.catboost_depth, 6)),
        iterations=max(10, min(cfg.catboost_iterations, 120)),
        l2_leaf_reg=max(1.0, cfg.catboost_l2_leaf_reg),
    )
    risk_cfg = SyntheticRiskConfig(
        trend_threshold_a=cfg.trend_threshold_a,
        direction_threshold_b=cfg.direction_threshold_b,
        break_exit_threshold=cfg.break_exit_threshold,
        target_volatility_pct=cfg.target_volatility_pct,
    )

    engine = SyntheticTraderEngine(
        barrier_config=barrier_cfg,
        catboost_params=cb_params,
        risk_config=risk_cfg,
        enable_advanced_stages=True,
    )

    comm_rate = cfg.commission_bps / Decimal("10000")
    slip_rate = cfg.slippage_bps / Decimal("10000")

    return engine.run_full_evaluation(
        asset_candles,
        instrument_uid=matched_inst.uid,
        ticker=matched_inst.ticker,
        class_code=matched_inst.class_code,
        timeframe=cfg.timeframe,
        lot_size=matched_inst.lot_size,
        initial_capital=cfg.initial_capital,
        commission_rate=comm_rate,
        slippage_rate=slip_rate,
        imoex_candles=imoex_candles,
    )
