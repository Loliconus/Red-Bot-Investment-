"""Интерактивный бэктест, соревнование алгоритмов и валидация «Синтетический трейдер» (GUI)."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.run_backtest import (
    BacktestRunParameters,
    run_synthetic_backtest,
)
from core.synthetic import SyntheticTraderEvaluationReport, translate_reason_to_human

router = APIRouter(tags=["backtest"], dependencies=[Depends(require_session)])

ALGO_COLORS: dict[str, str] = {
    "Buy & Hold IMOEX": "#64748b",
    "LightGBM (Single-Head ML)": "#3b82f6",
    "HistGBDT (Single-Head ML)": "#3b82f6",
    "LightGBM / HistGBDT (Single-Head ML)": "#3b82f6",
    "ElasticNet (Linear ML)": "#8b5cf6",
    "MA Crossover (8/21)": "#f59e0b",
    "RSI(14) Bot": "#10b981",
}


def _safe_decimal(value: Any, default: Decimal, *, lo: Decimal, hi: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return min(hi, max(lo, parsed))


def _safe_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    try:
        parsed = int(float(str(value)))
    except (ValueError, TypeError):
        return default
    return min(hi, max(lo, parsed))


def _align_pct_curve(
    equity_curve: tuple[Decimal, ...],
    initial_capital: Decimal,
    target_len: int,
) -> list[float]:
    """Преобразует кривую капитала в кумулятивные проценты доходности длины ``target_len``."""
    if not equity_curve or initial_capital <= Decimal("0") or target_len <= 0:
        return [0.0] * max(target_len, 1)
    init_f = float(initial_capital)
    pcts = [round((float(eq) / init_f - 1.0) * 100.0, 2) for eq in equity_curve]
    if len(pcts) >= target_len:
        return pcts[:target_len]
    return pcts + [pcts[-1]] * (target_len - len(pcts))


def _serialize_report(
    report: SyntheticTraderEvaluationReport,
    params: BacktestRunParameters,
) -> dict[str, Any]:
    """Преобразует доменный отчёт «Синтетического трейдера» в контекст шаблона и JSON графиков."""
    shap_items = [
        {"feature": k, "value": f"{v:+.4f}", "abs_pct": min(100, int(abs(v) * 220))}
        for k, v in report.latest_triad.top_shap_contributors.items()
    ]
    cal_list = []
    for head_name, cal in report.calibration_reports.items():
        cal_list.append(
            {
                "head_name": head_name,
                "method": cal.method,
                "temperature": f"{cal.temperature:.2f}",
                "brier_raw": f"{cal.brier_score_raw:.4f}",
                "brier_cal": f"{cal.brier_score_calibrated:.4f}",
                "bss": f"{cal.brier_skill_score:+.3f}",
                "ece_raw": f"{cal.ece_raw:.4f}",
                "ece_cal": f"{cal.ece_calibrated:.4f}",
                "bins": [
                    {
                        "range": f"{b.lower_bound:.1f}–{b.upper_bound:.1f}",
                        "pred": f"{b.mean_predicted * 100:.1f}%",
                        "emp": f"{b.empirical_frequency * 100:.1f}%",
                        "count": b.sample_count,
                    }
                    for b in cal.reliability_bins
                    if b.sample_count > 0
                ],
            }
        )

    bench_rows = [
        {
            "name": b.name,
            "category": b.category,
            "description": b.description,
            "color": ALGO_COLORS.get(b.name, "#0ea5e9"),
            "total_return_pct": str(b.total_return_pct),
            "is_positive": b.total_return_pct >= Decimal("0"),
            "beats_bot": b.total_return_pct > report.total_return_pct,
            "diff_vs_bot": str(
                (report.total_return_pct - b.total_return_pct).quantize(Decimal("0.01"))
            ),
            "max_drawdown_pct": str(b.max_drawdown_pct),
            "sharpe": f"{b.sharpe_ratio:.2f}",
            "trades": b.trades_count,
            "final_equity": (
                str(b.equity_curve[-1]) if b.equity_curve else str(params.initial_capital)
            ),
        }
        for b in report.benchmarks
    ]

    psi_items = [
        {
            "feature": k,
            "psi": f"{v:.4f}",
            "status": "CRITICAL" if v >= 0.20 else ("WARN" if v >= 0.10 else "OK"),
        }
        for k, v in list(report.drift_diagnostic.psi_by_feature.items())[:10]
    ]

    trade_rows = [
        {
            "trade_id": tr.trade_id,
            "direction": tr.direction,
            "entry_time": tr.entry_timestamp.strftime("%d.%m %H:%M"),
            "exit_time": tr.exit_timestamp.strftime("%d.%m %H:%M"),
            "entry_price": str(tr.entry_price),
            "exit_price": str(tr.exit_price),
            "bars_held": tr.bars_held,
            "position_pct": f"{tr.position_fraction * Decimal('100'):.0f}%",
            "pnl_pct": f"{'+' if tr.pnl_pct >= Decimal('0') else ''}{tr.pnl_pct}%",
            "pnl_rub": f"{'+' if tr.pnl_rub >= Decimal('0') else ''}{tr.pnl_rub} ₽",
            "is_win": tr.pnl_rub >= Decimal("0"),
            "entry_reason": tr.entry_reason,
            "exit_reason": tr.exit_reason,
            "exit_code": tr.exit_code,
        }
        for tr in report.completed_trades
    ]

    recent_steps = [
        {
            "timestamp": st.timestamp.strftime("%Y-%m-%d %H:%M"),
            "close_price": str(st.close_price),
            "l1_trend_price": str(st.l1_trend_price),
            "l1_slope": f"{st.l1_slope:+.5f}",
            "hmm_regime": st.hmm_regime.upper(),
            "p_trend": f"{st.p_trend * 100:.1f}%",
            "p_up": f"{st.p_up_given_trend * 100:.1f}%",
            "p_break": f"{st.p_break_within_h * 100:.1f}%",
            "direction": st.direction.value.upper(),
            "position_fraction": f"{st.position_fraction * Decimal('100'):.1f}%",
            "equity": str(st.equity),
            "blocked_reason": st.blocked_reason or "ACTIVE_SIGNAL",
            "human_explanation": st.human_explanation,
            "signal_action": st.signal_action,
            "signal_label": st.signal_label,
        }
        for st in report.step_records[-30:]
    ]

    # Формируем полный синхронный пакет данных для 3 интерактивных графиков
    n_steps = len(report.step_records)
    init_cap = report.initial_capital
    bot_eq_tuple = tuple(st.equity for st in report.step_records)
    bot_curve_pct = _align_pct_curve(bot_eq_tuple, init_cap, n_steps)

    bot_sign = "+" if report.total_return_pct >= Decimal("0") else ""
    algorithms_chart = [
        {
            "id": "synthetic_trader",
            "name": "Синтетический трейдер (CatBoost Triad + HMM)",
            "color": "#e11d48",
            "width": 3.2,
            "return_pct": f"{bot_sign}{report.total_return_pct}%",
            "max_dd_pct": f"{report.max_drawdown_pct}%",
            "trades": report.trades_count,
            "curve_pct": bot_curve_pct,
        }
    ]
    for b in report.benchmarks:
        b_sign = "+" if b.total_return_pct >= Decimal("0") else ""
        algorithms_chart.append(
            {
                "id": b.name.lower().replace(" ", "_"),
                "name": b.name,
                "color": ALGO_COLORS.get(b.name, "#0ea5e9"),
                "width": 1.9,
                "return_pct": f"{b_sign}{b.total_return_pct}%",
                "max_dd_pct": f"{b.max_drawdown_pct}%",
                "trades": b.trades_count,
                "curve_pct": _align_pct_curve(b.equity_curve, init_cap, n_steps),
            }
        )

    chart_signals = []
    for idx, st in enumerate(report.step_records):
        if st.signal_action in {"BUY", "EXIT"}:
            chart_signals.append(
                {
                    "bar": idx,
                    "action": st.signal_action,
                    "price": float(st.close_price),
                    "label": st.signal_label,
                    "reason": st.human_explanation,
                    "time": st.timestamp.strftime("%d.%m %H:%M"),
                }
            )

    chart_payload = {
        "ticker": report.ticker,
        "timeframe": report.timeframe,
        "timestamps": [st.timestamp.strftime("%d.%m %H:%M") for st in report.step_records],
        "prices": [round(float(st.close_price), 2) for st in report.step_records],
        "l1_trend": [round(float(st.l1_trend_price), 2) for st in report.step_records],
        "regimes": [st.hmm_regime.upper() for st in report.step_records],
        "p_trend": [round(st.p_trend * 100.0, 1) for st in report.step_records],
        "p_up": [round(st.p_up_given_trend * 100.0, 1) for st in report.step_records],
        "p_break": [round(st.p_break_within_h * 100.0, 1) for st in report.step_records],
        "explanations": [st.human_explanation for st in report.step_records],
        "thr_a": round(float(params.trend_threshold_a) * 100.0, 1),
        "thr_b": round(float(params.direction_threshold_b) * 100.0, 1),
        "thr_break": round(float(params.break_exit_threshold) * 100.0, 1),
        "signals": chart_signals,
        "algorithms": algorithms_chart,
    }

    pnl_rub_val = (report.final_equity - report.initial_capital).quantize(Decimal("0.01"))
    pnl_rub_str = f"{'+' if pnl_rub_val >= Decimal('0') else ''}{pnl_rub_val} ₽"

    return {
        "instrument_uid": report.instrument_uid,
        "ticker": report.ticker,
        "timeframe": report.timeframe,
        "scenario": params.scenario,
        "initial_capital": str(report.initial_capital),
        "final_equity": str(report.final_equity),
        "pnl_rub": pnl_rub_str,
        "total_return_pct": str(report.total_return_pct),
        "is_profitable": report.total_return_pct >= Decimal("0"),
        "max_drawdown_pct": str(report.max_drawdown_pct),
        "oos_sharpe": f"{report.oos_sharpe_ratio:.2f}",
        "win_rate_pct": str(report.win_rate_pct),
        "profit_factor": str(report.profit_factor),
        "trades_count": report.trades_count,
        "ml_backend_summary": report.ml_backend_summary,
        "human_summary": report.human_summary,
        "completed_trades": trade_rows,
        "chart_json": json.dumps(chart_payload, ensure_ascii=False),
        "triad": {
            "p_trend_pct": f"{report.latest_triad.p_trend * 100:.1f}",
            "p_up_pct": f"{report.latest_triad.p_up_given_trend * 100:.1f}",
            "p_break_pct": f"{report.latest_triad.p_break_within_h * 100:.1f}",
            "raw_p_trend_pct": f"{report.latest_triad.raw_p_trend * 100:.1f}",
            "raw_p_up_pct": f"{report.latest_triad.raw_p_up_given_trend * 100:.1f}",
            "raw_p_break_pct": f"{report.latest_triad.raw_p_break_within_h * 100:.1f}",
            "directional_edge": f"{report.latest_triad.expected_directional_edge:+.3f}",
            "confidence_edge": f"{report.latest_triad.confidence_adjusted_edge:+.3f}",
            "shap": shap_items,
        },
        "hmm": {
            "dominant_regime": report.latest_hmm.dominant_regime.value.upper(),
            "prob_trend_pct": f"{report.latest_hmm.prob_trend * 100:.1f}",
            "prob_chop_pct": f"{report.latest_hmm.prob_chop * 100:.1f}",
            "prob_panic_pct": f"{report.latest_hmm.prob_panic * 100:.1f}",
            "expected_duration": f"{report.latest_hmm.expected_duration_bars:.1f}",
            "is_trading_banned": report.latest_hmm.is_trading_banned,
        },
        "l1_filter": {
            "lambda_reg": f"{report.l1_trend_summary.lambda_reg:.2f}",
            "knots_count": len(report.l1_trend_summary.knot_indices),
            "residual_std": f"{report.l1_trend_summary.residual_std:.5f}",
            "dual_gap": f"{report.l1_trend_summary.dual_gap:.2e}",
        },
        "risk_decision": {
            "direction": report.latest_risk_decision.direction.value.upper(),
            "allow_entry": report.latest_risk_decision.allow_entry,
            "recommended_lots": report.latest_risk_decision.recommended_lots,
            "position_fraction_pct": (
                f"{report.latest_risk_decision.position_fraction * Decimal('100'):.2f}"
            ),
            "stop_loss": str(report.latest_risk_decision.stop_loss_price),
            "take_profit": str(report.latest_risk_decision.take_profit_price),
            "trailing_stop": str(report.latest_risk_decision.trailing_stop_price),
            "vol_scalar": str(report.latest_risk_decision.volatility_scalar),
            "break_scalar": str(report.latest_risk_decision.break_penalty_scalar),
            "blocked_reason": report.latest_risk_decision.blocked_reason or "Разрешён вход",
            "human_reason": translate_reason_to_human(report.latest_risk_decision.blocked_reason),
        },
        "feature_selection": {
            "initial_count": len(report.feature_selection.initial_features),
            "enet_count": len(report.feature_selection.elastic_net_survivors),
            "stability_count": len(report.feature_selection.stability_survivors),
            "final_count": len(report.feature_selection.final_selected_features),
            "selected_features": list(report.feature_selection.final_selected_features),
            "dropped_collinear": [
                {"dropped": a, "kept": b, "corr": f"{c:+.3f}"}
                for a, b, c in report.feature_selection.dropped_collinear_pairs[:8]
            ],
        },
        "calibration": cal_list,
        "validation": {
            "cpcv_splits": report.overfitting_audit.cpcv_n_splits,
            "cpcv_paths": report.overfitting_audit.cpcv_n_paths,
            "cpcv_mean_sharpe": f"{report.overfitting_audit.cpcv_mean_oos_sharpe:.2f}",
            "cpcv_std_sharpe": f"{report.overfitting_audit.cpcv_std_oos_sharpe:.2f}",
            "pbo_pct": f"{report.overfitting_audit.pbo_probability * 100:.1f}%",
            "pbo_logit": f"{report.overfitting_audit.pbo_logit_median:+.3f}",
            "dsr_pct": f"{report.overfitting_audit.deflated_sharpe_ratio * 100:.1f}%",
            "null_sr0": f"{report.overfitting_audit.expected_max_null_sharpe:.3f}",
            "n_trials": report.overfitting_audit.n_trials_tested,
            "white_rc_pvalue": f"{report.overfitting_audit.white_reality_check_pvalue:.3f}",
            "hansen_spa_pvalue": f"{report.overfitting_audit.hansen_spa_pvalue:.3f}",
            "passes_gate": report.overfitting_audit.passes_statistical_gate,
            "dev_samples": len(report.holdout_partition.dev_indices),
            "holdout_samples": len(report.holdout_partition.holdout_indices),
            "holdout_cutoff": report.holdout_partition.cutoff_timestamp.strftime("%Y-%m-%d %H:%M"),
        },
        "benchmarks": bench_rows,
        "mlops": {
            "dataset_id": report.dataset_manifest.dataset_id,
            "sha256_short": report.dataset_manifest.sha256_digest[:16],
            "schema_version": report.dataset_manifest.feature_schema_version,
            "bar_count": report.dataset_manifest.bar_count,
            "run_id": report.experiment_record.run_id,
            "beats_all_benchmarks": report.experiment_record.beats_all_benchmarks,
            "shadow_mode_ready": report.experiment_record.shadow_mode_ready,
            "drift_status": report.drift_diagnostic.overall_status.value.upper(),
            "max_psi": f"{report.drift_diagnostic.max_psi:.4f}",
            "mean_psi": f"{report.drift_diagnostic.mean_psi:.4f}",
            "retrain_triggered": report.drift_diagnostic.should_trigger_retraining,
            "retrain_reason": (
                report.drift_diagnostic.retraining_reason or "Распределение стабильно"
            ),
            "psi_items": psi_items,
        },
        "steps": recent_steps,
        "params": {
            "instrument_uid": params.instrument_uid,
            "timeframe": params.timeframe,
            "scenario": params.scenario,
            "bar_count": params.bar_count,
            "initial_capital": str(params.initial_capital),
            "commission_bps": str(params.commission_bps),
            "slippage_bps": str(params.slippage_bps),
            "horizon_bars": params.horizon_bars,
            "k_tp": str(params.k_tp),
            "k_sl": str(params.k_sl),
            "trend_threshold_a": str(params.trend_threshold_a),
            "direction_threshold_b": str(params.direction_threshold_b),
            "break_exit_threshold": str(params.break_exit_threshold),
            "catboost_depth": params.catboost_depth,
            "catboost_iterations": params.catboost_iterations,
        },
    }


def _resolve_ticker(context: Any, instrument_uid: str) -> str:
    for inst in context.tradable_instruments:
        if inst.uid == instrument_uid:
            return str(inst.ticker)
    return "SBER"


@router.get("/backtest")
async def page(request: Request, context: ContextDep) -> Any:
    default_uid = (
        context.tradable_instruments[0].uid if context.tradable_instruments else "uid-sber"
    )
    uid = request.query_params.get("instrument_uid", default_uid)
    tf = request.query_params.get("timeframe", "1h")
    scen = request.query_params.get("scenario", "auto")
    params = BacktestRunParameters(
        instrument_uid=uid,
        ticker=_resolve_ticker(context, uid),
        timeframe=tf if tf in {"5m", "15m", "1h", "4h", "1d"} else "1h",
        scenario=scen if scen in {"auto", "cycle", "bull", "chop", "crash_recovery"} else "auto",
    )
    report = await run_synthetic_backtest(context, params)
    return render_page(
        request,
        "pages/backtest.html",
        title="Синтетический трейдер — Соревнование алгоритмов и Бэктест",
        section="backtest",
        data={
            "instruments": context.tradable_instruments,
            "report": _serialize_report(report, params),
        },
    )


@router.post("/backtest/run")
@router.post("/api/backtest/runs")
async def launch(
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
    catboost_depth: int = Form(4),
    catboost_iterations: int = Form(36),
) -> Any:
    tf_clean = timeframe if timeframe in {"5m", "15m", "1h", "4h", "1d"} else "1h"
    scen_clean = (
        scenario if scenario in {"auto", "cycle", "bull", "chop", "crash_recovery"} else "auto"
    )
    params = BacktestRunParameters(
        instrument_uid=instrument_uid,
        ticker=_resolve_ticker(context, instrument_uid),
        timeframe=tf_clean,
        scenario=scen_clean,
        bar_count=_safe_int(bar_count, 120, lo=40, hi=300),
        initial_capital=_safe_decimal(
            initial_capital,
            Decimal("1000000"),
            lo=Decimal("10000"),
            hi=Decimal("100000000"),
        ),
        commission_bps=_safe_decimal(
            commission_bps,
            Decimal("5.0"),
            lo=Decimal("0"),
            hi=Decimal("100"),
        ),
        slippage_bps=_safe_decimal(
            slippage_bps,
            Decimal("5.0"),
            lo=Decimal("0"),
            hi=Decimal("100"),
        ),
        horizon_bars=_safe_int(horizon_bars, 8, lo=3, hi=36),
        k_tp=_safe_decimal(k_tp, Decimal("1.8"), lo=Decimal("0.5"), hi=Decimal("6.0")),
        k_sl=_safe_decimal(k_sl, Decimal("1.2"), lo=Decimal("0.3"), hi=Decimal("5.0")),
        trend_threshold_a=_safe_decimal(
            trend_threshold_a,
            Decimal("0.52"),
            lo=Decimal("0.30"),
            hi=Decimal("0.90"),
        ),
        direction_threshold_b=_safe_decimal(
            direction_threshold_b,
            Decimal("0.53"),
            lo=Decimal("0.50"),
            hi=Decimal("0.90"),
        ),
        break_exit_threshold=_safe_decimal(
            break_exit_threshold,
            Decimal("0.72"),
            lo=Decimal("0.35"),
            hi=Decimal("0.95"),
        ),
        catboost_depth=_safe_int(catboost_depth, 4, lo=4, hi=6),
        catboost_iterations=_safe_int(catboost_iterations, 36, lo=12, hi=80),
    )
    report = await run_synthetic_backtest(context, params)
    serialized = _serialize_report(report, params)

    if request.headers.get("HX-Request") == "true":
        return render_partial(
            request,
            "partials/backtest_report.html",
            {"report": serialized},
        )

    return render_page(
        request,
        "pages/backtest.html",
        title="Синтетический трейдер — Соревнование алгоритмов и Бэктест",
        section="backtest",
        data={
            "instruments": context.tradable_instruments,
            "report": serialized,
        },
    )
