"""Опциональные MLflow 3 / Prefect 3 интеграции, строго research-only.

В базовом режиме всегда работают immutable snapshots и append-only trial
ledger. Эти интеграции не создают брокерский клиент и не разрешают LIVE.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from synthetic_trader.storage import write_json


def log_mlflow_run(root: Path, run_dir: Path, report: dict[str, Any]) -> None:
    """Локальный SQLite backend (не deprecated filesystem metadata backend)."""
    try:
        import mlflow
        from mlflow.exceptions import MlflowException
        from mlflow.tracking import MlflowClient
    except ImportError as exc:
        raise ValueError(
            "MLflow не установлен: uv sync --extra mlops; tracking не выполнен"
        ) from exc
    uri = f"sqlite:///{(root / 'mlflow.sqlite3').resolve().as_posix()}"
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    experiment_name = "synthetic-trader-research"
    if client.get_experiment_by_name(experiment_name) is None:
        client.create_experiment(
            experiment_name, artifact_location=(root / "mlflow_artifacts").resolve().as_uri()
        )
    mlflow.set_experiment(experiment_name)
    params = {
        key: value for key, value in report["config"].items() if key not in {"risk", "symbols"}
    }
    params["symbols"] = ",".join(report["config"]["symbols"])
    params.update({f"risk.{key}": value for key, value in report["config"]["risk"].items()})
    params.update(
        {
            "dataset_id": report["dataset"]["dataset_id"],
            "feature_set_hash": report["feature_set_hash"],
            "feature_version": report["feature_version"],
            "python": report["environment"]["python"],
        }
    )
    metrics = {
        key: value
        for key, value in report["metrics"].items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    for head, score in report["probabilities"].items():
        if score["brier"] is not None:
            metrics[f"brier.{head}"] = score["brier"]
    for test, key in (
        ("dsr", "dsr"),
        ("pbo", "probability"),
        ("spa", "p_value"),
        ("reality_check", "p_value"),
    ):
        if report["statistics"].get(test):
            metrics[test] = report["statistics"][test][key]
    with mlflow.start_run(run_name=report["run_id"]) as run:
        mlflow.set_tags(
            {
                "source": report["source"],
                "stage": "research",
                "live_enabled": "false",
                "git_commit": report["environment"]["git_commit"],
                "final_oos": "locked",
            }
        )
        mlflow.log_params(params)
        mlflow.log_metrics(metrics)
        for name in ("report.json", "report.md", "predictions.parquet"):
            mlflow.log_artifact(str(run_dir / name))
        mlflow.log_artifacts(str(run_dir / "model"), artifact_path="calibrated_bundle")
        name = "synthetic-trader-calibrated-bundle"
        try:
            client.get_registered_model(name)
        except MlflowException:
            client.create_registered_model(name, tags={"live_enabled": "false"})
        # Custom artifact bundle, не raw CatBoost без calibration/regime state.
        version = client.create_model_version(
            name,
            source=f"runs:/{run.info.run_id}/calibrated_bundle",
            run_id=run.info.run_id,
            tags={
                "stage": "research",
                "live_enabled": "false",
                "loader": "synthetic_trader.models.ProbabilityBundle",
                "model_hash": report["candidate_model_hash"],
            },
        )
        write_json(
            run_dir / "mlflow.json",
            {
                "run_id": run.info.run_id,
                "registry_name": name,
                "version": version.version,
                "backend": "local_sqlite",
            },
        )


def build_research_flow() -> Callable[[str, str], str]:
    """Lazy Prefect factory: default install does not require optional types/runtime."""
    try:
        from prefect import flow
    except ImportError as exc:
        raise ValueError("Prefect не установлен: uv sync --extra mlops") from exc

    decorator = cast(
        "Callable[[Callable[[str,str],str]],Callable[[str,str],str]]",
        flow(name="synthetic-trader-research", log_prints=False),
    )

    @decorator
    def execute(config_file: str, root: str) -> str:
        from synthetic_trader.config import ExperimentConfig
        from synthetic_trader.manager import ResearchManager
        from synthetic_trader.pipeline import run_experiment

        config = ExperimentConfig.model_validate_json(Path(config_file).read_text())
        ResearchManager(Path(root))  # shared recovery/mutex with CLI and GUI
        run_id = uuid4().hex
        directory = Path(root) / "runs" / run_id
        started = datetime.now(UTC)
        write_json(directory / "config.json", config.model_dump(mode="json"))

        def progress(stage: str, percent: int, detail: str) -> None:
            write_json(
                directory / "status.json",
                {
                    "id": run_id,
                    "status": "completed" if stage == "completed" else "running",
                    "stage": stage,
                    "progress": percent,
                    "detail": detail,
                    "started_at": started,
                    "updated_at": datetime.now(UTC),
                    "pid": os.getpid(),
                },
            )

        try:
            run_experiment(Path(root), run_id, config, progress)
        except Exception:
            write_json(
                directory / "status.json",
                {
                    "id": run_id,
                    "status": "failed",
                    "stage": "failed",
                    "detail": "Prefect research job failed; result is unavailable",
                    "started_at": started,
                    "updated_at": datetime.now(UTC),
                },
            )
            raise
        return run_id

    return execute


def research_flow(config_file: str, root: str = "data/research") -> str:
    return build_research_flow()(config_file, root)


def serve_research_schedule(config_file: str, cron: str, root: str = "data/research") -> None:
    """Opt-in deployment runner. Never enable a schedule / server by default."""
    from prefect import Flow

    scheduled = cast("Flow[Any, str]", build_research_flow())
    scheduled.serve(
        name="synthetic-trader-research",
        cron=cron,
        parameters={
            "config_file": str(Path(config_file).resolve()),
            "root": str(Path(root).resolve()),
        },
        pause_on_shutdown=True,
    )
