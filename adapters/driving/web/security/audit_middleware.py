"""Сквозной append-only аудит всех изменяющих HTTP-запросов.

Тела запросов не читаются и не логируются: они могут содержать API-токен,
пароль GUI или номер счёта. До/после снимаются только с безопасных полей
контекста, а роутеры могут дополнить их замаскированными деталями.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from adapters.driving.web.security.session import COOKIE_NAME, SESSION_HEADER
from application.composition import AppContext
from application.use_cases.manage_risk import mask_account
from core.ports.persistence import GuiAuditEntry


class GuiAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await call_next(request)
        context: AppContext | None = getattr(request.app.state, "context", None)
        before = _safe_state(context) if context is not None else {}
        try:
            response = await call_next(request)
        except Exception:
            await self._record(request, context, before, "500")
            raise
        await self._record(request, context, before, str(response.status_code))
        return response

    async def _record(
        self,
        request: Request,
        context: AppContext | None,
        before: dict[str, str],
        outcome: str,
    ) -> None:
        if context is None:
            return
        path = request.url.path
        parts = path.strip("/").split("/")
        section = parts[1] if parts[0] == "api" and len(parts) > 1 else parts[0]
        after = _safe_state(context)
        # Никаких произвольных значений от клиента; только маркированные поля
        # состояния, причём даже в случае ошибки они записываются отдельно.
        extra_before = getattr(request.state, "audit_before", {})
        extra_after = getattr(request.state, "audit_after", {})
        before.update(_sanitize(extra_before))
        after.update(_sanitize(extra_after))
        entry = GuiAuditEntry(
            id=uuid4(),
            ts=datetime.now(tz=UTC),
            section=section,
            action=f"{request.method} {path}",
            before=before,
            after=after,
            outcome=outcome,
        )
        await context.repository.append_gui_audit(entry)
        if outcome.startswith("2"):
            manager = getattr(request.app.state, "sessions", None)
            token = request.headers.get(SESSION_HEADER) or request.cookies.get(COOKIE_NAME, "")
            if manager is not None and manager.verify(token):
                url = {
                    "admin": "/admin/storage",
                    "storage": "/admin/storage",
                    "system": "/control",
                    "config": "/risk",
                    "auth": "/security",
                    "trading": "/",
                    "analysis": "/journal",
                }.get(section, "/" + section)
                manager.remember_action(token, entry.action, url)
        hub = getattr(request.app.state, "hub", None)
        if hub is not None:
            await hub.publish(
                "security.audit",
                "audit.created",
                {
                    "id": str(entry.id),
                    "ts": entry.ts.isoformat(),
                    "section": entry.section,
                    "action": entry.action,
                    "outcome": entry.outcome,
                },
            )


def _sanitize(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    result = {}
    for key, value in values.items():
        name = str(key)[:64].lower()
        if any(word in name for word in ("token", "password", "secret", "authorization")):
            result[name] = "[скрыто]"
        elif "account" in name:
            result[name] = mask_account(str(value))
        else:
            result[name] = str(value)[:150]
    return result


def _safe_state(context: AppContext) -> dict[str, str]:
    config = context.config
    scheduler = context.scheduler
    return {
        "config_version": str(config.version),
        "risk_per_trade_pct": str(config.risk_per_trade_pct),
        "safety_multiplier": str(config.min_viable_target_multiplier),
        "allow_counter_trend": str(config.allow_counter_trend),
        "soft_paused": str(bool(context.kill_switch and context.kill_switch.is_engaged)),
        "process_running": str(bool(scheduler and scheduler.is_active)),
        "managed_account_id": mask_account(context.active_account_id),
        "restart_required": str(context.restart_required),
    }
