"""Интеграционные тесты Web GUI."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from adapters.driving.web.app import create_app
from application.composition import AppContext
from application.use_cases.bootstrap_database import seed_instrument


def _login(client: Any) -> str:
    response = client.post("/api/auth/login", json={"password": "dev-only-insecure-secret"})
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


@pytest.fixture
def client(context: AppContext) -> Any:
    from fastapi.testclient import TestClient

    app = create_app(context)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def duckdb_context(settings: Any, clock: Any, config: Any, tmp_path: Any) -> Any:
    """Контекст на реальном DuckDB: нужен для SQL-консоли и метрик хранилища."""
    from adapters.driven.storage.duckdb_repository import DuckDBRepository
    from adapters.driven.storage.parquet_archive import ParquetArchive
    from application.composition import AppContext
    from application.events import EventBus
    from application.kill_switch import KillSwitch
    from tests.fakes import FakeBroker, FakeMarketData

    settings.storage.data_dir = tmp_path / "data"
    repository = DuckDBRepository(tmp_path / "data" / "web.duckdb", memory_limit_mb=256, threads=1)
    event_bus = EventBus()

    context = AppContext(
        settings=settings,
        market_data=FakeMarketData(),
        broker=FakeBroker(),
        repository=repository,
        archive=ParquetArchive(repository=repository, archive_dir=tmp_path / "archive"),
        clock=clock,
        notifier=None,
        event_bus=event_bus,
        config=config,
        instruments=[],
        portfolio=None,
        kill_switch=KillSwitch(clock=clock, event_bus=event_bus),
        started_at=clock.now(),
    )
    yield context
    await repository.aclose()


@pytest.fixture
def duckdb_client(duckdb_context: Any) -> Any:
    from fastapi.testclient import TestClient

    with TestClient(create_app(duckdb_context)) as test_client:
        yield test_client


async def test_health_is_public(client: Any) -> None:
    response = client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_protected_endpoints_require_session(client: Any) -> None:
    assert client.get("/api/trading/plans").status_code == 401
    assert client.get("/api/config").status_code == 401


async def test_login_issues_token(client: Any) -> None:
    token = _login(client)
    assert token


async def test_login_rejects_wrong_password(client: Any) -> None:
    response = client.post("/api/auth/login", json={"password": "неверный"})
    assert response.status_code == 401


async def test_status_returns_context_state(client: Any, context: AppContext) -> None:
    token = _login(client)
    response = client.get("/api/system/status", headers={"X-Red-Bot-Token": token})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["execution_mode"] == "backtest"
    assert payload["config_version"] >= 1
    assert payload["kill_switch_engaged"] is False


async def test_kill_switch_toggle(client: Any, context: AppContext) -> None:
    token = _login(client)
    response = client.post(
        "/api/system/kill-switch",
        json={"engaged": True, "reason": "тест"},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 200, response.text
    assert context.kill_switch is not None
    assert context.kill_switch.is_engaged

    client.post(
        "/api/system/kill-switch",
        json={"engaged": False},
        headers={"X-Red-Bot-Token": token},
    )
    assert not context.kill_switch.is_engaged


async def test_config_get_and_update(client: Any, context: AppContext) -> None:
    token = _login(client)
    headers = {"X-Red-Bot-Token": token}

    current = client.get("/api/config", headers=headers)
    assert current.status_code == 200
    version = current.json()["version"]

    updated = client.put(
        "/api/config",
        json={"risk_per_trade_pct": "0.02", "max_holding_hours": 48},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["version"] == version + 1
    assert Decimal(payload["risk_per_trade_pct"]) == Decimal("0.02")
    assert payload["max_holding_hours"] == 48
    # Непереданные поля не обнуляются
    assert payload["min_viable_target_multiplier"] == current.json()["min_viable_target_multiplier"]


async def test_config_rejects_unknown_field(client: Any) -> None:
    token = _login(client)
    response = client.put(
        "/api/config",
        json={"risk_per_trade_pfffft": "0.02"},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 422


async def test_config_rejects_out_of_range(client: Any) -> None:
    token = _login(client)
    response = client.put(
        "/api/config",
        json={"risk_per_trade_pct": "0.9"},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 422


async def test_plans_endpoint_lists_plans(client: Any, context: AppContext) -> None:
    from application.use_cases.make_decision import make_decision

    await context.repository.save_instrument(seed_instrument("uid-sber", "SBER", 10))
    context.instruments = await context.repository.list_instruments()
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.plan is not None

    token = _login(client)
    response = client.get("/api/trading/plans", headers={"X-Red-Bot-Token": token})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload, "созданный план обязан быть виден в GUI"
    assert payload[0]["ticker"] == "SBER"


async def test_sql_console_allows_select(duckdb_client: Any) -> None:
    token = _login(duckdb_client)
    response = duckdb_client.post(
        "/api/admin/sql",
        json={"query": "SELECT count(*) FROM trades", "row_limit": 10},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [[0]]


async def test_sql_console_blocks_mutation(client: Any) -> None:
    token = _login(client)
    response = client.post(
        "/api/admin/sql",
        json={"query": "DELETE FROM trades"},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 400


async def test_sql_console_blocks_injection_through_semicolon(client: Any) -> None:
    token = _login(client)
    response = client.post(
        "/api/admin/sql",
        json={"query": "SELECT 1; DROP TABLE trades"},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 400


async def test_hypotheses_apply_requires_confirmation(client: Any, context: AppContext) -> None:
    from core.journal.hypothesis_engine import Hypothesis

    hypothesis = Hypothesis.create(
        text="тест",
        condition_description="x",
        sample_size=40,
        confidence=Decimal("0.9"),
    )
    from core.domain.enums import HypothesisStatus

    hypothesis.status = HypothesisStatus.CONFIRMED
    await context.repository.save_hypothesis(hypothesis)

    token = _login(client)
    response = client.post(
        "/api/journal/hypotheses/apply",
        json={"hypothesis_id": str(hypothesis.id), "confirmed_by_user": False},
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 400


async def test_hypotheses_apply_with_confirmation(client: Any, context: AppContext) -> None:
    from core.domain.enums import HypothesisStatus
    from core.journal.hypothesis_engine import Hypothesis

    hypothesis = Hypothesis.create(
        text="тест",
        condition_description="x",
        sample_size=40,
        confidence=Decimal("0.9"),
    )
    hypothesis.status = HypothesisStatus.CONFIRMED
    hypothesis.walk_forward_efficiency = Decimal("0.8")
    await context.repository.save_hypothesis(hypothesis)

    token = _login(client)
    response = client.post(
        "/api/journal/hypotheses/apply",
        json={
            "hypothesis_id": str(hypothesis.id),
            "confirmed_by_user": True,
            "confirmation": "ОДОБРИТЬ ГИПОТЕЗУ",
        },
        headers={"X-Red-Bot-Token": token},
    )
    assert response.status_code == 200, response.text


async def test_websocket_endpoint_accepts_connection(client: Any) -> None:
    _login(client)
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"action": "subscribe", "channels": ["system.mode"]})
        response = websocket.receive_json()
        assert response["channel"] == "system.mode"
        assert response["type"] == "snapshot"


async def test_gui_pages_render(client: Any, context: AppContext) -> None:
    assert client.get("/control", follow_redirects=False).status_code == 303
    _login(client)
    await context.repository.save_instrument(seed_instrument("uid-sber", "SBER", 10))
    context.instruments = await context.repository.list_instruments()
    for path in (
        "/control",
        "/risk",
        "/security",
        "/admin/storage",
        "/",
        "/instruments",
        "/chart/uid-sber",
        "/journal",
        "/backtest",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:600]}"
        assert "Red-Bot Control Panel" in response.text, path
        assert "/static/css/app.css" in response.text, path


async def test_storage_endpoint_reports_usage(client: Any, duckdb_client: Any) -> None:
    token = _login(duckdb_client)
    response = duckdb_client.get("/api/admin/storage", headers={"X-Red-Bot-Token": token})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "hot" in payload["usage_by_layer"]
    assert payload["total_bytes"] >= 0


async def test_trades_endpoint_empty_history(client: Any) -> None:
    token = _login(client)
    response = client.get("/api/journal/trades?since_days=1", headers={"X-Red-Bot-Token": token})
    assert response.status_code == 200
    assert response.json() == []


async def test_instruments_add_and_delete_endpoints(client: Any, context: AppContext) -> None:
    token = _login(client)
    headers = {"X-Red-Bot-Token": token}

    # Добавление инструмента через форму
    resp = client.post(
        "/instruments/add", data={"ticker": "VTBR", "class_code": "TQBR"}, headers=headers
    )
    assert resp.status_code == 200
    assert "VTBR" in resp.text
    assert "успешно добавлен" in resp.text

    # Повторное добавление возвращает понятную ошибку без 500
    resp_dup = client.post(
        "/instruments/add", data={"ticker": "VTBR", "class_code": "TQBR"}, headers=headers
    )
    assert resp_dup.status_code == 200
    assert "уже добавлен" in resp_dup.text

    # Каталог API
    resp_cat = client.get("/api/instruments/catalog", headers=headers)
    assert resp_cat.status_code == 200
    assert any(item["ticker"] == "VTBR" for item in resp_cat.json())

    # Удаление инструмента
    added_inst = next((i for i in context.instruments if i.ticker == "VTBR"), None)
    assert added_inst is not None
    resp_del = client.post(f"/instruments/{added_inst.uid}/delete", headers=headers)
    assert resp_del.status_code == 200
    assert "удалён из корзины" in resp_del.text


async def test_sandbox_account_web_endpoints(client: Any, context: AppContext) -> None:
    token = _login(client)
    headers = {"X-Red-Bot-Token": token}

    # Пополнение счёта
    resp_topup = client.post(
        "/risk/account/sandbox/topup", data={"amount": "300000"}, headers=headers
    )
    assert resp_topup.status_code == 200
    assert "пополнен" in resp_topup.text

    # Создание счёта
    resp_create = client.post(
        "/risk/account/sandbox/create", data={"name": "Новый счёт"}, headers=headers
    )
    assert resp_create.status_code == 200
    assert "Создан новый счёт" in resp_create.text

    # Обновление
    resp_ref = client.post("/risk/account/sandbox/refresh", headers=headers)
    assert resp_ref.status_code == 200
    assert "обновлены" in resp_ref.text
