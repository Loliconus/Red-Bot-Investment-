import pytest

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.storage import write_json


def test_optional_mlflow_registers_full_bundle_locally_without_live(tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    from mlflow.tracking import MlflowClient

    from synthetic_trader.mlops import log_mlflow_run

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "research"
    directory = root / "runs" / ("a" * 32)
    config = ExperimentConfig().model_dump(mode="json")
    report = {
        "run_id": "a" * 32,
        "source": "demo",
        "config": config,
        "dataset": {"dataset_id": "b" * 64},
        "feature_set_hash": "c" * 64,
        "feature_version": "v1",
        "candidate_model_hash": "d" * 64,
        "environment": {"python": "3.14.8", "git_commit": "unknown"},
        "metrics": {"sharpe": 0.1, "trades": 3},
        "probabilities": {"trend": {"brier": 0.2}},
        "statistics": {},
    }
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text("test fixture; not trading evidence")
    (directory / "predictions.parquet").write_bytes(b"fixture-only")
    write_json(directory / "model" / "bundle.json", {"fixture": True})
    log_mlflow_run(root, directory, report)
    assert (root / "mlflow.sqlite3").is_file()
    assert (root / "mlflow_artifacts").is_dir()
    logged = __import__("json").loads((directory / "mlflow.json").read_text())
    client = MlflowClient(
        tracking_uri=f"sqlite:///{root / 'mlflow.sqlite3'}",
        registry_uri=f"sqlite:///{root / 'mlflow.sqlite3'}",
    )
    version = client.get_model_version(logged["registry_name"], logged["version"])
    assert version.tags["live_enabled"] == "false" and version.tags["model_hash"] == "d" * 64
    assert "calibrated_bundle" in version.source
    assert not (tmp_path / "mlruns").exists()


def test_optional_prefect_is_a_real_lazy_deployable_flow():
    prefect = pytest.importorskip("prefect")
    from synthetic_trader.mlops import build_research_flow

    flow = build_research_flow()
    assert isinstance(flow, prefect.Flow)
    assert flow.name == "synthetic-trader-research"
    assert callable(flow.serve)  # No server, schedule, real data, or job is started here.
