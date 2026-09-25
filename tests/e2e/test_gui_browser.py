"""Browser E2E: реальные JS/HTMX/Alpine + API + cookie + WS.

Запуск: `playwright install chromium && pytest -m e2e`. Если Chromium не
установлен (например, ограниченная сеть CI), тесты честно пропускаются.
REDBOT_E2E_CHROME позволяет указать свой совместимый Chromium.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn

from adapters.driving.web.app import create_app
from application.composition import AppContext
from application.scheduler import Scheduler, TaskSpec

pytestmark = pytest.mark.e2e


@pytest.fixture
def live_gui(context: AppContext) -> Iterator[str]:
    """Поднимем настоящую ASGI-сеть на свободном локальном порту."""
    scheduler = Scheduler()

    async def no_op() -> None:
        return

    scheduler.add(TaskSpec(name="decisions", cycle=no_op, interval_seconds=3600))
    scheduler.add(TaskSpec(name="position_monitor", cycle=no_op, interval_seconds=3600))
    scheduler._running = True
    context.scheduler = scheduler
    app = create_app(context)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "Сервер GUI не запустился"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Сервер GUI не остановился"


@pytest.fixture
def browser() -> Iterator[Any]:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            chrome = runtime.chromium.launch(
                headless=True,
                executable_path=os.environ.get("REDBOT_E2E_CHROME") or None,
                args=["--no-sandbox"],
            )
        except playwright.Error as exc:
            pytest.skip(f"Нужен установленный Chromium для e2e: {str(exc)[:160]}")
        try:
            yield chrome
        finally:
            chrome.close()


def test_login_htmx_ws_and_risk_form(browser: Any, live_gui: str) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(live_gui + "/control")
    assert "/login" in page.url
    page.get_by_label("Пароль сессии").fill("dev-only-insecure-secret")
    page.get_by_role("button", name="Открыть панель").click()
    page.wait_for_url("**/control")
    page.get_by_text("WS ONLINE").wait_for(timeout=10000)
    assert page.context.cookies()[0]["httpOnly"]
    page.get_by_role("button", name="Приостановить").click()
    page.get_by_text("Новые входы заблокированы").wait_for()
    page.get_by_role("button", name="Снять паузу").click()
    page.get_by_text("Открытие новых планов разрешено").wait_for()
    page.goto(live_gui + "/risk")
    assert page.get_by_role("heading", name="Риск-модуль").is_visible()
    assert not errors, errors
    page.close()


def test_browser_hard_stop_has_server_delay_and_cannot_be_released(
    browser: Any,
    live_gui: str,
) -> None:
    page = browser.new_page(viewport={"width": 1360, "height": 860})
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(live_gui + "/login")
    page.get_by_label("Пароль сессии").fill("dev-only-insecure-secret")
    page.get_by_role("button", name="Открыть панель").click()
    page.wait_for_url("**/control")
    page.goto(live_gui + "/security")
    page.get_by_role("button", name="ОСТАНОВИТЬ БОТА").click()
    modal = page.get_by_role("dialog", name="Подтверждение остановки")
    modal.get_by_placeholder("ОСТАНОВИТЬ").fill("ОСТАНОВИТЬ")
    confirm = modal.get_by_role("button", name="Подтвердить")
    assert confirm.is_disabled()
    confirm.wait_for(state="visible")
    page.wait_for_timeout(3200)
    assert confirm.is_enabled()
    confirm.click()
    page.get_by_text("Бот остановлен").wait_for()
    page.goto(live_gui + "/control")
    assert page.get_by_role("button", name="Запустить бот").is_disabled()
    assert not errors, errors
    page.close()
