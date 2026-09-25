"""FastAPI-приложение Web GUI.

Тонкая оболочка: вся логика — в слоях ``application`` и ``core``, здесь только
HTTP-маршрутизация, сессии и WebSocket-рассылка.

Дизайн под удалённый доступ:
* сервер биндится на ``0.0.0.0`` (иначе контейнер/прокси не пустит трафик);
* не используется ``TrustedHostMiddleware`` с жёстким allowlist — он ломает
  работу через внешний прокси с произвольным Host;
* все запросы frontend делает относительными (``/api/...``), без localhost.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from adapters.driving.web.dependencies import SESSION_HEADER, SessionManager
from adapters.driving.web.routers import admin, analysis, config, journal, system, trading
from adapters.driving.web.schemas import LoginRequest, LoginResponse
from adapters.driving.web.websocket import WebSocketHub, websocket_endpoint
from application.composition import AppContext

logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
API_PREFIX = "/api"


def create_app(
    context: AppContext,
    *,
    session_secret: str | None = None,
    hub: WebSocketHub | None = None,
) -> FastAPI:
    """Собирает приложение вокруг готового контекста."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.context = context
        app.state.sessions = SessionManager(
            session_secret or context.settings.web.session_secret.get_secret_value()
        )
        app.state.hub = hub or WebSocketHub()
        await app.state.hub.start()
        logger.info("web_app_started", mode=context.settings.execution_mode.value)
        try:
            yield
        finally:
            await app.state.hub.stop()
            logger.info("web_app_stopped")

    app = FastAPI(
        title="Red-Bot",
        version="0.1.0",
        description="Управление алгоритмической системой торговли (long-only, свинг/интрадей)",
        lifespan=lifespan,
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    # --- публичные ручки -------------------------------------------------
    @app.post(f"{API_PREFIX}/auth/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, request: Request) -> LoginResponse:
        manager: SessionManager = request.app.state.sessions
        if not manager.check_password(payload.password):
            raise HTTPException(status_code=401, detail="Неверный пароль")
        token, expires_at = manager.issue()
        return LoginResponse(token=token, expires_at=expires_at)

    # --- защищённые ручки -----------------------------------------------
    protected = APIRouter()
    protected.include_router(system.router)
    protected.include_router(trading.router)
    protected.include_router(journal.router)
    protected.include_router(analysis.router)
    protected.include_router(config.router)
    protected.include_router(admin.router)
    app.include_router(protected)

    # --- WebSocket -------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket.app.state.hub, websocket)

    # --- статика ---------------------------------------------------------
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/docs")

    @app.get(f"{API_PREFIX}/session-header")
    async def session_header() -> dict[str, str]:
        """Подсказка клиенту, в каком заголовке передавать токен."""
        return {"header": SESSION_HEADER}

    return app
