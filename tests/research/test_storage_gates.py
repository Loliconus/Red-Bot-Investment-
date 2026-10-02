from concurrent.futures import ThreadPoolExecutor

import pytest

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.storage import (
    ExperimentLedger,
    FinalAlreadyConsumedError,
    research_job,
    study_key,
)


def test_once_is_atomic_between_competing_workers(tmp_path):
    ledger = ExperimentLedger(tmp_path)
    config = ExperimentConfig()
    ledger.register_study(config)

    def claim(index):
        try:
            ledger.reveal_once(study_key(config), str(index))
            return True
        except FinalAlreadyConsumedError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2017-01-01"},
        {"interval": "1d"},
        {"symbols": ["GMKN", "ROSN"]},
        {"freeze_months": 12},
        {"end": "2026-09-29"},
    ],
)
def test_new_study_id_cannot_reveal_the_same_calendar(change, tmp_path):
    ledger = ExperimentLedger(tmp_path)
    config = ExperimentConfig()
    ledger.register_study(config)
    ledger.reveal_once(study_key(config), "previous")
    other = ExperimentConfig.model_validate({**config.model_dump(mode="json"), **change})
    assert study_key(config) != study_key(other)
    with pytest.raises(FinalAlreadyConsumedError):
        ledger.register_study(other)


def test_dsr_trial_count_and_variance_do_not_reset_with_universe(tmp_path):
    ledger = ExperimentLedger(tmp_path)
    config = ExperimentConfig(additional_trials=3)
    ledger.register_study(config)
    assert ledger.register_trials(study_key(config), "first", config, [".5", ".6"]) == 5
    ledger.record_sharpes("first", {".5": 0.1, ".6": 0.2})
    other = ExperimentConfig(symbols=("GMKN",))
    ledger.register_study(other)
    assert ledger.register_trials(study_key(other), "second", other, [".5"]) == 6
    ledger.record_sharpes("second", {".5": 0.3})
    population = ledger.trial_population(study_key(other))
    assert population["observed_sharpes"] == 3 and abs(population["variance"] - 0.01) < 1e-12
    assert population["external_trials"] == 3


def test_candidate_ledger_is_immutable_and_binds_model_report(tmp_path):
    ledger = ExperimentLedger(tmp_path)
    ledger.register_candidate("run", "study", "report_hash", "model_hash")
    ledger.assert_candidate("run", "report_hash", "model_hash")
    with pytest.raises(ValueError, match="изменены"):
        ledger.assert_candidate("run", "different", "model_hash")
    with pytest.raises(ValueError, match="изменены"):
        ledger.assert_candidate("run", "report_hash", "different")


def test_one_mutex_across_cli_gui_prefect_and_cleanup_after_error(tmp_path):
    with pytest.raises(RuntimeError), research_job(tmp_path, "first"):
        with pytest.raises(ValueError, match="занят"), research_job(tmp_path, "second"):
            pass
        raise RuntimeError("simulated worker error")
    assert not (tmp_path / "worker.lock").exists()
