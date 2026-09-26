"""Настройки запуска, сохраняемые приложением в operational_settings DuckDB."""

from __future__ import annotations

from config.enums import ExecutionMode
from core.ports.persistence import RepositoryPort

DEFAULT_MODE_KEY = "execution_mode"
LEGACY_ACCOUNT_KEY = "managed_account_id"
SANDBOX_OPEN_PENDING_KEY = "sandbox_account_creation_pending"


def account_setting_key(mode: ExecutionMode) -> str:
    """Раздельный default-счёт для live и sandbox, чтобы не смешивать контуры."""
    return f"managed_account_id:{mode.value}"


async def load_default_mode(
    repository: RepositoryPort,
    fallback: ExecutionMode,
) -> ExecutionMode:
    raw = await repository.get_operational_value(DEFAULT_MODE_KEY)
    if not raw:
        return fallback
    try:
        return ExecutionMode(raw)
    except ValueError:
        return fallback


async def save_default_mode(repository: RepositoryPort, mode: ExecutionMode) -> None:
    await repository.set_operational_value(DEFAULT_MODE_KEY, mode.value)


async def load_preferred_account_id(
    repository: RepositoryPort,
    mode: ExecutionMode,
) -> str | None:
    """Читает account default текущего контура, с fallback на старый единый ключ."""
    raw = await repository.get_operational_value(account_setting_key(mode))
    if raw is not None:
        return raw if raw and raw != "auto" else None
    # Миграционная совместимость: старый ID будет принят только если API
    # подтвердит, что этот счёт открыт в выбранном контуре.
    legacy = await repository.get_operational_value(LEGACY_ACCOUNT_KEY)
    return legacy or None


async def save_preferred_account_id(
    repository: RepositoryPort,
    mode: ExecutionMode,
    account_id: str,
) -> None:
    value = account_id.strip() or "auto"
    await repository.set_operational_value(account_setting_key(mode), value)


async def prepare_sandbox_account_creation(
    repository: RepositoryPort,
    *,
    has_open_account: bool,
    explicit_account_id: str | None,
) -> None:
    """Запоминает pending перед неидемпотентным open, не повторяя неопределённый вызов."""
    pending = (
        await repository.get_operational_value(SANDBOX_OPEN_PENDING_KEY)
    ) == "true"
    if pending and not has_open_account:
        raise RuntimeError(
            "Предыдущий запрос создания sandbox-счёта мог завершиться успешно, "
            "но счёт пока не виден в списке. Повторное открытие запрещено во "
            "избежание дубликата; проверьте список счетов и повторите запуск."
        )
    if not has_open_account and not explicit_account_id and not pending:
        await repository.set_operational_value(SANDBOX_OPEN_PENDING_KEY, "true")


async def complete_sandbox_account_creation(repository: RepositoryPort) -> None:
    await repository.set_operational_value(SANDBOX_OPEN_PENDING_KEY, "false")


async def set_account_preference(
    repository: RepositoryPort,
    broker: object,
    mode: ExecutionMode,
    account_id: str,
) -> None:
    """Сохраняет auto либо только ID, подтверждённый как открытый в текущем API."""
    value = account_id.strip()
    if value and value.lower() != "auto":
        get_accounts = getattr(broker, "get_accounts", None)
        if get_accounts is None:
            raise ValueError("Адаптер текущего контура не поддерживает список счетов")
        accounts = await get_accounts()
        if not any(
            str(account.get("id")) == value and int(account.get("status", 0)) == 2
            for account in accounts
        ):
            raise ValueError("Можно сохранить только ID открытого счёта текущего контура")
    selected_id = "" if not value or value.lower() == "auto" else value
    await save_preferred_account_id(repository, mode, selected_id)
