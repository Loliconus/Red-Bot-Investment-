"""Исследовательский CLI без токенов и без торгового scheduler."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer

research_cli = typer.Typer(
    help="Синтетический трейдер: offline/demo, MOEX исследования и безопасный GUI"
)


@research_cli.command("serve")
def serve(host: str = "0.0.0.0", port: int = 8000, data_dir: Path = Path("data")) -> None:  # noqa: S104 — preview/container
    """GUI без INVEST_TOKEN, подключения к брокеру и decision scheduler."""
    asyncio.run(_serve(host, port, data_dir))


async def _serve(host: str, port: int, data_dir: Path) -> None:
    import uvicorn
    from pydantic import SecretStr

    from adapters.driving.web.app import create_app
    from application.composition import build_context
    from config.enums import ExecutionMode
    from config.settings import Settings, StorageSettings, TBankSettings

    settings = Settings(
        execution_mode=ExecutionMode.BACKTEST,
        tbank=TBankSettings(api_token=SecretStr("")),
        storage=StorageSettings(data_dir=data_dir),
    )
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)
    typer.echo("RESEARCH ONLY · broker OFF · http://<host>:" + str(port) + "/backtest")
    try:
        await uvicorn.Server(
            uvicorn.Config(
                create_app(context, research_only=True), host=host, port=port, log_level="info"
            )
        ).serve()
    finally:
        await context.aclose()


@research_cli.command("run")
def run(
    config_file: Path = typer.Argument(..., exists=True, readable=True),
    root: Path = Path("data/research"),
) -> None:
    """Версионированный JSON config → полный воспроизводимый эксперимент."""
    from synthetic_trader.config import ExperimentConfig

    config = ExperimentConfig.model_validate_json(config_file.read_text())
    _run(config, root)


@research_cli.command("demo")
def demo(
    root: Path = Path("data/research"),
    interval: str = "1h",
    iterations: int = 120,
    cpcv: bool = True,
) -> None:
    """Искусственные данные для проверки инфраструктуры; не доказательство alpha."""
    from synthetic_trader.config import ExperimentConfig

    _run(
        ExperimentConfig.model_validate(
            {"interval": interval, "iterations": iterations, "cpcv": cpcv}
        ),
        root,
    )


def _run(config: Any, root: Path) -> None:
    from synthetic_trader.manager import ResearchManager
    from synthetic_trader.pipeline import run_experiment
    from synthetic_trader.storage import write_json

    ResearchManager(root)  # recover dead CLI/GUI jobs without reading frozen prices
    run_id = uuid4().hex
    directory = root / "runs" / run_id
    write_json(directory / "config.json", config.model_dump(mode="json"))

    def progress(stage: str, percent: int, detail: str) -> None:
        from datetime import UTC, datetime

        typer.echo(f"{percent:3}% · {stage}: {detail}")
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
                "source": config.source,
                "pid": os.getpid(),
            },
        )

    from datetime import UTC, datetime

    started = datetime.now(UTC).isoformat()
    try:
        report = run_experiment(root, run_id, config, progress)
    except (ValueError, OSError) as exc:
        write_json(
            directory / "status.json",
            {
                "id": run_id,
                "status": "failed",
                "stage": "failed",
                "progress": 0,
                "detail": str(exc),
                "started_at": started,
            },
        )
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    typer.echo(f"Report: {directory / 'report.json'}")


@research_cli.command("final")
def final(
    run_id: str,
    root: Path = Path("data/research"),
    confirmation: str = typer.Option(..., prompt="Введите ОТКРЫТЬ FINAL OOS"),
) -> None:
    """Одноразовый final OOS фиксированной модели. Нельзя отменить раскрытие."""
    import re

    from synthetic_trader.pipeline import run_final

    if confirmation != "ОТКРЫТЬ FINAL OOS" or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise typer.BadParameter("Нужны корректный run_id и точное подтверждение")
    report = run_final(
        root, run_id, lambda stage, percent, detail: typer.echo(f"{percent}% {detail}")
    )
    typer.echo(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


@research_cli.command("schedule")
def schedule(
    config_file: Path = typer.Argument(..., exists=True, readable=True),
    cron: str = typer.Option(..., help="Явное расписание Prefect, например 0 2 * * 1"),
    root: Path = Path("data/research"),
) -> None:
    """Opt-in Prefect runner, research only; не открывает final / не торгует."""
    from synthetic_trader.mlops import serve_research_schedule

    serve_research_schedule(str(config_file), cron, str(root))
