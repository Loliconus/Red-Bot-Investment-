"""Параллельные эталонные алгоритмы и ML-конкуренты для честного соревнования (раздел 7.2 ТЗ).

Запускаются на тех же самых свечах с идентичными комиссией брокера и проскальзыванием:
1. ``Buy & Hold IMOEX`` (пассивное удержание индекса / актива);
2. ``MA Crossover (8/21)`` (трендовый робот на пересечении средних EMA 8 / EMA 21);
3. ``RSI(14) Bot`` (контртрендовый осцилляторный робот возврата к среднему);
4. ``LightGBM / HistGBDT (Одиночный ML)`` — градиентный бустинг без фильтра боковика ``P(trend)``;
5. ``ElasticNet + ℓ1-Тренд (Линейный ML)`` — линейная модель с L1/L2-регуляризацией.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.domain.value_objects import OHLCV
from core.synthetic.feature_selection import fit_elastic_net_weights
from core.synthetic.validation import compute_annualized_sharpe


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkRunResult:
    """Результаты прогона конкурирующего алгоритма в тех же издержках."""

    name: str
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: float
    trades_count: int
    equity_curve: tuple[Decimal, ...]
    bar_returns: tuple[float, ...]
    category: str = "classic"
    description: str = ""


def _compute_max_drawdown_pct(equity_curve: Sequence[Decimal]) -> Decimal:
    if not equity_curve:
        return Decimal("0")
    peak = equity_curve[0]
    max_dd = Decimal("0")
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > Decimal("0"):
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return (max_dd * Decimal("100")).quantize(Decimal("0.01"))


def run_buy_and_hold_benchmark(
    candles: Sequence[OHLCV],
    *,
    name: str = "Buy & Hold IMOEX",
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 1: Купи-и-держи (Buy-and-Hold IMOEX / базовый актив) с комиссией."""
    n = len(candles)
    if n < 2:
        return BenchmarkRunResult(
            name=name,
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
            category="passive",
            description="Пассивное удержание актива от первого до последнего бара",
        )

    cost_rate = commission_rate + slippage_rate
    entry_capital = initial_capital * (Decimal("1") - cost_rate)
    p0 = candles[0].close

    equity: list[Decimal] = [entry_capital.quantize(Decimal("0.01"))]
    bar_rets: list[float] = []

    for i in range(1, n):
        prev_c = candles[i - 1].close
        cur_c = candles[i].close
        r_i = float((cur_c - prev_c) / prev_c) if prev_c > Decimal("0") else 0.0
        bar_rets.append(r_i)
        eq_i = entry_capital * (cur_c / p0) if p0 > Decimal("0") else entry_capital
        if i == n - 1:
            eq_i *= Decimal("1") - cost_rate
        equity.append(eq_i.quantize(Decimal("0.01")))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=name,
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=1,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
        category="passive",
        description="Пассивная покупка и удержание без защиты от просадок",
    )


