"""Настройки следующего запуска и счёта активного execution mode."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.manage_account import get_account_overview
from application.use_cases.manage_app_config import (
    load_default_mode,
    load_preferred_account_id,
    save_default_mode,
    set_account_preference,
)
from application.use_cases.manage_risk import get_risk_state, mask_account
from application.use_cases.select_account import ACCOUNT_TYPE_LABELS, OPEN_ACCOUNT_STATUS
from config.enums import ExecutionMode

router = APIRouter(tags=["app-settings"], dependencies=[Depends(require_session)])


def _sorted_open_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {1: 0, 2: 1, 3: 2}
    return sorted(
        (account for account in accounts if int(account.get("status", 0)) == OPEN_ACCOUNT_STATUS),
        key=lambda account: (
            priority.get(int(account.get("type", 0)), 99),
            str(account.get("name", "")).casefold(),
            str(account.get("id", "")),
        ),
    )


@router.get("/settings")
async def page(request: Request, context: ContextDep) -> Any:
    saved_mode = await load_default_mode(context.repository, context.execution_mode)
    selected_account = await load_preferred_account_id(context.repository, context.execution_mode)
    accounts: list[dict[str, Any]] = []
    accounts_error = ""
    get_accounts = getattr(context.broker, "get_accounts", None)
    if get_accounts is not None:
        try:
            accounts = _sorted_open_accounts(await get_accounts())
        except Exception:  # noqa: BLE001 - UI показывает недоступность, не секрет SDK детали
            accounts_error = "Список счетов сейчас недоступен. Проверьте соединение с брокером."

    # Администрирование счетов переехало сюда из «Риска»: это функция запуска,
    # а не лимитов. Sandbox-панель и визард используют те же use cases.
    account_overview: dict[str, Any] | None = None
    account_overview_error = ""
    try:
        account_overview = await get_account_overview(context)
    except Exception:  # noqa: BLE001 - недоступность показываем честно
        account_overview_error = "Данные счёта сейчас недоступны."
    return render_page(
        request,
        "pages/settings.html",
        title="Счёт и режим",
        section="settings",
        data={
            "execution_modes": list(ExecutionMode),
            "saved_mode": saved_mode,
            "active_mode": context.execution_mode,
            "accounts": accounts,
            "accounts_error": accounts_error,
            "selected_account": selected_account or "auto",
            "account_type_labels": ACCOUNT_TYPE_LABELS,
            "account": account_overview,
            "account_overview_error": account_overview_error,
            "risk": get_risk_state(context),
            "masked_account": mask_account(context.active_account_id),
        },
    )


@router.post("/settings/mode")
async def update_mode(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    execution_mode: str = Form(...),
) -> Any:
    try:
        mode = ExecutionMode(execution_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестный режим исполнения") from exc
    await save_default_mode(context.repository, mode)
    return render_partial(
        request,
        "partials/settings_status.html",
        {
            "message": (
                f"Режим следующего запуска сохранён: {mode.value.upper()}. "
                "Текущий контур не изменён."
            ),
            "error": "",
        },
    )


@router.post("/settings/account")
async def update_account(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    account_id: str = Form("auto"),
) -> Any:
    if context.execution_mode is ExecutionMode.BACKTEST:
        return render_partial(
            request,
            "partials/settings_status.html",
            {
                "message": "",
                "error": "Backtest не использует брокерские счета; запустите live или sandbox для настройки account ID.",
            },
            status_code=400,
        )
    try:
        await set_account_preference(
            context.repository,
            context.broker,
            context.execution_mode,
            account_id,
        )
    except ValueError as exc:
        return render_partial(
            request,
            "partials/settings_status.html",
            {"message": "", "error": str(exc)},
            status_code=400,
        )
    label = "автоматический выбор" if account_id.strip().lower() == "auto" else "выбранный счёт"
    return render_partial(
        request,
        "partials/settings_status.html",
        {
            "message": (
                f"Сохранён {label} для контура {context.execution_mode.value.upper()}; "
                "применяется после перезапуска."
            ),
            "error": "",
        },
    )
