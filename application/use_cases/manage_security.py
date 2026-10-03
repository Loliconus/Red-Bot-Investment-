"""Write-only секрет брокера и неизменяемый журнал GUI."""

from __future__ import annotations

from datetime import datetime

from application.composition import AppContext
from config.secrets_source import store_token
from core.ports.persistence import GuiAuditEntry


async def save_broker_token(context: AppContext, token: str) -> str:
    if not token.strip() or len(token) > 4096:
        raise ValueError("Введите токен допустимой длины")
    # Не помещаем значение ни в context, ни в ответ/аудит/логи.
    store_token(token)
    updated = context.clock.now().isoformat()
    await context.repository.set_operational_value("token_updated_at", updated)
    context.restart_required = True  # старый gRPC-канал не меняет авторизацию на лету
    return updated


async def token_metadata(context: AppContext) -> dict[str, str | None]:
    return {
        "source": "keyring",
        "updated_at": await context.repository.get_operational_value("token_updated_at"),
    }


async def read_audit(
    context: AppContext,
    *,
    section: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[GuiAuditEntry]:
    return await context.repository.list_gui_audit(
        section=section, action=action, since=since, limit=limit
    )
