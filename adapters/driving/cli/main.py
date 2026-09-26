"""CLI: управление ботом, секретами и хранилищем.

Команды:
* ``run`` — запуск торгового цикла в заданном контуре;
* ``secrets set-token`` — положить токен в системное хранилище ОС;
* ``db bootstrap`` — первичное заполнение БД;
* ``db stats`` — статистика хранилища.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Any

import structlog
import typer

from config.enums import ExecutionMode
from config.logging_config import configure_logging
from config.settings import load_settings

logger = structlog.get_logger(__name__)

app_cli = typer.Typer(help="Red-Bot: алгоритмическая система торговли")
secrets_app = typer.Typer(help="Работа с секретами")
db_app = typer.Typer(help="Работа с хранилищем")
app_cli.add_typer(secrets_app, name="secrets")
app_cli.add_typer(db_app, name="db")


@secrets_app.command("set-token")
def secrets_set_token(
    token: str = typer.Option(..., prompt="Токен T-Invest", hide_input=True),
) -> None:
    """Кладёт токен в keyring (Windows Credential Manager / macOS Keychain / Secret Service)."""
    from config.secrets_source import store_token

    store_token(token)
    typer.echo("Токен сохранён в системном хранилище.")


@db_app.command("bootstrap")
def db_bootstrap() -> None:
    """Первичное заполнение БД: инструменты и конфиг v1."""
    asyncio.run(_bootstrap())


async def _bootstrap() -> None:
    from application.composition import build_context
    from application.use_cases.bootstrap_database import bootstrap_database

    settings = load_settings()
    configure_logging(settings.log_level.value, json_logs=settings.log_json)
    # Bootstrap/init must never open a sandbox account or touch a live account.
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)
    try:
        config = await bootstrap_database(context)
        typer.echo(f"БД инициализирована. Конфиг версии {config.version}.")
    finally:
        await context.aclose()


@db_app.command("stats")
def db_stats() -> None:
    """Статистика хранилища по слоям и таблицам."""
    asyncio.run(_stats())


async def _stats() -> None:
    from application.composition import build_context

    settings = load_settings()
    configure_logging(settings.log_level.value, json_logs=settings.log_json)
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)
    try:
        sizes = await context.repository.table_sizes()
        for table, count in sizes.items():
            typer.echo(f"{table:24s} {count:>12,}")
        if context.archive is not None:
            usage = await context.archive.usage_by_layer()
            for layer, size in usage.items():
                typer.echo(f"{layer:24s} {size:>12,} байт")
    finally:
        await context.aclose()


@app_cli.command("run")
def run(
    mode: str | None = typer.Option(
        None, help="Контур: live | sandbox | backtest (по умолчанию сохранённый)"
    ),
    account: str = typer.Option("auto", help="Счёт: auto или явный account ID"),
    host: str = typer.Option("", help="Хост Web GUI (по умолчанию из настроек)"),
    port: int = typer.Option(0, help="Порт Web GUI (по умолчанию из настроек)"),
    *,
    with_gui: bool = typer.Option(True, help="Поднять Web GUI"),
) -> None:
    """Запускает торговый цикл (и GUI, если не отключён)."""
    asyncio.run(_run(mode, account, host or None, port or None, with_gui=with_gui))


def _parse_account_choice(choice: str, accounts: Sequence[dict[str, Any]]) -> str:
    """Принимает показанный номер счёта или его полный ID."""
    choice = choice.strip()
    for account_item in accounts:
        account_id = str(account_item["id"])
        if choice == account_id:
            return account_id

    if choice.isdecimal():
        index = int(choice)
        if 1 <= index <= len(accounts):
            return str(accounts[index - 1]["id"])
    raise ValueError("Введите номер счёта из списка или его полный ID")


def _prompt_for_account(accounts: Sequence[dict[str, Any]]) -> str:
    from application.use_cases.select_account import ACCOUNT_TYPE_LABELS

    typer.echo("Найдено несколько открытых счетов равного приоритета:")
    for index, account_item in enumerate(accounts, start=1):
        account_type = int(account_item.get("type", 0))
        label = ACCOUNT_TYPE_LABELS.get(account_type, "Другой")
        typer.echo(
            f"  {index}. {account_item.get('name') or 'Без названия'} — "
            f"{label}; ID: {account_item['id']}"
        )
    if not sys.stdin.isatty():
        raise typer.BadParameter(
            "Выбор счёта требует интерактивного терминала; повторите запуск с --account <ID>"
        )

    while True:
        choice = typer.prompt("Введите номер счёта из списка или его полный ID")
        try:
            return _parse_account_choice(choice, accounts)
        except ValueError as exc:
            typer.echo(str(exc))


async def _run(
    mode: str | None,
    account: str,
    host: str | None,
    port: int | None,
    *,
    with_gui: bool,
) -> None:
    import uvicorn

    from adapters.driving.web.app import create_app
    from application.composition import build_context, load_saved_execution_mode

    settings = load_settings()
    configure_logging(settings.log_level.value, json_logs=settings.log_json)
    resolved_mode = ExecutionMode(mode) if mode else await load_saved_execution_mode(settings)
    if resolved_mode is ExecutionMode.LIVE:
        typer.confirm(
            "Вы запускаете бота в БОЕВОМ контуре на реальные деньги. Продолжить?",
            abort=True,
        )

    context = await build_context(
        settings,
        mode=resolved_mode,
        requested_account_id=None if account.strip().lower() == "auto" else account.strip(),
        account_selector=_prompt_for_account,
        remember_mode=True,
    )
    settings.web.host = host or settings.web.host
    settings.web.port = port or settings.web.port

    scheduler = _build_scheduler(context)
    context.scheduler = scheduler

    try:
        if with_gui:
            web_app = create_app(context)
            config = uvicorn.Config(
                web_app,
                host="0.0.0.0",  # noqa: S104 - доступ через прокси/контейнер
                port=settings.web.port,
                log_level=settings.log_level.value,
            )
            server = uvicorn.Server(config)
            context.scheduler_task = asyncio.create_task(scheduler.run(), name="bot-scheduler")
            await server.serve()
        else:
            await scheduler.run()
    finally:
        scheduler.stop()
        if context.scheduler_task is not None:
            await context.scheduler_task
        await context.aclose()


def _build_scheduler(context: Any) -> Any:
    from application.scheduler import Scheduler, TaskSpec
    from application.use_cases.make_decision import make_decision
    from application.use_cases.monitor_positions import monitor_positions

    async def decision_cycle() -> None:
        for instrument in context.tradable_instruments:
            await make_decision(context, instrument)

    async def monitor_cycle() -> None:
        await monitor_positions(context)

    scheduler = Scheduler()
    scheduler.add(TaskSpec(name="decisions", cycle=decision_cycle, interval_seconds=300))
    scheduler.add(TaskSpec(name="position_monitor", cycle=monitor_cycle, interval_seconds=60))
    return scheduler


if __name__ == "__main__":
    app_cli()
