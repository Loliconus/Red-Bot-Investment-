"""CLI: управление ботом, секретами и хранилищем.

Команды:
* ``run`` — запуск торгового цикла в заданном контуре;
* ``secrets set-token`` — положить токен в системное хранилище ОС;
* ``db bootstrap`` — первичное заполнение БД;
* ``db stats`` — статистика хранилища.
"""

from __future__ import annotations

import asyncio
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
    context = await build_context(settings)
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
    mode: str = typer.Option("sandbox", help="Контур: live | sandbox | backtest"),
    host: str = typer.Option("", help="Хост Web GUI (по умолчанию из настроек)"),
    port: int = typer.Option(0, help="Порт Web GUI (по умолчанию из настроек)"),
    *,
    with_gui: bool = typer.Option(True, help="Поднять Web GUI"),
) -> None:
    """Запускает торговый цикл (и GUI, если не отключён)."""
    asyncio.run(_run(mode, host or None, port or None, with_gui=with_gui))


async def _run(mode: str, host: str | None, port: int | None, *, with_gui: bool) -> None:
    import uvicorn

    from adapters.driving.web.app import create_app
    from application.composition import build_context

    resolved_mode = ExecutionMode(mode)
    if resolved_mode is ExecutionMode.LIVE:
        typer.confirm(
            "Вы запускаете бота в БОЕВОМ контуре на реальные деньги. Продолжить?",
            abort=True,
        )

    settings = load_settings()
    configure_logging(settings.log_level.value, json_logs=settings.log_json)

    context = await build_context(settings, mode=resolved_mode)
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
