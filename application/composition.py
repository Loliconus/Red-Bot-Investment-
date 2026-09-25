"""Composition root — единственное место, где известны все конкретные классы.

Здесь собирается объектный граф: конфиг → адаптеры → контекст → юзкейсы.
Именно здесь подставляется ``SandboxMarketDataAdapter`` вместо
``TBankMarketDataAdapter`` в зависимости от ``execution_mode``; ни один другой
модуль проекта не знает, в каком контуре он работает.

Импорты адаптеров — **ленивые**, внутри ``build_context``: это позволяет
тестировать composition и GUI без установленного T-Invest SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from application.events import EventBus
from application.kill_switch import KillSwitch
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

    @property
    def tradable_instruments(self) -> list[Instrument]:
        """Бумаги, которыми можно торговать. IMOEX сюда не входит."""
        return [i for i in self.instruments if not i.is_benchmark]

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


async def build_tbank_adapters(
    settings: Settings,
) -> tuple[MarketDataPort, OrderExecutionPort]:
    """Создаёт адаптеры T-Invest. Импорт SDK — ленивый, только здесь."""
    from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter
    from adapters.driven.tbank.grpc_client import create_channel
    from adapters.driven.tbank.market_data_adapter import TBankMarketDataAdapter

    channel = await create_channel(settings)
    market_data: MarketDataPort = TBankMarketDataAdapter(channel)
    broker: OrderExecutionPort = TBankBrokerAdapter(channel, account_id=settings.tbank.account_id)
    return market_data, broker


def build_backtest_adapters(
    settings: Settings,
) -> tuple[MarketDataPort, OrderExecutionPort]:
    """Адаптеры для бэктеста: реплей истории + симулятор исполнения."""
    from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
    from adapters.driven.backtest.simulated_broker import SimulatedBroker

    data_dir: Path = settings.storage.data_dir
    replay: MarketDataPort = BacktestReplayAdapter(data_dir=data_dir / "history")
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
) -> AppContext:
    """Собирает граф зависимостей под конкретный контур исполнения."""
    mode = mode or settings.execution_mode
    clock = clock or SystemClock()
    event_bus = EventBus()

    repository, archive = build_storage(settings)

    if mode is ExecutionMode.BACKTEST:
        market_data, broker = build_backtest_adapters(settings)
    else:
        market_data, broker = await build_tbank_adapters(settings)

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
        kill_switch=kill_switch,
        started_at=clock.now(),
    )

    logger.info(
        "context_built",
        mode=mode.value,
        instruments=len(stored_instruments),
        config_version=active_config.version,
    )
    return ctx


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
