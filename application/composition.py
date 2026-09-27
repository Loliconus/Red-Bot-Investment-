"""Composition root — единственное место, где известны все конкретные классы.

Здесь собирается объектный граф: конфиг → адаптеры → контекст → юзкейсы.
Именно здесь подставляется ``SandboxMarketDataAdapter`` вместо
``TBankMarketDataAdapter`` в зависимости от ``execution_mode``; ни один другой
модуль проекта не знает, в каком контуре он работает.

Импорты адаптеров — **ленивые**, внутри ``build_context``: это позволяет
тестировать composition и GUI без установленного T-Invest SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from application.events import EventBus
from application.kill_switch import KillSwitch
from application.scheduler import Scheduler
from application.use_cases.manage_app_config import (
    complete_sandbox_account_creation,
    load_default_mode,
    load_preferred_account_id,
    prepare_sandbox_account_creation,
    save_default_mode,
    save_preferred_account_id,
)
from application.use_cases.select_account import (
    OPEN_ACCOUNT_STATUS,
    AccountSelector,
    resolve_managed_account_id,
)
from config.enums import ExecutionMode
from config.settings import Settings
from core.domain.entities import Instrument, PortfolioState, StrategyConfig
from core.domain.enums import MarketRegime
from core.ports.archive import ArchivePort
from core.ports.broker import OrderExecutionPort
from core.ports.clock import ClockPort, SystemClock
from core.ports.market_data import MarketDataPort
from core.ports.notifier import NotificationPort
from core.ports.persistence import RepositoryPort
from core.risk.position_sizing import SizingResult, calculate_position_size

logger = structlog.get_logger(__name__)

#: Дневной лимит убытка по умолчанию. Переопределяется из БД через GUI.
DEFAULT_DAILY_LOSS_LIMIT_PCT = Decimal("0.03")


@dataclass(slots=True)
class AppContext:
    """Граф зависимостей приложения. Живёт один на процесс."""

    settings: Settings
    market_data: MarketDataPort
    broker: OrderExecutionPort
    repository: RepositoryPort
    archive: ArchivePort | None
    clock: ClockPort
    notifier: NotificationPort | None
    event_bus: EventBus
    config: StrategyConfig
    instruments: list[Instrument]
    benchmark: Instrument | None = None
    portfolio: PortfolioState | None = None
    kill_switch: KillSwitch | None = None
    config_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    regime_cache: dict[str, MarketRegime] = field(default_factory=dict)
    started_at: datetime | None = None
    mode: ExecutionMode | None = None
    scheduler: Scheduler | None = None
    scheduler_task: asyncio.Task[None] | None = None
    managed_account_id: str | None = None
    instrument_enabled: dict[str, bool] = field(default_factory=dict)
    last_restart_at: datetime | None = None
    last_restart_reason: str | None = None
    restart_required: bool = False
    hard_stop_latched: bool = False
    broker_latency_ms: int | None = None
    storage_memory_limit_mb: int | None = None

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.mode or self.settings.execution_mode

    @property
    def active_account_id(self) -> str:
        return self.managed_account_id or self.settings.tbank.account_id

    @property
    def tradable_instruments(self) -> list[Instrument]:
        """Бумаги, которыми можно торговать. IMOEX и soft-off исключены."""
        return [
            i
            for i in self.instruments
            if not i.is_benchmark and self.instrument_enabled.get(i.uid, True)
        ]

    def preview_size(
        self, entry_price: Decimal, stop_price: Decimal, instrument: Instrument
    ) -> SizingResult:
        """Быстрая прикидка размера позиции для GUI."""
        if self.portfolio is None:
            msg = "Портфель не загружен: прикидка размера невозможна"
            raise ValueError(msg)
        return calculate_position_size(
            equity=self.portfolio.total_value,
            risk_pct=self.config.risk_per_trade_pct,
            entry_price=entry_price,
            stop_price=stop_price,
            instrument=instrument,
            max_position_notional=self.config.max_position_notional,
        )

    async def aclose(self) -> None:
        """Аккуратно закрывает ресурсы адаптеров."""
        for resource in (self.market_data, self.broker, self.repository, self.archive):
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.exception("Ошибка при закрытии ресурса %s", type(resource).__name__)


def build_storage(settings: Settings) -> tuple[RepositoryPort, ArchivePort]:
    """Создаёт репозиторий и архив на базе DuckDB + Parquet."""
    from adapters.driven.storage.duckdb_repository import DuckDBRepository
    from adapters.driven.storage.parquet_archive import ParquetArchive

    data_dir: Path = settings.storage.data_dir
    repository = DuckDBRepository(
        db_path=data_dir / "redbot.duckdb",
        memory_limit_mb=settings.storage.duckdb_memory_limit_mb,
        threads=settings.storage.duckdb_threads,
    )
    archive = ParquetArchive(
        repository=repository,
        archive_dir=data_dir / "archive",
        batch_size=settings.storage.archive_batch_size,
    )
    return repository, archive


async def load_saved_execution_mode(settings: Settings) -> ExecutionMode:
    """Читает сохранённый режим до сетевых запросов CLI; иначе использует bootstrap fallback."""
    repository, _ = build_storage(settings)
    try:
        return await load_default_mode(repository, settings.execution_mode)
    finally:
        await repository.aclose()


async def build_tbank_adapters(
    settings: Settings,
    *,
    mode: ExecutionMode = ExecutionMode.LIVE,
    managed_account_id: str | None = None,
) -> tuple[MarketDataPort, OrderExecutionPort]:
    """Создаёт адаптеры T-Invest. Импорт SDK — ленивый, только здесь."""
    from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter
    from adapters.driven.tbank.grpc_client import create_channel
    from adapters.driven.tbank.market_data_adapter import TBankMarketDataAdapter

    channel = await create_channel(settings, mode=mode)
    market_data: MarketDataPort = TBankMarketDataAdapter(channel)
    broker: OrderExecutionPort = TBankBrokerAdapter(
        channel, account_id=managed_account_id or settings.tbank.account_id
    )
    return market_data, broker


def build_backtest_adapters(
    settings: Settings,
    *,
    repository: RepositoryPort | None = None,
) -> tuple[MarketDataPort, OrderExecutionPort]:
    """Адаптеры для бэктеста: реплей истории + симулятор исполнения.

    Реплей получает репозиторий, чтобы читать справочник инструментов из
    сохранённого каталога: в бэктесте нет сети, а лот и UID обязаны быть теми
    же, что прислал API при обновлении каталога.
    """
    from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
    from adapters.driven.backtest.simulated_broker import SimulatedBroker

    data_dir: Path = settings.storage.data_dir
    replay: MarketDataPort = BacktestReplayAdapter(
        data_dir=data_dir / "history", repository=repository
    )
    broker: OrderExecutionPort = SimulatedBroker()
    return replay, broker


async def build_context(
    settings: Settings,
    *,
    mode: ExecutionMode | None = None,
    clock: ClockPort | None = None,
    notifier: NotificationPort | None = None,
    instruments: list[Instrument] | None = None,
    config: StrategyConfig | None = None,
    requested_account_id: str | None = None,
    account_selector: AccountSelector | None = None,
    remember_mode: bool = False,
) -> AppContext:
    """Собирает граф зависимостей под конкретный контур исполнения."""
    clock = clock or SystemClock()
    event_bus = EventBus()

    repository, archive = build_storage(settings)
    if mode is None:
        mode = await load_default_mode(repository, settings.execution_mode)
    if mode is ExecutionMode.LIVE:
        token = settings.tbank.api_token.get_secret_value().strip()
        if not token or token == "changeme":
            await repository.aclose()
            raise ValueError("api_token не заполнен — боевой режим невозможен")
    if remember_mode:
        await save_default_mode(repository, mode)

    # Явный CLI ID и bootstrap/ENV ID проверяются у брокера как открытые счета.
    configured_account_id = (
        requested_account_id.strip()
        if requested_account_id and requested_account_id.strip()
        else (settings.tbank.account_id.strip() or "")
    )
    preferred_account_id = await load_preferred_account_id(repository, mode)
    active_account_id = configured_account_id
    memory_override = await repository.get_operational_value("duckdb_memory_limit_mb")
    active_memory_mb = (
        int(memory_override) if memory_override else settings.storage.duckdb_memory_limit_mb
    )
    if memory_override:
        await repository.set_memory_limit_mb(active_memory_mb)

    if mode is ExecutionMode.BACKTEST:
        market_data, broker = build_backtest_adapters(settings, repository=repository)
        active_account_id = active_account_id or "backtest"
    else:
        if mode is ExecutionMode.SANDBOX:
            from adapters.driven.sandbox.sandbox_adapter import create_sandbox_adapters

            market_data, broker = await create_sandbox_adapters(
                settings, managed_account_id=configured_account_id
            )
        else:
            market_data, broker = await build_tbank_adapters(
                settings,
                mode=mode,
                managed_account_id=configured_account_id,
            )
        try:
            known_accounts: list[dict[str, Any]] | None = None
            if mode is ExecutionMode.SANDBOX:
                get_accounts = getattr(broker, "get_accounts", None)
                if get_accounts is None:
                    raise RuntimeError("Sandbox адаптер не поддерживает список счетов")
                known_accounts = await get_accounts()
                has_open_account = any(
                    int(account.get("status", 0)) == OPEN_ACCOUNT_STATUS
                    for account in known_accounts
                )
                # Маркер фиксируется до неидемпотентного открытия: при неопределённом
                # результате следующий запуск не создаст второй sandbox-счёт вслепую.
                await prepare_sandbox_account_creation(
                    repository,
                    has_open_account=has_open_account,
                    explicit_account_id=configured_account_id or None,
                )

            # Сначала проверяем явный ID, затем mode-specific saved default, затем
            # типы счетов. При пустом sandbox списке открывается только счёт — без pay-in.
            active_account_id = await resolve_managed_account_id(
                broker,
                mode=mode,
                requested_account_id=configured_account_id or None,
                preferred_account_id=preferred_account_id,
                selector=account_selector,
                accounts=known_accounts,
            )
            configure_account = getattr(broker, "configure_managed_account", None)
            if configure_account is not None:
                configure_account(active_account_id)
            await save_preferred_account_id(repository, mode, active_account_id)
        except BaseException:
            await market_data.aclose()
            await repository.aclose()
            raise

    kill_switch = KillSwitch(
        clock=clock,
        event_bus=event_bus,
        daily_loss_limit_pct=DEFAULT_DAILY_LOSS_LIMIT_PCT,
    )

    active_config = config or await repository.get_active_strategy_config()
    if active_config is None:
        active_config = _default_config(settings)
        await repository.save_strategy_config(active_config)

    stored_instruments = instruments or await repository.list_instruments()
    benchmark = next((i for i in stored_instruments if i.is_benchmark), None)

    initial_portfolio: PortfolioState | None = None
    if mode is ExecutionMode.SANDBOX:
        try:
            get_portfolio = getattr(broker, "get_portfolio")
            initial_portfolio = await get_portfolio()
            if initial_portfolio is None:
                raise RuntimeError("Sandbox API не вернул портфель выбранного счета")
            await complete_sandbox_account_creation(repository)
        except BaseException:
            await market_data.aclose()
            await repository.aclose()
            raise
    else:
        if hasattr(broker, "get_portfolio"):
            try:
                initial_portfolio = await broker.get_portfolio()
            except Exception:  # noqa: BLE001
                initial_portfolio = None
        if initial_portfolio is None:
            try:
                initial_portfolio = await repository.get_latest_portfolio_state()
            except Exception:  # noqa: BLE001
                initial_portfolio = None

    # Только бэктест получает начальный виртуальный капитал. Sandbox не должен
    # показывать пользователю локальный баланс, отсутствующий в T-Invest API.
    if initial_portfolio is None and mode is ExecutionMode.BACKTEST:
        import json

        initial_portfolio = PortfolioState(
            account_id=active_account_id or "backtest",
            total_value=Decimal("1000000"),
            available_cash=Decimal("1000000"),
            positions_value=Decimal("0"),
            updated_at=clock.now(),
        )
        try:
            await repository.save_portfolio_state(
                json.dumps(
                    {
                        "account_id": initial_portfolio.account_id,
                        "total_value": str(initial_portfolio.total_value),
                        "available_cash": str(initial_portfolio.available_cash),
                        "positions_value": str(initial_portfolio.positions_value),
                        "updated_at": initial_portfolio.updated_at.isoformat(),
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_initial_portfolio_failed", error=str(exc))

    ctx = AppContext(
        settings=settings,
        market_data=market_data,
        broker=broker,
        repository=repository,
        archive=archive,
        clock=clock,
        notifier=notifier,
        event_bus=event_bus,
        config=active_config,
        instruments=stored_instruments,
        benchmark=benchmark,
        portfolio=initial_portfolio,
        kill_switch=kill_switch,
        started_at=clock.now(),
        mode=mode,
        managed_account_id=active_account_id,
        storage_memory_limit_mb=active_memory_mb,
        instrument_enabled={
            i.uid: (await repository.get_operational_value(f"instrument:{i.uid}:enabled"))
            != "false"
            for i in stored_instruments
        },
    )

    if mode in {ExecutionMode.LIVE, ExecutionMode.SANDBOX}:
        await _refresh_catalog_if_stale(ctx)

    logger.info(
        "context_built",
        mode=mode.value,
        instruments=len(stored_instruments),
        config_version=active_config.version,
    )
    return ctx


async def _refresh_catalog_if_stale(ctx: AppContext) -> None:
    """Подтягивает справочник инструментов из API, если он устарел.

    Каталог — данные для торговли и GUI, поэтому он не хардкодится: список
    загружается из ``InstrumentsService`` и сохраняется в БД. Ошибка сети на
    старте не должна ронять приложение — используем сохранённый каталог.
    """
    from application.use_cases.manage_instrument_catalog import ensure_catalog_fresh

    try:
        refreshed = await ensure_catalog_fresh(ctx)
    except Exception:  # noqa: BLE001 - сеть на старте не обязана быть доступна
        logger.warning("instrument_catalog_refresh_failed", mode=ctx.execution_mode.value)
        return
    if refreshed:
        logger.info("instrument_catalog_updated", mode=ctx.execution_mode.value)


def _default_config(settings: Settings) -> StrategyConfig:
    """Конфиг по умолчанию, если БД пуста (без обращения к API)."""
    from config.seed_defaults import DEFAULT_CONFLUENCE_WEIGHTS

    risks = settings.risk_defaults
    return StrategyConfig(
        version=1,
        risk_per_trade_pct=Decimal(str(risks.max_risk_per_trade_pct)),
        min_viable_target_multiplier=Decimal(str(risks.min_viable_target_multiplier)),
        commission_rate=Decimal(str(risks.commission_rate)),
        max_holding_hours=risks.default_max_holding_hours,
        max_position_notional=Decimal("500000"),
        confluence_threshold=Decimal("0.3"),
        confluence_weights=dict(DEFAULT_CONFLUENCE_WEIGHTS),
    )
