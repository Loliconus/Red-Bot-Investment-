"""Роутер экрана «Синтетический трейдер · Бэктест и Валидация» (`/backtest`)."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, templates
from application.use_cases.run_backtest import (
    BacktestRunParameters,
    run_synthetic_backtest,
)
from core.synthetic.engine import SyntheticTraderEvaluationReport

router = APIRouter(tags=["gui-backtest"], dependencies=[Depends(require_session)])


def _pad_or_trim_series(
    values: list[Decimal],
    target_len: int,
    initial_capital: Decimal,
) -> list[float]:
    if not values or initial_capital <= Decimal("0"):
        return [0.0] * target_len
    pcts = [float((v - initial_capital) / initial_capital * Decimal("100")) for v in values]
    if len(pcts) >= target_len:
        return [round(x, 3) for x in pcts[-target_len:]]
    pad_len = target_len - len(pcts)
    padded = pcts + [pcts[-1]] * pad_len
    return [round(x, 3) for x in padded]


def _svg_polyline_points(
    xs: list[float],
    ys: list[float],
) -> str:
    return " ".join(f"{xs[i]:.1f},{ys[i]:.1f}" for i in range(min(len(xs), len(ys))))


def _build_svg_arena_bundle(
    steps: list[Any],
    competing_series: list[dict[str, Any]],
    train_split_idx: int,
    thr_a_pct: float,
    thr_b_pct: float,
    thr_break_pct: float,
    nn_train_loss: list[float],
    nn_val_loss: list[float],
) -> dict[str, Any]:
    """Строит готовые серверные SVG-координаты для 4 графиков (работают даже без JS/CSP)."""
    n = max(len(steps), 2)
    pad_l = 58.0
    pad_r = 24.0
    w_total = 1000.0
    plot_w = w_total - pad_l - pad_r

    xs = [
        pad_l + (i / max(n - 1, 1)) * plot_w
        for i in range(len(steps))
    ]
    split_clamped = min(max(train_split_idx, 1), max(len(steps) - 1, 1))
    split_x = round(pad_l + (split_clamped / max(n - 1, 1)) * plot_w, 1)

    # 1. График соревнования алгоритмов (Equity Curves, height=310)
    h1 = 310.0
    p1_top = 24.0
    p1_bot = 28.0
    p1_h = h1 - p1_top - p1_bot

    all_eq_vals = [0.0]
    for s in competing_series:
        all_eq_vals.extend(float(v) for v in s["values"])
    eq_min = min(all_eq_vals)
    eq_max = max(all_eq_vals)
    eq_span = max(eq_max - eq_min, 1.5)
    eq_min -= eq_span * 0.08
    eq_max += eq_span * 0.10
    eq_span = max(eq_max - eq_min, 1.0)

    def _y_eq(val: float) -> float:
        return p1_top + (1.0 - (val - eq_min) / eq_span) * p1_h

    eq_grid: list[dict[str, Any]] = []
    for g in range(6):
        val = eq_max - (g / 5.0) * eq_span
        eq_grid.append(
            {
                "y": round(_y_eq(val), 1),
                "label": f"{'+' if val > 0 else ''}{val:.1f}%",
            }
        )
    zero_y = round(_y_eq(0.0), 1)

    svg_series: list[dict[str, Any]] = []
    bot_ys: list[float] = []
    for idx, s in enumerate(competing_series):
        vals: list[float] = [float(v) for v in s["values"]]
        ys = [_y_eq(v) for v in vals]
        if idx == 0:
            bot_ys = ys
        pts = _svg_polyline_points(xs, ys)
        area_pts = (
            f"{xs[0]:.1f},{zero_y:.1f} {pts} {xs[-1]:.1f},{zero_y:.1f}"
            if xs and ys
            else ""
        )
        svg_series.append(
            {
                "id": f"algo-{idx}",
                "name": s["name"],
                "color": s["color"],
                "width": s["width"],
                "dash": "6,4" if s.get("dash") else "",
                "is_primary": idx == 0,
                "points": pts,
                "area_points": area_pts,
                "final_val": f"{'+' if vals[-1] >= 0 else ''}{vals[-1]:.2f}%" if vals else "0%",
                "final_y": round(ys[-1], 1) if ys else zero_y,
            }
        )

    eq_markers: list[dict[str, Any]] = []
    for i, st in enumerate(steps):
        if st.signal_action in {"BUY", "EXIT"} and i < len(xs) and i < len(bot_ys):
            eq_markers.append(
                {
                    "x": round(xs[i], 1),
                    "y": round(bot_ys[i], 1),
                    "action": st.signal_action,
                    "color": "#22c55e" if st.signal_action == "BUY" else "#c084fc",
                    "label": st.signal_label,
                }
            )

    # 2. График цены, тренда L1, фаз HMM и сделок (height=290)
    h2 = 290.0
    p2_top = 24.0
    p2_bot = 30.0
    p2_h = h2 - p2_top - p2_bot

    prices = [float(st.close_price) for st in steps]
    l1_vals = [float(st.l1_trend_price) for st in steps]
    p_all = (prices + l1_vals) if prices else [100.0]
    p_min = min(p_all)
    p_max = max(p_all)
    p_span = max(p_max - p_min, 1.0)
    p_min -= p_span * 0.10
    p_max += p_span * 0.12
    p_span = max(p_max - p_min, 1.0)

    def _y_price(val: float) -> float:
        return p2_top + (1.0 - (val - p_min) / p_span) * p2_h

    price_grid: list[dict[str, Any]] = []
    for g in range(5):
        val = p_max - (g / 4.0) * p_span
        price_grid.append({"y": round(_y_price(val), 1), "label": f"{val:.1f} ₽"})

    price_ys = [_y_price(v) for v in prices]
    l1_ys = [_y_price(v) for v in l1_vals]
    price_points = _svg_polyline_points(xs, price_ys)
    l1_points = _svg_polyline_points(xs, l1_ys)

    col_w = round(plot_w / max(len(steps), 1) + 0.8, 2)
    regime_bands: list[dict[str, Any]] = []
    holding_bars: list[dict[str, Any]] = []
    trade_pins: list[dict[str, Any]] = []

    for i, st in enumerate(steps):
        rx = round(xs[i] - col_w / 2.0, 1)
        if st.hmm_regime == "panic":
            r_fill = "rgba(239, 68, 68, 0.20)"
        elif st.hmm_regime == "trend":
            r_fill = "rgba(16, 185, 129, 0.10)"
        else:
            r_fill = "rgba(245, 158, 11, 0.08)"
        regime_bands.append({"x": rx, "w": col_w, "fill": r_fill})

        pos_pct = float(st.position_fraction)
        if pos_pct > 0.01:
            holding_bars.append({"x": rx, "w": col_w})

        if st.signal_action == "BUY":
            px = round(xs[i], 1)
            py = round(price_ys[i], 1)
            tri = f"{px:.1f},{py + 4:.1f} {px - 7:.1f},{py + 18:.1f} {px + 7:.1f},{py + 18:.1f}"
            trade_pins.append(
                {
                    "x": px,
                    "y": py,
                    "tri": tri,
                    "label_y": round(min(py + 31.0, h2 - 8.0), 1),
                    "color": "#22c55e",
                    "text": f"▲ КУПИЛ {float(st.close_price):.1f}₽",
                }
            )
        elif st.signal_action == "EXIT":
            px = round(xs[i], 1)
            py = round(price_ys[i], 1)
            tri = f"{px:.1f},{py - 4:.1f} {px - 7:.1f},{py - 18:.1f} {px + 7:.1f},{py - 18:.1f}"
            is_loss = "-" in (st.signal_label or "")
            trade_pins.append(
                {
                    "x": px,
                    "y": py,
                    "tri": tri,
                    "label_y": round(max(py - 23.0, 14.0), 1),
                    "color": "#ef4444" if is_loss else "#c084fc",
                    "text": f"▼ {st.signal_label}",
                }
            )

    # 3. График вероятностей 3 голов (height=210)
    h3 = 210.0
    p3_top = 18.0
    p3_bot = 26.0
    p3_h = h3 - p3_top - p3_bot

    def _y_prob(pct_val: float) -> float:
        return p3_top + (1.0 - min(max(pct_val, 0.0), 100.0) / 100.0) * p3_h

    p_tr_pts = _svg_polyline_points(xs, [_y_prob(st.p_trend * 100.0) for st in steps])
    p_up_pts = _svg_polyline_points(xs, [_y_prob(st.p_up_given_trend * 100.0) for st in steps])
    p_br_pts = _svg_polyline_points(xs, [_y_prob(st.p_break_within_h * 100.0) for st in steps])

    # Подписи оси времени X
    x_ticks: list[dict[str, Any]] = []
    tick_step = max(1, len(steps) // 7)
    for i in range(0, len(steps), tick_step):
        x_ticks.append(
            {
                "x": round(xs[i], 1),
                "label": steps[i].timestamp.strftime("%m-%d %H:%M"),
            }
        )

    # 4. График обучения нейросети по эпохам (Loss Curve, 520x165)
    lw = 520.0
    lh = 165.0
    lp_l = 46.0
    lp_r = 16.0
    lp_t = 16.0
    lp_b = 24.0
    n_ep = max(len(nn_train_loss), 2)
    l_xs = [lp_l + (i / max(n_ep - 1, 1)) * (lw - lp_l - lp_r) for i in range(len(nn_train_loss))]
    l_all = (nn_train_loss + nn_val_loss) if nn_train_loss else [0.693, 0.45]
    l_min = max(0.05, min(l_all) * 0.92)
    l_max = max(l_all) * 1.05
    l_span = max(l_max - l_min, 0.05)

    def _y_loss(v: float) -> float:
        return lp_t + (1.0 - (v - l_min) / l_span) * (lh - lp_t - lp_b)

    tr_loss_pts = _svg_polyline_points(l_xs, [_y_loss(v) for v in nn_train_loss])
    val_loss_pts = _svg_polyline_points(l_xs, [_y_loss(v) for v in nn_val_loss])

    return {
        "split_x": split_x,
        "pad_l": pad_l,
        "w_total": w_total,
        "plot_w": plot_w,
        "x_ticks": x_ticks,
        "equity": {
            "h": h1,
            "grid": eq_grid,
            "zero_y": zero_y,
            "series": svg_series,
            "markers": eq_markers,
        },
        "price": {
            "h": h2,
            "grid": price_grid,
            "price_points": price_points,
            "l1_points": l1_points,
            "regime_bands": regime_bands,
            "holding_bars": holding_bars,
            "trade_pins": trade_pins,
        },
        "prob": {
            "h": h3,
            "grid": [
                {"y": round(_y_prob(100.0), 1), "label": "100%"},
                {"y": round(_y_prob(75.0), 1), "label": "75%"},
                {"y": round(_y_prob(50.0), 1), "label": "50%"},
                {"y": round(_y_prob(25.0), 1), "label": "25%"},
                {"y": round(_y_prob(0.0), 1), "label": "0%"},
            ],
            "thr_entry_y": round(_y_prob(max(thr_a_pct, thr_b_pct)), 1),
            "thr_break_y": round(_y_prob(thr_break_pct), 1),
            "p_trend_points": p_tr_pts,
            "p_up_points": p_up_pts,
            "p_break_points": p_br_pts,
        },
        "loss": {
            "w": lw,
            "h": lh,
            "train_points": tr_loss_pts,
            "val_points": val_loss_pts,
            "y_top_label": f"{l_max:.3f}",
            "y_mid_label": f"{(l_max + l_min) / 2.0:.3f}",
            "y_bot_label": f"{l_min:.3f}",
            "epochs": len(nn_train_loss),
        },
    }


def _build_chart_payload(
    report: SyntheticTraderEvaluationReport,
    params: BacktestRunParameters,
) -> tuple[str, dict[str, Any]]:
    """Сериализует данные и строит серверные SVG-графики соревнования алгоритмов."""
    steps = list(report.step_records)
    n_steps = len(steps)
    cap = report.initial_capital

    bot_ret_pct = [
        round(float((st.equity - cap) / cap * Decimal("100")), 3) for st in steps
    ]

    palette = {
        "Buy & Hold IMOEX": "#9ca3af",
        "Neural MLP (Adam 3-Layer)": "#06b6d4",
        "LightGBM (Single-Head ML)": "#a855f7",
        "HistGBDT (Single-Head ML)": "#a855f7",
        "ElasticNet (Linear ML)": "#3b82f6",
        "MA Crossover (8/21)": "#eab308",
        "RSI(14) Mean-Reversion": "#10b981",
    }

    competing_series: list[dict[str, Any]] = [
        {
            "name": "Синтетический трейдер (CatBoost 3-Head + HMM)",
            "color": "#ff4d4d",
            "width": 3.2,
            "dash": False,
            "values": bot_ret_pct,
            "total_return_pct": float(report.total_return_pct),
            "max_dd_pct": float(report.max_drawdown_pct),
            "sharpe": round(float(report.oos_sharpe_ratio), 2),
            "trades": report.trades_count,
            "description": (
                "Ансамбль 3 голов CatBoost + 3-слойная нейросеть + фильтр тренда ℓ₁ "
                "+ защита от обвалов HMM"
            ),
        }
    ]

    for bm in report.benchmarks:
        bm_series = _pad_or_trim_series(list(bm.equity_curve), n_steps, cap)
        competing_series.append(
            {
                "name": bm.name,
                "color": palette.get(bm.name, "#60a5fa"),
                "width": 2.1,
                "dash": bm.category == "passive",
                "values": bm_series,
                "total_return_pct": float(bm.total_return_pct),
                "max_dd_pct": float(bm.max_drawdown_pct),
                "sharpe": round(float(bm.sharpe_ratio), 2),
                "trades": bm.trades_count,
                "description": bm.description,
            }
        )

    wm = report.weights_manifest
    nn_tr_loss = list(wm.nn_train_loss_curve) if wm else [0.693, 0.52, 0.44]
    nn_val_loss = list(wm.nn_val_loss_curve) if wm else [0.693, 0.55, 0.48]

    svg_bundle = _build_svg_arena_bundle(
        steps=steps,
        competing_series=competing_series,
        train_split_idx=report.train_split_index,
        thr_a_pct=round(float(params.trend_threshold_a) * 100.0, 1),
        thr_b_pct=round(float(params.direction_threshold_b) * 100.0, 1),
        thr_break_pct=round(float(params.break_exit_threshold) * 100.0, 1),
        nn_train_loss=nn_tr_loss,
        nn_val_loss=nn_val_loss,
    )

    payload = {
        "ticker": report.ticker,
        "timeframe": report.timeframe,
        "train_split_index": report.train_split_index,
        "thresholds": {
            "trend_a": round(float(params.trend_threshold_a) * 100, 1),
            "dir_b": round(float(params.direction_threshold_b) * 100, 1),
            "break_exit": round(float(params.break_exit_threshold) * 100, 1),
        },
        "timestamps": [st.timestamp.strftime("%m-%d %H:%M") for st in steps],
        "prices": [round(float(st.close_price), 2) for st in steps],
        "l1_trend": [round(float(st.l1_trend_price), 2) for st in steps],
        "p_trend": [round(st.p_trend * 100, 1) for st in steps],
        "p_up": [round(st.p_up_given_trend * 100, 1) for st in steps],
        "p_break": [round(st.p_break_within_h * 100, 1) for st in steps],
        "regimes": [st.hmm_regime for st in steps],
        "positions": [round(float(st.position_fraction) * 100, 1) for st in steps],
        "signals": [st.signal_action or "" for st in steps],
        "signal_labels": [st.signal_label for st in steps],
        "explanations": [st.human_explanation for st in steps],
        "series": competing_series,
    }
    return json.dumps(payload, ensure_ascii=False), svg_bundle


@router.get("/backtest")
async def page(request: Request, context: ContextDep) -> Any:
    default_inst = next((i for i in context.instruments if not i.is_benchmark), None)
    default_params = BacktestRunParameters(
        instrument_uid=default_inst.uid if default_inst else "uid-sber",
        ticker=default_inst.ticker if default_inst else "SBER",
        timeframe="1h",
        scenario="auto",
        bar_count=120,
    )
    report = await run_synthetic_backtest(context, default_params)
    chart_json, svg_bundle = _build_chart_payload(report, default_params)
    return render_page(
        request,
        "pages/backtest.html",
        title="Синтетический трейдер · Соревнование алгоритмов",
        section="backtest",
        data={
            "params": default_params,
            "report": report,
            "chart_json": chart_json,
            "svg": svg_bundle,
            "instruments": [i for i in context.instruments if not i.is_benchmark],
        },
    )


@router.post("/backtest/run", response_class=HTMLResponse)
async def run_backtest_endpoint(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    instrument_uid: str = Form("uid-sber"),
    timeframe: str = Form("1h"),
    scenario: str = Form("auto"),
    bar_count: int = Form(120),
    initial_capital: str = Form("1000000"),
    commission_bps: str = Form("5.0"),
    slippage_bps: str = Form("5.0"),
    horizon_bars: int = Form(8),
    k_tp: str = Form("1.8"),
    k_sl: str = Form("1.2"),
    trend_threshold_a: str = Form("0.52"),
    direction_threshold_b: str = Form("0.53"),
    break_exit_threshold: str = Form("0.72"),
    target_volatility_pct: str = Form("0.015"),
    catboost_depth: int = Form(4),
    catboost_iterations: int = Form(36),
    catboost_l2_leaf_reg: float = Form(5.0),
    weights_mode: str = Form("auto"),
    force_exchange_sync: str = Form("false"),
) -> Any:
    matched = next((i for i in context.instruments if i.uid == instrument_uid), None)
    ticker = matched.ticker if matched else "SBER"

    try:
        cap = Decimal(str(initial_capital))
        comm = Decimal(str(commission_bps))
        slip = Decimal(str(slippage_bps))
        tp = Decimal(str(k_tp))
        sl = Decimal(str(k_sl))
        thr_a = Decimal(str(trend_threshold_a))
        thr_b = Decimal(str(direction_threshold_b))
        thr_break = Decimal(str(break_exit_threshold))
        target_vol = Decimal(str(target_volatility_pct))
    except (InvalidOperation, ValueError):
        cap = Decimal("1000000")
        comm = Decimal("5.0")
        slip = Decimal("5.0")
        tp = Decimal("1.8")
        sl = Decimal("1.2")
        thr_a = Decimal("0.52")
        thr_b = Decimal("0.53")
        thr_break = Decimal("0.72")
        target_vol = Decimal("0.015")

    params = BacktestRunParameters(
        instrument_uid=instrument_uid,
        ticker=ticker,
        timeframe=timeframe if timeframe in {"5m", "15m", "1h", "4h", "1d"} else "1h",
        scenario=scenario
        if scenario in {"auto", "cycle", "bull", "chop", "crash_recovery"}
        else "auto",
        bar_count=max(40, min(int(bar_count), 360)),
        initial_capital=max(Decimal("10000"), cap),
        commission_bps=max(Decimal("0"), min(Decimal("100"), comm)),
        slippage_bps=max(Decimal("0"), min(Decimal("100"), slip)),
        horizon_bars=max(2, min(int(horizon_bars), 48)),
        k_tp=max(Decimal("0.5"), min(Decimal("10.0"), tp)),
        k_sl=max(Decimal("0.3"), min(Decimal("10.0"), sl)),
        trend_threshold_a=max(Decimal("0.30"), min(Decimal("0.90"), thr_a)),
        direction_threshold_b=max(Decimal("0.30"), min(Decimal("0.90"), thr_b)),
        break_exit_threshold=max(Decimal("0.30"), min(Decimal("0.95"), thr_break)),
        target_volatility_pct=max(Decimal("0.002"), min(Decimal("0.10"), target_vol)),
        catboost_depth=max(3, min(int(catboost_depth), 6)),
        catboost_iterations=max(10, min(int(catboost_iterations), 100)),
        catboost_l2_leaf_reg=max(1.0, min(float(catboost_l2_leaf_reg), 30.0)),
        weights_mode=weights_mode
        if weights_mode in {"auto", "retrain", "load_saved"}
        else "auto",
        force_exchange_sync=(force_exchange_sync.lower() in {"true", "1", "yes"}),
    )

    report = await run_synthetic_backtest(context, params)
    chart_json, svg_bundle = _build_chart_payload(report, params)
    return templates.TemplateResponse(
        request,
        "partials/backtest_report.html",
        {
            "request": request,
            "params": params,
            "report": report,
            "chart_json": chart_json,
            "svg": svg_bundle,
        },
    )
