"""Контракты безопасности GUI: HTTP, CSRF, WS, SQL и Hard Stop.

Все проверки проходят через реальный FastAPI/JS-facing API. Один и тот же
контракт ожидается при разрыве WS и при прямом API-доступе.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from adapters.driving.web.app import create_app
from application.composition import AppContext
from application.scheduler import Scheduler, TaskSpec
from tests.integration.test_web_app import (
    duckdb_context as db_fixture,  # noqa: F401 — pytest fixture
)


def login(client: TestClient) -> tuple[str, str]:
    response = client.post("/api/auth/login", json={"password": "dev-only-insecure-secret"})
    assert response.status_code == 200
    token = response.json()["token"]
    page = client.get("/control")
    assert page.status_code == 200
    csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)"', page.text)
    assert csrf is not None
    return token, csrf.group(1)


@pytest.fixture
def client(context: AppContext) -> Any:
    with TestClient(create_app(context)) as http:
        yield http


def test_cookie_form_requires_csrf_even_without_websocket(
    client: TestClient, context: AppContext
) -> None:
    _, csrf = login(client)
    response = client.post("/control/pause")
    assert response.status_code == 403
    assert context.kill_switch is not None and not context.kill_switch.is_engaged

    response = client.post("/control/pause", headers={"X-Red-Bot-CSRF": csrf})
    assert response.status_code == 200
    assert context.kill_switch.is_engaged
    assert "заблокированы" in response.text
    assert any(
        entry.action == "POST /control/pause" and entry.outcome == "403"
        for entry in context.repository.gui_audit
    )  # type: ignore[attr-defined]


def test_audit_masks_account_id_and_never_logs_broker_token(
    client: TestClient,
    context: AppContext,
    monkeypatch: Any,
) -> None:
    from application.use_cases import manage_security

    writes: list[str] = []
    monkeypatch.setattr(manage_security, "store_token", writes.append)
    _, csrf = login(client)
    old_id = context.active_account_id
    response = client.post(
        "/security/token",
        data={"api_token": "NEW_SECRET_BROKER_VALUE"},
        headers={"X-Red-Bot-CSRF": csrf},
    )
    assert response.status_code == 200
    assert writes == ["NEW_SECRET_BROKER_VALUE"]
    assert "NEW_SECRET_BROKER_VALUE" not in response.text
    audit_entries = context.repository.gui_audit  # type: ignore[attr-defined]
    assert "NEW_SECRET_BROKER_VALUE" not in str(audit_entries)
    assert old_id not in str(context.repository.gui_audit)  # type: ignore[attr-defined]
    assert response.headers["HX-Trigger"] == "token-saved"
    assert context.restart_required


def test_account_change_requires_all_three_steps_and_masks_audit(
    client: TestClient,
    context: AppContext,
) -> None:
    from config.enums import ExecutionMode

    context.mode = ExecutionMode.SANDBOX
    _, csrf = login(client)
    headers = {"X-Red-Bot-CSRF": csrf}
    account = "NEW_MANAGED_ACCOUNT_654321"
    context.broker._accounts.append(  # type: ignore[attr-defined]
        {"id": account, "name": "Selected account", "status": 2, "type": 1, "is_current": False}
    )
    step2_url = "/risk/account/step/2"
    assert (
        client.post(step2_url, data={"new_account_id": account}, headers=headers).status_code == 422
    )
    assert client.post("/risk/account/step/1", headers=headers).status_code == 200
    step2 = client.post(step2_url, data={"new_account_id": account}, headers=headers)
    assert step2.status_code == 200
    assert account not in step2.text
    wrong = client.post("/risk/account/step/3", data={"confirmation": "НЕТ"}, headers=headers)
    assert wrong.status_code == 422
    done = client.post(
        "/risk/account/step/3", data={"confirmation": "СМЕНИТЬ СЧЁТ"}, headers=headers
    )
    assert done.status_code == 200
    assert account not in done.text
    assert context.restart_required
    assert context.active_account_id != account  # действующий брокер не переназначен
    assert account not in str(context.repository.gui_audit)  # type: ignore[attr-defined]
    key = f"managed_account_id:{context.execution_mode.value}"
    assert context.repository.operational_values[key] == account  # type: ignore[attr-defined]


def test_settings_store_next_mode_and_open_account_without_switching_runtime(
    client: TestClient,
    context: AppContext,
) -> None:
    from config.enums import ExecutionMode

    context.mode = ExecutionMode.SANDBOX
    _, csrf = login(client)
    headers = {"X-Red-Bot-CSRF": csrf}

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Настройки запуска" in page.text
    assert "Автоматически" in page.text

    response = client.post(
        "/settings/mode",
        data={"execution_mode": "live"},
        headers=headers,
    )
    assert response.status_code == 200
    operational_values = context.repository.operational_values  # type: ignore[attr-defined]
    assert operational_values["execution_mode"] == "live"
    assert context.execution_mode.value == "sandbox"

    response = client.post(
        "/settings/account",
        data={"account_id": "fake-account"},
        headers=headers,
    )
    assert response.status_code == 200
    key = f"managed_account_id:{context.execution_mode.value}"
    assert (
        context.repository.operational_values[key] == "fake-account"  # type: ignore[attr-defined]
    )

    invalid = client.post(
        "/settings/account",
        data={"account_id": "not-an-open-id"},
        headers=headers,
    )
    assert invalid.status_code == 400


def test_countertrend_requires_type_to_confirm_in_form_and_legacy_api(
    client: TestClient,
    context: AppContext,
) -> None:
    token, csrf = login(client)
    version = context.config.version
    data = {
        "multiplier": "2",
        "risk_percent": "1",
        "max_holding_hours": "48",
        "allow_counter_trend": "true",
        "confirmed_warning": "true",
    }
    denied = client.post("/risk/settings", data=data, headers={"X-Red-Bot-CSRF": csrf})
    assert denied.status_code == 422 and "РАЗРЕШИТЬ КОНТРТРЕНД" in denied.text
    assert context.config.version == version
    legacy = client.put(
        "/api/config", json={"allow_counter_trend": True}, headers={"X-Red-Bot-Token": token}
    )
    assert legacy.status_code == 409
    assert context.config.version == version
    allowed = client.post(
        "/risk/settings",
        data={**data, "confirmation": "РАЗРЕШИТЬ КОНТРТРЕНД"},
        headers={"X-Red-Bot-CSRF": csrf},
    )
    assert allowed.status_code == 200, allowed.text
    assert context.config.allow_counter_trend and context.config.version == version + 1


def test_hard_stop_latches_even_against_legacy_release_api(
    client: TestClient,
    context: AppContext,
) -> None:
    scheduler = Scheduler()

    async def noop() -> None:
        pass

    scheduler.add(TaskSpec(name="position_monitor", cycle=noop, interval_seconds=600))
    scheduler._running = True
    context.scheduler = scheduler
    token, csrf = login(client)
    headers = {"X-Red-Bot-CSRF": csrf}
    challenge = client.post("/security/kill/challenge", headers=headers)
    assert challenge.status_code == 200
    nonce = re.search(r'name="nonce" value="([^"]+)"', challenge.text)
    assert nonce is not None
    early = client.post(
        "/security/kill/confirm",
        data={"nonce": nonce.group(1), "confirmation": "ОСТАНОВИТЬ"},
        headers=headers,
    )
    assert early.status_code == 422
    assert scheduler.is_running
    session = client.app.state.sessions.get(token)
    assert session is not None
    session.kill_ready_at = datetime.now(tz=UTC) - timedelta(seconds=1)

    stopped = client.post(
        "/security/kill/confirm",
        data={"nonce": nonce.group(1), "confirmation": "ОСТАНОВИТЬ"},
        headers=headers,
    )
    assert stopped.status_code == 200
    assert context.hard_stop_latched
    assert not scheduler.is_running
    assert context.kill_switch is not None and context.kill_switch.is_engaged
    assert (
        client.post("/control/start", data={"confirmation": ""}, headers=headers).status_code == 200
    )
    # HTML сообщает об ошибке, а не повторно запускает Scheduler.
    assert "Hard Stop" in client.post("/control/start", headers=headers).text
    assert "Hard Stop" in client.post("/control/resume", headers=headers).text
    release = client.post(
        "/api/system/kill-switch", json={"engaged": False}, headers={"X-Red-Bot-Token": token}
    )
    assert release.status_code == 409
    assert context.kill_switch.is_engaged


def test_ws_cookie_and_origin_check(context: AppContext) -> None:
    with TestClient(create_app(context)) as http:
        with pytest.raises(WebSocketDisconnect) as unauth, http.websocket_connect("/ws"):
            pass
        assert unauth.value.code == 4401
        login(http)
        with (
            pytest.raises(WebSocketDisconnect) as other_origin,
            http.websocket_connect("/ws", headers={"origin": "https://other.example"}),
        ):
            pass
        assert other_origin.value.code == 4401
        with http.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            ws.send_json({"action": "subscribe", "channels": ["system.mode"]})
            msg = ws.receive_json()
            assert msg["channel"] == "system.mode"
            assert msg["type"] == "snapshot"
            assert msg["seq"] >= 0


def test_ws_replay_delivers_ordered_deltas_and_reports_gaps(client: TestClient) -> None:
    token, _ = login(client)
    hub = client.app.state.hub
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "subscribe", "channels": ["system.notifications"]})
        initial = ws.receive_json()
        assert initial["type"] == "snapshot" and initial["seq"] == 0
        first = client.portal.call(
            hub.publish,
            "system.notifications",
            "alert",
            {"level": "WARNING", "text": "Первый сигнал"},
        )
        second = client.portal.call(
            hub.publish,
            "system.notifications",
            "alert",
            {"level": "CRITICAL", "text": "Второй сигнал"},
        )
        assert ws.receive_json()["seq"] == first.seq
        assert ws.receive_json()["seq"] == second.seq
        replay = client.get(
            "/api/ws/replay?channel=system.notifications&since_seq=0",
            headers={"X-Red-Bot-Token": token},
        )
        assert replay.status_code == 200
        assert [event["seq"] for event in replay.json()["events"]] == [1, 2]
        # Кольцевой буфер ограничен: запрос старого seq требует snapshot, не
        # возвращает неполную историю как полную.
        hub._history["system.notifications"] = deque([second], maxlen=1)
        gap = client.get(
            "/api/ws/replay?channel=system.notifications&since_seq=0",
            headers={"X-Red-Bot-Token": token},
        )
        assert gap.status_code == 409


def test_csv_export_isolation_between_sessions(
    db_fixture: AppContext,
) -> None:  # noqa: F811 — pytest fixture
    with TestClient(create_app(db_fixture)) as a, TestClient(create_app(db_fixture)) as b:
        # Экспорт хранится в сессии GUI, не в URL с доступом для любого оператора.
        token_a, csrf = login(a)
        result = a.post(
            "/admin/storage/sql",
            data={"query": "SELECT count(*) FROM trades"},
            headers={"X-Red-Bot-CSRF": csrf},
        )
        assert result.status_code == 200, result.text
        match = re.search(r'href="(/admin/storage/sql/[a-f0-9]+\.csv)"', result.text)
        assert match is not None
        url = match.group(1)
        assert a.get(url, headers={"X-Red-Bot-Token": token_a}).status_code == 200
        login(b)
        assert b.get(url).status_code == 404
