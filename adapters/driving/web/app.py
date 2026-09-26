"""FastAPI composition root Web GUI: SSR + HTMX + локальная сессия + WS."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from adapters.driving.web.dependencies import SESSION_HEADER, SessionManager, require_session
from adapters.driving.web.jobs import JobManager
from adapters.driving.web.render import render_page
from adapters.driving.web.routers import (
    admin,
    analysis,
    app_settings,
    backtest,
    chart,
    config,
    control,
    dashboard,
    db_admin,
    instruments,
    journal,
    risk,
    security,
    system,
    trading,
)
from adapters.driving.web.schemas import LoginRequest, LoginResponse
from adapters.driving.web.security.audit_middleware import GuiAuditMiddleware
from adapters.driving.web.security.log_stream import GuiLogHandler
from adapters.driving.web.security.session import COOKIE_NAME, websocket_session
from adapters.driving.web.ws.hub import BroadcastHub, websocket_endpoint
from adapters.driving.web.ws.replay import router as replay_router
from application.composition import AppContext
from application.events import DecisionRecorded
from application.use_cases.gui_views import decision_view
from application.use_cases.monitor_gui import channel_snapshot, resources
from application.use_cases.search_gui import search_gui
from core.domain.events import KillSwitchEngaged

logger = structlog.get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def _cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=12 * 3600,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
    )


def create_app(
    context: AppContext,
    *,
    session_secret: str | None = None,
    hub: BroadcastHub | None = None,
) -> FastAPI:
    """Собирает GUI поверх готового контекста приложения (без импорта driven)."""
    secret = session_secret or context.settings.web.session_secret.get_secret_value()
    if context.execution_mode.value == "live" and secret == "dev-only-insecure-secret":  # noqa: S105
        raise ValueError("LIVE требует отдельный пароль GUI в keyring/конфигурации")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.context = context
        app.state.sessions = SessionManager(secret)

        async def snapshot(channel: str) -> dict[str, Any] | list[Any]:
            if channel == "db_admin.jobs":
                return [
                    {
                        "id": j.id,
                        "kind": j.kind,
                        "status": j.status,
                        "detail": j.detail,
                        "started_at": j.started_at.isoformat(),
                    }
                    for j in app.state.jobs.jobs
                ]
            return await channel_snapshot(context, channel)

        app.state.hub = hub or BroadcastHub(context.repository, snapshot)
        await app.state.hub.start()
        app.state.jobs = JobManager(app.state.hub)
        app.state.logs = GuiLogHandler(
            app.state.hub,
            asyncio.get_running_loop(),
            context.settings.tbank.api_token.get_secret_value(),
        )
        app.state.logs.redact_secret(secret)
        logging.getLogger().addHandler(app.state.logs)

        async def on_decision(event: DecisionRecorded) -> None:
            record = event.record
            await app.state.hub.publish(
                "dashboard.decisions",
                "decision." + record.snapshot.decision.value,
                decision_view(record, context),
            )
            snapshot_data = await context.repository.get_market_snapshot(
                record.snapshot.market_snapshot_id
            )
            if snapshot_data is not None:
                from core.domain.enums import Timeframe

                candle = snapshot_data.ohlcv.get(Timeframe.M1)
                if candle is not None:
                    await app.state.hub.publish(
                        f"chart.{record.instrument_uid}",
                        "bar.update",
                        {
                            "time": int(candle.timestamp.timestamp()),
                            "open": str(candle.open),
                            "high": str(candle.high),
                            "low": str(candle.low),
                            "close": str(candle.close),
                        },
                    )
                if snapshot_data.orderbook is not None:
                    book = snapshot_data.orderbook
                    await app.state.hub.publish(
                        f"chart.{record.instrument_uid}",
                        "book.update",
                        {
                            "bids": [
                                {"price": str(level.price), "quantity": level.quantity}
                                for level in book.bids[:5]
                            ],
                            "asks": [
                                {"price": str(level.price), "quantity": level.quantity}
                                for level in book.asks[:5]
                            ],
                        },
                    )

        async def on_kill(event: KillSwitchEngaged) -> None:
            await app.state.hub.publish("system.tasks", "system.paused", {"reason": event.reason})
            await app.state.hub.publish(
                "system.notifications",
                "critical",
                {
                    "level": "CRITICAL",
                    "text": "Soft kill-switch активирован",
                },
            )

        context.event_bus.subscribe(DecisionRecorded, on_decision)
        context.event_bus.subscribe(KillSwitchEngaged, on_kill)

        async def heartbeat() -> None:
            counter = 0
            while True:
                await asyncio.sleep(2)
                if app.state.hub.connection_count:
                    await app.state.hub.publish(
                        "system.resources", "resources.updated", await resources(context)
                    )
                    if counter % 2 == 0:
                        from application.use_cases.gui_control import get_control_state

                        await app.state.hub.publish(
                            "system.tasks", "tasks.updated", get_control_state(context).tasks
                        )
                counter += 1

        beat = asyncio.create_task(heartbeat(), name="gui-heartbeat")
        logger.info("web_app_started", mode=context.execution_mode.value)
        try:
            yield
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            context.event_bus.unsubscribe(DecisionRecorded, on_decision)
            context.event_bus.unsubscribe(KillSwitchEngaged, on_kill)
            await app.state.jobs.stop()
            logging.getLogger().removeHandler(app.state.logs)
            await app.state.hub.stop()
            logger.info("web_app_stopped")

    app = FastAPI(title="Red-Bot Control Panel", version="0.2.0", lifespan=lifespan)
    app.add_middleware(GuiAuditMiddleware)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'"
        )
        if request.url.path not in {"/api/system/health"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Any:
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/login")
    async def login_page(request: Request) -> Any:
        if request.app.state.sessions.verify(request.cookies.get(COOKIE_NAME, "")):
            return RedirectResponse(url="/control", status_code=303)
        return render_page(request, "pages/login.html", title="Вход", section="login")

    def _check_login(request: Request, password: str) -> tuple[str, Any]:
        manager: SessionManager = request.app.state.sessions
        remote_addr = request.client.host if request.client else "local"
        if not manager.allow_login(remote_addr):
            raise HTTPException(status_code=429, detail="Слишком много попыток; подождите минуту")
        if not manager.check_password(password):
            manager.record_failure(remote_addr)
            raise HTTPException(status_code=401, detail="Неверный пароль GUI")
        return manager.issue()

    @app.post("/api/auth/login", response_model=LoginResponse)
    async def login_api(
        payload: LoginRequest, request: Request, response: Response
    ) -> LoginResponse:
        token, expires_at = _check_login(request, payload.password)
        _cookie(response, request, token)
        return LoginResponse(token=token, expires_at=expires_at)

    @app.post("/login")
    async def login_form(request: Request, password: str = Form(...)) -> Any:
        try:
            token, _ = _check_login(request, password)
        except HTTPException:
            return render_page(
                request,
                "pages/login.html",
                title="Вход",
                section="login",
                data={"error": "Неверный пароль или слишком много попыток"},
                status_code=401,
            )
        response = RedirectResponse(url="/control", status_code=303)
        _cookie(response, request, token)
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        from adapters.driving.web.security.session import require_session

        token = require_session(request)
        request.app.state.sessions.revoke(token)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/api/session-header")
    async def session_header() -> dict[str, str]:
        return {"header": SESSION_HEADER}

    @app.get("/api/search", dependencies=[Depends(require_session)])
    async def search(request: Request, q: str = "") -> dict[str, Any]:
        token = request.headers.get(SESSION_HEADER) or request.cookies.get(COOKIE_NAME, "")
        session = request.app.state.sessions.get(token)
        return {
            "entities": await search_gui(context, q),
            "actions": session.actions if session else [],
        }

    # Существующие JSON API сохраняются, SQL и мутации постепенно переводятся
    # на выделенные use cases. Новые страницы НЕ импортируют driven.
    legacy = APIRouter()
    for router in (
        system.router,
        trading.router,
        journal.router,
        analysis.router,
        config.router,
        admin.router,
    ):
        legacy.include_router(router)
    app.include_router(legacy)
    for router in (
        control.router,
        app_settings.router,
        dashboard.router,
        instruments.router,
        risk.router,
        chart.router,
        journal.page_router,
        backtest.router,
        db_admin.router,
        security.router,
        replay_router,
    ):
        app.include_router(router)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if websocket_session(websocket, websocket.app.state.sessions) is None:
            await websocket.close(code=4401)
            return
        await websocket_endpoint(websocket.app.state.hub, websocket)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
