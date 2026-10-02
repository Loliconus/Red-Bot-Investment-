import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from synthetic_trader.statistics import multiple_testing, population_stability_index


def returns():
    rng = np.random.default_rng(8)
    index = pd.bdate_range("2020-01-01", periods=300, tz="Europe/Moscow")
    matrix = pd.DataFrame(
        {"primary": rng.normal(0.001, 0.01, 300), "alternative": rng.normal(0.0005, 0.012, 300)},
        index=index,
    )
    benchmark = pd.Series(rng.normal(0.0003, 0.01, 300), index=index)
    return matrix, benchmark


def test_dsr_uses_daily_sharpe_and_raw_kurtosis_and_counts_trials():
    matrix, benchmark = returns()
    stats = multiple_testing(
        matrix, benchmark, primary="primary", trials=10, repetitions=100, seed=1
    )
    dsr = stats["dsr"]
    r = matrix["primary"].to_numpy()
    sr = r.mean() / r.std(ddof=0)
    expected = norm.cdf(
        (sr - dsr["sr_star"])
        * np.sqrt(len(r) - 1)
        / np.sqrt(
            1 - skew(r, bias=False) * sr + (kurtosis(r, bias=False, fisher=False) - 1) * sr**2 / 4
        )
    )
    assert abs(expected - dsr["dsr"]) < 1e-12
    assert dsr["n_trials"] == 10 and dsr["n_obs"] == 300
    assert abs(dsr["observed_sr"] - sr) < 1e-12
    more = multiple_testing(
        matrix, benchmark, primary="primary", trials=100, repetitions=100, seed=1
    )
    assert more["dsr"]["dsr"] < dsr["dsr"]


def test_pbo_is_strategy_matrix_cscv_and_spa_uses_loss_sign_correctly():
    matrix, benchmark = returns()
    stats = multiple_testing(
        matrix, benchmark, primary="primary", trials=2, repetitions=100, seed=1
    )
    assert 0 <= stats["pbo"]["probability"] <= 1
    assert stats["pbo"]["combinations"] == 70
    assert 0 <= stats["spa"]["p_value"] <= 1
    assert 0 <= stats["reality_check"]["p_value"] <= 1
    assert stats["bootstrap"]["method"] == "stationary"


def test_no_trade_runs_do_not_fabricate_statistical_significance():
    matrix, benchmark = returns()
    matrix[:] = 0
    stats = multiple_testing(
        matrix, benchmark, primary="primary", trials=2, repetitions=100, seed=1
    )
    assert stats["dsr"] is None and stats["pbo"] is None and stats["spa"] is None
    assert stats["unavailable_reason"]


def test_psi_includes_shifted_tail_mass_instead_of_discarding_it():
    reference = np.linspace(0, 1, 1000)
    bounds = np.quantile(reference, np.linspace(0, 1, 11)).tolist()
    assert population_stability_index(bounds, reference, reference) < 1e-12
    assert population_stability_index(bounds, reference, reference + 10) > 1
