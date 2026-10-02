from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from adapters.driving.web.app import create_app
from synthetic_trader.config import ExperimentConfig
from synthetic_trader.manager import ResearchManager
from synthetic_trader.storage import write_json


@pytest.fixture
def research_client(context, tmp_path):
    manager = ResearchManager(tmp_path / "research")
    context.research = manager
    with TestClient(create_app(context)) as client:
        yield client, manager


def session(client):
    response = client.post("/api/auth/login", json={"password": "dev-only-insecure-secret"})
    assert response.status_code == 200
    return {"X-Red-Bot-Token": response.json()["token"]}


def test_research_page_and_artifacts_require_existing_session(research_client):
    client, _ = research_client
    assert client.get("/backtest", follow_redirects=False).status_code == 303
    assert client.get("/api/backtest/runs").status_code == 401
    assert (
        client.get("/api/backtest/runs/" + ("a" * 32) + "/artifacts/report.json").status_code == 401
    )
    headers = session(client)
    response = client.get("/backtest", headers=headers)
    assert response.status_code == 200 and "P(up | trend)" in response.text
    assert "/static/js/research.js" in response.text and "REAL ORDERS OFF" in response.text
    assert client.get("/synthetic", headers=headers).status_code == 200


def test_csrf_schema_and_no_broker_mutation(research_client, context, monkeypatch):
    client, manager = research_client
    headers = session(client)
    spy = AsyncMock(return_value={"id": "a" * 32, "status": "running"})
    monkeypatch.setattr(manager, "launch", spy)
    # Cookie requests require CSRF, even in BACKTEST mode.
    assert client.post("/api/backtest/runs", json={}).status_code == 403
    assert not spy.called
    assert (
        client.post("/api/backtest/runs", headers=headers, json={"source": "live"}).status_code
        == 422
    )
    assert not spy.called
    response = client.post(
        "/api/backtest/runs", headers=headers, json=ExperimentConfig().model_dump(mode="json")
    )
    assert response.status_code == 202 and spy.call_count == 1
    assert not context.broker.placed


def test_final_exact_confirmation_and_whitelisted_artifacts(research_client, monkeypatch):
    client, manager = research_client
    headers = session(client)
    run_id = uuid4().hex
    directory = manager.root / "runs" / run_id
    write_json(directory / "config.json", ExperimentConfig().model_dump(mode="json"))
    write_json(directory / "status.json", {"id": run_id, "status": "completed"})
    write_json(directory / "report.json", {"run_id": run_id})
    spy = AsyncMock(return_value={"id": run_id, "status": "running"})
    monkeypatch.setattr(manager, "finalize", spy)
    assert (
        client.post(
            f"/api/backtest/runs/{run_id}/final", headers=headers, json={"confirmation": "yes"}
        ).status_code
        == 409
    )
    assert not spy.called
    response = client.post(
        f"/api/backtest/runs/{run_id}/final",
        headers=headers,
        json={"confirmation": "ОТКРЫТЬ FINAL OOS"},
    )
    assert response.status_code == 202 and spy.call_count == 1
    assert (
        client.get(
            f"/api/backtest/runs/{run_id}/artifacts/report.json", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/backtest/runs/{run_id}/artifacts/config.json", headers=headers
        ).status_code
        == 404
    )
    assert client.get("/api/backtest/runs/not-a-uuid", headers=headers).status_code == 404
