"""DI для HTTP-роутеров; web вызывает только use cases приложения."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from adapters.driving.web.security.session import (
    SESSION_HEADER,
    SessionManager,
    get_session_manager,
    require_session,
)
from application.composition import AppContext


def get_context(request: Request) -> AppContext:
    context: AppContext | None = getattr(request.app.state, "context", None)
    if context is None:
        raise HTTPException(status_code=503, detail="Контекст приложения не инициализирован")
    return context


ContextDep = Annotated[AppContext, Depends(get_context)]
SessionDep = Annotated[str, Depends(require_session)]

__all__ = [
    "SESSION_HEADER",
    "ContextDep",
    "SessionDep",
    "SessionManager",
    "get_context",
    "get_session_manager",
    "require_session",
]
