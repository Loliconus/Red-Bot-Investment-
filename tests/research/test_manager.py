import asyncio
import json

import pytest

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.manager import ResearchBusyError, ResearchManager
from synthetic_trader.storage import write_json


async def test_actual_subprocess_whitelist_busy_cancel_and_cleanup(tmp_path, monkeypatch):
    manager = ResearchManager(tmp_path)
    monkeypatch.setenv("INVEST_TOKEN", "not-a-real-test-credential")
    monkeypatch.setenv("REDBOT_WEB__SESSION_SECRET", "private-test-value")
    actual = asyncio.create_subprocess_exec
    captured = {}

    async def spy(*args, **kwargs):
        captured.update(kwargs["env"])
        return await actual(*args, **kwargs)

    monkeypatch.setattr("synthetic_trader.manager.asyncio.create_subprocess_exec", spy)
    try:
        run = await manager.launch(
            ExperimentConfig(iterations=40, interval="1d", cpcv=False).model_dump(mode="json")
        )
        assert run["status"] == "running" and run["pid"] > 0
        assert "INVEST_TOKEN" not in captured and "REDBOT_WEB__SESSION_SECRET" not in captured
        with pytest.raises(ResearchBusyError):
            await manager.launch(ExperimentConfig().model_dump(mode="json"))
        cancelled = await manager.cancel(run["id"])
        assert cancelled["status"] == "cancelled"
    finally:
        await manager.aclose()
    assert not (tmp_path / "worker.lock").exists()


async def test_actual_worker_completes_no_pid_status_overwrite(tmp_path):
    manager = ResearchManager(tmp_path)
    try:
        run = await manager.launch(
            ExperimentConfig(
                iterations=40, folds=2, interval="1d", cpcv=False, bootstrap_reps=100
            ).model_dump(mode="json")
        )
        await asyncio.wait_for(asyncio.gather(*manager._watchers), timeout=75)
        finished = manager.get(run["id"])
        assert finished["status"] == "completed" and finished["progress"] == 100
        assert finished["report"]["source"] == "demo" and not finished["report"]["live_enabled"]
        assert not (tmp_path / "worker.lock").exists()
        with pytest.raises(ValueError):
            manager.artifact(run["id"], "../config.json")
        assert manager.artifact(run["id"], "report.json").endswith("report.json")
    finally:
        await manager.aclose()


def test_dead_workers_recover_including_final_status(tmp_path):
    directory = tmp_path / "runs" / ("a" * 32)
    write_json(directory / "status.json", {"id": "a" * 32, "status": "completed", "pid": None})
    write_json(directory / "final_status.json", {"id": "a" * 32, "status": "running", "pid": None})
    write_json(tmp_path / "worker.lock", {"owner_pid": None, "pid": None})
    ResearchManager(tmp_path)
    assert json.loads((directory / "status.json").read_text())["status"] == "completed"
    assert json.loads((directory / "final_status.json").read_text())["status"] == "interrupted"
    assert not (tmp_path / "worker.lock").exists()
