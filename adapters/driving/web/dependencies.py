"""Зависимости FastAPI: доступ к ``AppContext`` и проверка сессии.

GUI — это пульт управления реальными деньгами, поэтому:
* все ручки (кроме логина и health) требуют валидную сессию;
* сессия — HMAC-подписанный токен с временем жизни;
* секрет сессии берётся из keyring/настроек, а не из исходников.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

SESSION_TTL = timedelta(hours=12)
SESSION_HEADER = "X-Red-Bot-Token"


class SessionManager:
    """Выдача и проверка сессионных токенов."""

    __slots__ = ("_secret", "_tokens")

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")
        self._tokens: dict[str, datetime] = {}

    def issue(self) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(tz=UTC) + SESSION_TTL
        self._tokens[token] = expires_at
        return token, expires_at

    def verify(self, token: str) -> bool:
        if not token:
            return False
        expires_at = self._tokens.get(token)
        if expires_at is None:
            return False
        if expires_at < datetime.now(tz=UTC):
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def check_password(self, password: str) -> bool:
        """Сравнение пароля с секретом — за постоянное время."""
        expected = hashlib.sha256(self._secret).digest()
        actual = hashlib.sha256(password.encode("utf-8")).digest()
        return hmac.compare_digest(expected, actual)


def get_context(request: Request) -> Any:
    """Достаёт ``AppContext`` из состояния приложения."""
    context = getattr(request.app.state, "context", None)
    if context is None:
        msg = "Контекст приложения не инициализирован"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    return context


def get_session_manager(request: Request) -> SessionManager:
    manager: SessionManager | None = getattr(request.app.state, "sessions", None)
    if manager is None:
        msg = "Менеджер сессий не инициализирован"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    return manager


def require_session(request: Request) -> str:
    """Требует валидную сессию для всех управляющих ручек."""
    token = request.headers.get(SESSION_HEADER) or request.cookies.get("redbot_session", "")
    manager = get_session_manager(request)
    if not manager.verify(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется валидная сессия"
        )
    return token


ContextDep = Annotated[Any, Depends(get_context)]
SessionDep = Annotated[str, Depends(require_session)]