def run_ma_crossover_benchmark(
    candles: Sequence[OHLCV],
    *,
    fast_period: int = 8,
    slow_period: int = 21,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 2: Простой бот на пересечении двух скользящих средних (EMA fast / EMA slow)."""
    n = len(candles)
    if n < 3:
        return BenchmarkRunResult(
            name="MA Crossover (8/21)",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
            category="classic",
            description="Классическое пересечение быстрой и медленной EMA",
        )

    closes = [float(c.close) for c in candles]
    alpha_f = 2.0 / (fast_period + 1.0)
    alpha_s = 2.0 / (slow_period + 1.0)
    ema_f = closes[0]
    ema_s = closes[0]

    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    in_pos = False
    trades = 0

    for i in range(1, n):
        prev_c = closes[i - 1]
        cur_c = closes[i]
        raw_ret = (cur_c - prev_c) / prev_c if prev_c > 0 else 0.0

        want_long = ema_f > ema_s and i >= min(slow_period, max(3, n // 5))
        turnover_cost = 0.0
        if want_long != in_pos:
            turnover_cost = cost_rate
            in_pos = want_long
            if in_pos:
                trades += 1

        step_ret = (raw_ret if in_pos else 0.0) - turnover_cost
        eq *= 1.0 + step_ret
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

        ema_f = alpha_f * cur_c + (1.0 - alpha_f) * ema_f
        ema_s = alpha_s * cur_c + (1.0 - alpha_s) * ema_s

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=f"MA Crossover ({fast_period}/{slow_period})",
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
        category="classic",
        description="Входит при EMA(8) > EMA(21); страдает от ложных пробоев в боковике",
    )


def run_rsi_benchmark(
    candles: Sequence[OHLCV],
    *,
    period: int = 14,
    oversold: float = 35.0,
    overbought: float = 65.0,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 3: Простой контртрендовый бот по RSI(14)."""
    n = len(candles)
    if n < 3:
        return BenchmarkRunResult(
            name="RSI(14) Bot",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
            category="classic",
            description="Покупает перепроданность RSI < 35, фиксирует при RSI > 65",
        )

    closes = [float(c.close) for c in candles]
    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    in_pos = False
    trades = 0

    avg_gain = 0.0
    avg_loss = 0.0

    for i in range(1, n):
        prev_c = closes[i - 1]
        cur_c = closes[i]
        raw_ret = (cur_c - prev_c) / prev_c if prev_c > 0 else 0.0

        step_ret = raw_ret if in_pos else 0.0

        diff = cur_c - prev_c
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        if i <= period:
            avg_gain = ((avg_gain * (i - 1)) + gain) / i
            avg_loss = ((avg_loss * (i - 1)) + loss) / i
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 1e-12 else 50.0

        next_in_pos = in_pos
        if i >= min(period, max(3, n // 6)):
            if not in_pos and rsi < oversold:
                next_in_pos = True
            elif in_pos and rsi > overbought:
                next_in_pos = False

        if next_in_pos != in_pos:
            step_ret -= cost_rate
            if next_in_pos:
                trades += 1
            in_pos = next_in_pos

        eq *= max(0.01, 1.0 + step_ret)
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=f"RSI({period}) Bot",
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
        category="classic",
        description="Контртрендовый осциллятор: ловит отскоки, но рано выходит на тренде",
    )


def run_lightgbm_single_head_benchmark(
    candles: Sequence[OHLCV],
    feature_matrix: Sequence[Sequence[float]],
    dev_indices: Sequence[int],
    *,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 4 (ML-конкурент): Одиночный бустинг LightGBM / HistGBDT без фильтра режима."""
    n = min(len(candles), len(feature_matrix))
    if n < 6 or not feature_matrix or not feature_matrix[0]:
        return BenchmarkRunResult(
            name="LightGBM (Single-Head ML)",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
            category="ml",
            description="Одиночный градиентный бустинг на направление без фильтра пилы P(trend)",
        )

    closes = [float(c.close) for c in candles[:n]]
    next_up_labels = [
        1 if closes[min(n - 1, i + 1)] > closes[i] else 0 for i in range(n)
    ]
    train_idx = [i for i in dev_indices if i < n - 1]
    if len(train_idx) < 6:
        train_idx = list(range(max(2, (n * 2) // 3)))

    x_train = [list(feature_matrix[i]) for i in train_idx]
    y_train = [next_up_labels[i] for i in train_idx]

    pred_probs: list[float] = []
    model_label = "LightGBM / HistGBDT (Single-Head ML)"

    # Пытаемся обучить нативный LightGBM, затем HistGradientBoosting из scikit-learn
    fitted = False
    try:
        lgb_mod = importlib.import_module("lightgbm")
        if len(set(y_train)) >= 2:
            clf = lgb_mod.LGBMClassifier(
                n_estimators=35,
                max_depth=3,
                learning_rate=0.06,
                min_child_samples=max(3, len(x_train) // 8),
                random_state=42,
                verbose=-1,
            )
            clf.fit(x_train, y_train)
            raw_p = clf.predict_proba([list(feature_matrix[i]) for i in range(n)])
            pred_probs = [float(row[1]) for row in raw_p]
            model_label = "LightGBM (Single-Head ML)"
            fitted = True
    except (ImportError, RuntimeError, ValueError, TypeError):
        fitted = False

    if not fitted:
        try:
            sk_ens = importlib.import_module("sklearn.ensemble")
            if len(set(y_train)) >= 2:
                clf = sk_ens.HistGradientBoostingClassifier(
                    max_iter=35,
                    max_depth=3,
                    learning_rate=0.06,
                    min_samples_leaf=max(3, len(x_train) // 8),
                    random_state=42,
                )
                clf.fit(x_train, y_train)
                raw_p = clf.predict_proba([list(feature_matrix[i]) for i in range(n)])
                pred_probs = [float(row[1]) for row in raw_p]
                model_label = "HistGBDT (Single-Head ML)"
                fitted = True
        except (ImportError, RuntimeError, ValueError, TypeError):
            fitted = False

    if not fitted:
        # Встроенный быстрый пень-бустинг по топ-признакам импульса
        n_feat = len(feature_matrix[0])
        corrs = [0.0] * n_feat
        y_mean = sum(y_train) / len(y_train)
        for f_j in range(n_feat):
            col = [x_train[k][f_j] for k in range(len(x_train))]
            c_mean = sum(col) / len(col)
            num = sum((col[k] - c_mean) * (y_train[k] - y_mean) for k in range(len(col)))
            den = math.sqrt(
                sum((col[k] - c_mean) ** 2 for k in range(len(col)))
                * max(sum((y_train[k] - y_mean) ** 2 for k in range(len(col))), 1e-9)
            )
            corrs[f_j] = num / den if den > 1e-9 else 0.0
        top_f = sorted(range(n_feat), key=lambda j: abs(corrs[j]), reverse=True)[:5]
        for i in range(n):
            score = sum(corrs[j] * feature_matrix[i][j] for j in top_f)
            pred_probs.append(1.0 / (1.0 + math.exp(-max(min(score * 2.2, 15.0), -15.0))))

    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    pos_frac = 0.0
    trades = 0

    mean_p = sum(pred_probs) / max(len(pred_probs), 1)
    entry_thr = min(max(mean_p + 0.015, 0.48), 0.56)

    for i in range(n - 1):
        p_up = pred_probs[i]
        # Одиночный ML без фильтра пилы входит всякий раз, когда P(up) выше адаптивного порога
        target_frac = 0.70 if p_up > entry_thr else 0.0
        turnover = abs(target_frac - pos_frac)
        if target_frac > 0.0 and pos_frac == 0.0:
            trades += 1
        pos_frac = target_frac

        raw_ret = (closes[i + 1] - closes[i]) / closes[i] if closes[i] > 0 else 0.0
        step_ret = pos_frac * raw_ret - turnover * cost_rate
        eq *= max(0.01, 1.0 + step_ret)
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=model_label,
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
        category="ml",
        description="Одиночный ML-классификатор знака свечи (без фильтра боковика и риска слома)",
    )


def run_elastic_net_trend_benchmark(
    candles: Sequence[OHLCV],
    feature_names: Sequence[str],
    feature_matrix: Sequence[Sequence[float]],
    dev_indices: Sequence[int],
    *,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 5 (ML-конкурент): Линейная модель ElasticNet (L1+L2) по стационарным признакам."""
    n = min(len(candles), len(feature_matrix))
    if n < 6 or not feature_matrix or not feature_names:
        return BenchmarkRunResult(
            name="ElasticNet (Linear ML)",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
            category="ml",
            description="Линейная регрессия с L1+L2 регуляризацией по признакам Слоя A+B",
        )

    closes = [float(c.close) for c in candles[:n]]
    fwd_rets = [
        (closes[min(n - 1, i + 1)] - closes[i]) / max(closes[i], 1e-9) for i in range(n)
    ]
    train_idx = [i for i in dev_indices if i < n - 1]
    if len(train_idx) < 6:
        train_idx = list(range(max(2, (n * 2) // 3)))

    x_train = [feature_matrix[i] for i in train_idx]
    y_train = [fwd_rets[i] for i in train_idx]

    weights_map = fit_elastic_net_weights(
        feature_names,
        x_train,
        y_train,
        alpha=0.0004,
        l1_ratio=0.5,
    )
    w_vec = [weights_map.get(nm, 0.0) for nm in feature_names]

    # Нормализуем признаки по обучающей выборке для честного линейного прогноза
    n_feat = len(feature_names)
    means = [
        sum(x_train[r][j] for r in range(len(x_train))) / len(x_train) for j in range(n_feat)
    ]
    stds = [
        max(
            math.sqrt(
                sum((x_train[r][j] - means[j]) ** 2 for r in range(len(x_train)))
                / len(x_train)
            ),
            1e-9,
        )
        for j in range(n_feat)
    ]
    y_mean = sum(y_train) / len(y_train)

    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    pos_frac = 0.0
    trades = 0

    all_preds = [
        y_mean
        + sum(w_vec[j] * ((feature_matrix[i][j] - means[j]) / stds[j]) for j in range(n_feat))
        for i in range(n)
    ]
    median_pred = sorted(all_preds)[n // 2]

    for i in range(n - 1):
        pred_ret = all_preds[i]
        target_frac = 0.68 if (pred_ret > median_pred and pred_ret > -0.001) else 0.0
        turnover = abs(target_frac - pos_frac)
        if target_frac > 0.0 and pos_frac == 0.0:
            trades += 1
        pos_frac = target_frac

        raw_ret = fwd_rets[i]
        step_ret = pos_frac * raw_ret - turnover * cost_rate
        eq *= max(0.01, 1.0 + step_ret)
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name="ElasticNet (Linear ML)",
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
        category="ml",
        description="Линейная ML-модель (L1+L2): ловит тренды, но не видит нелинейных разворотов",
    )
