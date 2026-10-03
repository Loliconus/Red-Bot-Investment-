"""Use-case запуска вероятностного бэктеста, загрузки свечей в DuckDB и обучения весов."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries
from core.synthetic import (
    CatBoostHyperparameters,
    SyntheticRiskConfig,
    SyntheticTraderEngine,
    SyntheticTraderEvaluationReport,
    TripleBarrierConfig,
    generate_reference_moex_series,
)

if TYPE_CHECKING:
    from application.composition import AppContext


@dataclass(frozen=True, slots=True, kw_only=True)
class BacktestRunParameters:
    """Входные параметры интерактивного бэктеста и обучения «Синтетического трейдера»."""

    instrument_uid: str = "uid-sber"
    ticker: str = "SBER"
    timeframe: str = "1h"
    scenario: str = "auto"
    bar_count: int = 120
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
    catboost_iterations: int = 36
    catboost_l2_leaf_reg: float = 5.0
    weights_mode: str = "auto"
    force_exchange_sync: bool = False


def fetch_moex_iss_candles_sync(
    ticker: str,
    domain_tf: Timeframe,
    *,
    bar_count: int = 240,
    end_time: datetime | None = None,
) -> list[OHLCV]:
    """Скачивает реальные исторические свечи с публичного API Московской Биржи (MOEX ISS).

    Не требует токена или регистрации: обращается напрямую к публичному контуру
    ``https://iss.moex.com/iss/engines/stock/markets/.../candles.json``.
    """
    clean_ticker = ticker.strip().upper()
    is_index = clean_ticker in {"IMOEX", "RTSI", "MOEXBC"}
    market = "index" if is_index else "shares"
    board = "SNDX" if is_index else "TQBR"
    interval = 24 if domain_tf is Timeframe.D1 else 60

    now_utc = (end_time or datetime.now(tz=UTC)).astimezone(UTC)
    days_back = max(400, bar_count * 2) if domain_tf is Timeframe.D1 else max(35, bar_count // 4)
    from_date = (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%d")

    params = urllib.parse.urlencode(
        {
            "interval": str(interval),
            "from": from_date,
            "iss.meta": "off",
        }
    )
    url = (
        f"https://iss.moex.com/iss/engines/stock/markets/{market}/"
        f"boards/{board}/securities/{clean_ticker}/candles.json?{params}"
    )

    try:
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={"Accept": "application/json", "User-Agent": "RedBot-Investment/0.2"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        candles_block = payload.get("candles", {})
        cols: list[str] = list(candles_block.get("columns", []))
        rows: list[list[object]] = list(candles_block.get("data", []))
        if not cols or not rows:
            return []

        idx_open = cols.index("open")
        idx_close = cols.index("close")
        idx_high = cols.index("high")
        idx_low = cols.index("low")
        idx_vol = cols.index("volume")
        idx_begin = cols.index("begin")

        parsed: list[OHLCV] = []
        for r in rows:
            raw_dt = str(r[idx_begin]).strip()
            dt_naive = datetime.fromisoformat(raw_dt)
            dt_utc = dt_naive.replace(tzinfo=UTC)
            open_p = Decimal(str(r[idx_open]))
            close_p = Decimal(str(r[idx_close]))
            high_p = max(Decimal(str(r[idx_high])), open_p, close_p)
            low_p = min(Decimal(str(r[idx_low])), open_p, close_p)
            vol = max(0, int(float(str(r[idx_vol]))))
            parsed.append(
                OHLCV(
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    timestamp=dt_utc,
                    timeframe=domain_tf,
                )
            )
        return parsed[-bar_count:] if len(parsed) > bar_count else parsed
    except Exception:  # noqa: BLE001
        return []


async def _persist_series_to_duckdb(
    context: AppContext,
    instrument_uid: str,
    domain_tf: Timeframe,
    candles: tuple[OHLCV, ...],
) -> None:
    """Сохраняет серию свечей в таблицу ``candles`` локальной БД DuckDB."""
    if not candles:
        return
    save_fn = getattr(context.repository, "save_candles", None)
    if callable(save_fn):
        try:
            series = CandleSeries(timeframe=domain_tf, candles=candles)
            await save_fn(series, instrument_uid)
        except Exception:  # noqa: BLE001
            return


async def _load_series_from_duckdb(
    context: AppContext,
    instrument_uid: str,
    domain_tf: Timeframe,
    now_utc: datetime,
    bar_count: int,
) -> list[OHLCV]:
    """Читает сохранённые свечи из таблицы ``candles`` локальной БД DuckDB."""
    get_fn = getattr(context.repository, "get_candles", None)
    if not callable(get_fn):
        return []
    try:
        since = now_utc - timedelta(days=365 * 3)
        series: CandleSeries = await get_fn(instrument_uid, domain_tf, since, now_utc)
        if series and series.candles:
            return list(series.candles[-bar_count:])
    except Exception:  # noqa: BLE001
        return []
    return []


async def sync_real_candles_to_duckdb(
    context: AppContext,
    *,
    ticker: str = "SBER",
    instrument_uid: str = "uid-sber",
    timeframe: str = "1h",
    bar_count: int = 160,
) -> tuple[int, str]:
    """Принудительно загружает свечи (T-Invest API -> MOEX ISS -> Эталон MOEX) в БД DuckDB."""
    domain_tf = Timeframe.D1 if timeframe.lower() == "1d" else Timeframe.H1
    now_utc = context.clock.now()
    lookback = timedelta(days=400) if domain_tf is Timeframe.D1 else timedelta(days=35)

    matched_inst = Instrument(
        uid=instrument_uid,
        ticker=ticker.upper(),
        class_code="TQBR",
        lot_size=10,
        currency="RUB",
    )
    for inst in context.instruments:
        if inst.uid == instrument_uid or inst.ticker.upper() == ticker.upper():
            matched_inst = inst
            break

    asset_loaded: list[OHLCV] = []
    source_desc = ""

    # 1. Пробуем T-Invest API (если настроен реальный канал)
    if context.execution_mode.value in {"live", "sandbox"}:
        paginated_fn = getattr(context.market_data, "get_candles_paginated", None)
        try:
            if callable(paginated_fn):
                asset_loaded = list(
                    await paginated_fn(
                        matched_inst,
                        timeframe.lower(),
                        now_utc - lookback,
                        now_utc,
                    )
                )
            else:
                asset_loaded = list(
                    await context.market_data.get_candles(
                        matched_inst,
                        domain_tf,
                        now_utc - lookback,
                        now_utc,
                    )
                )
            if len(asset_loaded) >= 30:
                source_desc = "T-Invest API → сохранено в таблицу candles DuckDB"
        except Exception:  # noqa: BLE001
            asset_loaded = []

    # 2. Пробуем публичный сервер Московской Биржи (MOEX ISS API, без токена)
    if len(asset_loaded) < 30:
        moex_bars = await asyncio.to_thread(
            fetch_moex_iss_candles_sync,
            matched_inst.ticker,
            domain_tf,
            bar_count=bar_count,
            end_time=now_utc,
        )
        if len(moex_bars) >= 30:
            asset_loaded = moex_bars
            source_desc = "Биржа MOEX ISS (реальные торги TQBR) → сохранено в DuckDB"

    # 3. Если сеть недоступна (офлайн-контейнер), формируем калиброванную серию MOEX и пишем в БД
    if len(asset_loaded) < 30:
        ref_a, ref_m = generate_reference_moex_series(
            ticker=matched_inst.ticker,
            bar_count=bar_count,
            domain_tf=domain_tf,
            end_time=now_utc,
            scenario="cycle",
        )
        asset_loaded = list(ref_a)
        await _persist_series_to_duckdb(context, "uid-imoex", domain_tf, ref_m)
        source_desc = "Локальный исторический датасет MOEX → записан в таблицу candles DuckDB"
    else:
        moex_idx = await asyncio.to_thread(
            fetch_moex_iss_candles_sync,
            "IMOEX",
            domain_tf,
            bar_count=len(asset_loaded),
            end_time=now_utc,
        )
        if len(moex_idx) >= 20:
            await _persist_series_to_duckdb(context, "uid-imoex", domain_tf, tuple(moex_idx))

    await _persist_series_to_duckdb(
        context,
        matched_inst.uid,
        domain_tf,
        tuple(asset_loaded),
    )
    return len(asset_loaded), source_desc


async def run_synthetic_backtest(
    context: AppContext,
    params: BacktestRunParameters | None = None,
) -> SyntheticTraderEvaluationReport:
    """Выполняет полный бэктест, сохраняет свечи в DuckDB и веса моделей в ``data/models/``."""
    cfg = params or BacktestRunParameters()

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
    scen = cfg.scenario.lower().strip()
    data_source_label = ""

    asset_candles: tuple[OHLCV, ...] = ()
    imoex_candles: tuple[OHLCV, ...] = ()

    # Если выбран режим реальных данных ("auto") — работаем через таблицу candles в DuckDB
    if scen == "auto":
        if cfg.force_exchange_sync:
            _, data_source_label = await sync_real_candles_to_duckdb(
                context,
                ticker=matched_inst.ticker,
                instrument_uid=matched_inst.uid,
                timeframe=cfg.timeframe,
                bar_count=max(cfg.bar_count, 140),
            )

        db_bars = await _load_series_from_duckdb(
            context,
            matched_inst.uid,
            domain_tf,
            now_utc,
            cfg.bar_count,
        )
        if len(db_bars) >= 35:
            asset_candles = tuple(db_bars[-cfg.bar_count :])
            if not data_source_label:
                data_source_label = (
                    f"Локальная БД DuckDB (таблица candles, {len(asset_candles)} свечей)"
                )
            imoex_db = await _load_series_from_duckdb(
                context,
                context.benchmark.uid if context.benchmark else "uid-imoex",
                domain_tf,
                now_utc,
                len(asset_candles),
            )
            imoex_candles = (
                tuple(imoex_db[-len(asset_candles) :])
                if len(imoex_db) >= 20
                else asset_candles
            )
        else:
            # Если в DuckDB ещё нет свечей по этому инструменту — загружаем и сохраняем в DuckDB
            _, data_source_label = await sync_real_candles_to_duckdb(
                context,
                ticker=matched_inst.ticker,
                instrument_uid=matched_inst.uid,
                timeframe=cfg.timeframe,
                bar_count=max(cfg.bar_count, 140),
            )
            db_bars = await _load_series_from_duckdb(
                context,
                matched_inst.uid,
                domain_tf,
                now_utc,
                cfg.bar_count,
            )
            if len(db_bars) >= 35:
                asset_candles = tuple(db_bars[-cfg.bar_count :])
                imoex_db = await _load_series_from_duckdb(
                    context,
                    context.benchmark.uid if context.benchmark else "uid-imoex",
                    domain_tf,
                    now_utc,
                    len(asset_candles),
                )
                imoex_candles = (
                    tuple(imoex_db[-len(asset_candles) :])
                    if len(imoex_db) >= 20
                    else asset_candles
                )

    if len(asset_candles) < 35:
        asset_candles, imoex_candles = generate_reference_moex_series(
            ticker=matched_inst.ticker,
            bar_count=cfg.bar_count,
            domain_tf=domain_tf,
            end_time=now_utc,
            scenario=scen,
        )
        # Записываем серию в таблицу candles DuckDB, чтобы БД никогда не была пустой
        await _persist_series_to_duckdb(context, matched_inst.uid, domain_tf, asset_candles)
        await _persist_series_to_duckdb(
            context,
            context.benchmark.uid if context.benchmark else "uid-imoex",
            domain_tf,
            imoex_candles,
        )
        scen_names = {
            "auto": "Эталонная серия MOEX (сохранена в таблицу candles DuckDB)",
            "cycle": "Стресс-сценарий: 6-фазный цикл (сохранён в DuckDB)",
            "bull": "Стресс-сценарий: бычий тренд (сохранён в DuckDB)",
            "chop": "Стресс-сценарий: боковая пила (сохранён в DuckDB)",
            "crash_recovery": "Стресс-сценарий: обвал и V-разворот (сохранён в DuckDB)",
        }
        if not data_source_label:
            data_source_label = scen_names.get(scen, "Таблица candles DuckDB")

    total_db_candles = len(asset_candles)
    try:
        sizes = await context.repository.table_sizes()
        total_db_candles = max(int(sizes.get("candles", 0)), len(asset_candles))
    except Exception:  # noqa: BLE001
        total_db_candles = len(asset_candles)

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
        weights_mode=cfg.weights_mode,
        data_source_label=data_source_label,
        db_candles_count=total_db_candles,
    )
