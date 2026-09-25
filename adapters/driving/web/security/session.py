"""Сессия GUI: HttpOnly cookie + CSRF для браузера, header для API-клиентов.

Пароль GUI не является токеном брокера. Все состояния подтверждения живут лишь
в памяти сессии и протухают, не попадают в HTML или БД до финального шага.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, WebSocket, status

SESSION_TTL = timedelta(hours=12)
DRAFT_TTL = timedelta(minutes=5)
SESSION_HEADER = "X-Red-Bot-Token"
CSRF_HEADER = "X-Red-Bot-CSRF"
COOKIE_NAME = "redbot_session"


@dataclass(slots=True)
class SessionData:
    expires_at: datetime
    account_draft: str | None = None
    draft_expires_at: datetime | None = None
    actions: list[dict[str, str]] = field(default_factory=list)
    sql_history: list[str] = field(default_factory=list)
    sql_exports: dict[str, Any] = field(default_factory=dict)
    kill_nonce: str | None = None
    kill_ready_at: datetime | None = None
    kill_expires_at: datetime | None = None


class SessionManager:
    __slots__ = ("_failures", "_secret", "_sessions")

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")
        self._sessions: dict[str, SessionData] = {}
        self._failures: dict[str, list[datetime]] = {}

    def issue(self) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(tz=UTC) + SESSION_TTL
        self._sessions[token] = SessionData(expires_at=expires_at)
        return token, expires_at

    def get(self, token: str) -> SessionData | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= datetime.now(tz=UTC):
            self._sessions.pop(token, None)
            return None
        return session

    def verify(self, token: str) -> bool:
        return self.get(token) is not None

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def check_password(self, password: str) -> bool:
        expected = hashlib.sha256(self._secret).digest()
        actual = hashlib.sha256(password.encode("utf-8")).digest()
        return hmac.compare_digest(expected, actual)

    def allow_login(self, remote_addr: str) -> bool:
        now = datetime.now(tz=UTC)
        self._failures[remote_addr] = [
            at for at in self._failures.get(remote_addr, ()) if now - at < timedelta(minutes=1)
        ]
        return len(self._failures[remote_addr]) < 5

    def record_failure(self, remote_addr: str) -> None:
        self._failures.setdefault(remote_addr, []).append(datetime.now(tz=UTC))

    def csrf(self, token: str) -> str:
        return hmac.new(self._secret, token.encode(), hashlib.sha256).hexdigest()

    def verify_csrf(self, token: str, candidate: str) -> bool:
        return bool(
            candidate and self.verify(token) and hmac.compare_digest(self.csrf(token), candidate)
        )

    def draft_account(self, token: str, value: str) -> None:
        session = self.get(token)
        if session is None:
            raise ValueError("Сессия истекла")
        session.account_draft = value
        session.draft_expires_at = datetime.now(tz=UTC) + DRAFT_TTL

    def take_account_draft(self, token: str) -> str:
        session = self.get(token)
        if (
            session is None
            or session.account_draft is None
            or session.draft_expires_at is None
            or session.draft_expires_at <= datetime.now(tz=UTC)
        ):
            raise ValueError("Шаг смены счёта истёк: начните заново")
        value = session.account_draft
        session.account_draft = None
        session.draft_expires_at = None
        return value

    def issue_kill_challenge(self, token: str) -> str:
        session = self.get(token)
        if session is None:
            raise ValueError("Сессия истекла")
        session.kill_nonce = secrets.token_urlsafe(24)
        session.kill_ready_at = datetime.now(tz=UTC) + timedelta(seconds=3)
        session.kill_expires_at = datetime.now(tz=UTC) + DRAFT_TTL
        return session.kill_nonce

    def consume_kill_challenge(self, token: str, nonce: str) -> None:
        session = self.get(token)
        if (
            session is None
            or not session.kill_nonce
            or not hmac.compare_digest(session.kill_nonce, nonce)
        ):
            raise ValueError("Откройте подтверждение kill-switch заново")
        now = datetime.now(tz=UTC)
        if session.kill_expires_at is None or now >= session.kill_expires_at:
            raise ValueError("Подтверждение kill-switch истекло")
        if session.kill_ready_at is None or now < session.kill_ready_at:
            raise ValueError("Подождите не менее 3 секунд")
        session.kill_nonce = None
        session.kill_ready_at = None
        session.kill_expires_at = None

    def remember_action(self, token: str, label: str, url: str) -> None:
        session = self.get(token)
        if session is not None:
            session.actions = [{"label": label, "url": url}, *session.actions][:10]

    def remember_sql(self, token: str, query: str) -> None:
        session = self.get(token)
        if session is not None:
            session.sql_history = ([query] + [q for q in session.sql_history if q != query])[:20]


def get_session_manager(request: Request) -> SessionManager:
    manager: SessionManager | None = getattr(request.app.state, "sessions", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Менеджер сессий не инициализирован")
    return manager


def require_session(request: Request) -> str:
    """Cookie-запросы с мутацией обязаны иметь CSRF; явный header — API доступ."""
    token = request.headers.get(SESSION_HEADER)
    via_cookie = token is None
    token = token or request.cookies.get(COOKIE_NAME, "")
    manager = get_session_manager(request)
    if not manager.verify(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется сессия")
    if (
        via_cookie
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not manager.verify_csrf(token, request.headers.get(CSRF_HEADER, ""))
    ):
        raise HTTPException(status_code=403, detail="Отсутствует или неверен CSRF-токен")
    return token


def websocket_session(websocket: WebSocket, manager: SessionManager) -> str | None:
    origin = websocket.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc.lower() != websocket.headers.get("host", "").lower():
            return None
    token = websocket.cookies.get(COOKIE_NAME, "")
    return token if manager.verify(token) else None
